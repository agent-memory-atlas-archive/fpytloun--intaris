import json
import stat
from argparse import Namespace
from unittest.mock import Mock

import httpx
import pytest

from tools.paid_write_replay import (
    Meter,
    MeteredTransport,
    ReplayStop,
    refresh_token,
    run,
)
from tools.rc3_replay import private_write


def test_token_refresh_uses_explicit_namespace(monkeypatch, tmp_path):
    target = tmp_path / "token"
    monkeypatch.setenv("REPLAY_TOKEN_NAMESPACE", "example-app")
    monkeypatch.setenv("REPLAY_TOKEN_POD", "guardrails-0")
    monkeypatch.setenv("REPLAY_TOKEN_PATH", "/run/credentials/inference")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(target))

    def copy_token(command, **kwargs):
        assert command == [
            "kubectl",
            "-n",
            "example-app",
            "exec",
            "guardrails-0",
            "--",
            "cat",
            "/run/credentials/inference",
        ]
        kwargs["stdout"].write(b"synthetic-test-token")

    monkeypatch.setattr("tools.paid_write_replay.subprocess.run", copy_token)
    assert refresh_token()
    assert target.read_bytes() == b"synthetic-test-token"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_token_refresh_requires_explicit_namespace(monkeypatch):
    monkeypatch.setenv("REPLAY_TOKEN_POD", "guardrails-0")
    monkeypatch.setenv("REPLAY_TOKEN_PATH", "/run/credentials/inference")
    monkeypatch.delenv("REPLAY_TOKEN_NAMESPACE", raising=False)
    launch = Mock()
    monkeypatch.setattr("tools.paid_write_replay.subprocess.run", launch)
    with pytest.raises(KeyError, match="REPLAY_TOKEN_NAMESPACE"):
        refresh_token()
    launch.assert_not_called()


def test_incremental_snapshots_close_handles_without_gc(tmp_path):
    import gc
    import sqlite3
    from contextlib import closing

    from tools.paid_write_replay import snapshot_db

    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE evidence (value TEXT)")
        conn.commit()
    descriptors = __import__("pathlib").Path("/proc/self/fd")
    before = len(list(descriptors.iterdir()))
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(300):
            snapshot_db(source, tmp_path / "audit.db")
        assert len(list(descriptors.iterdir())) <= before + 2
    finally:
        if enabled:
            gc.enable()


def test_fake_transport_cost_stop_and_no_further_request():
    meter = Meter(input_rate=1, output_rate=1)
    handler = Mock(
        return_value=httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 3_000_000, "completion_tokens": 2_000_001}
            },
        )
    )
    with httpx.Client(
        transport=MeteredTransport(meter, httpx.MockTransport(handler))
    ) as client:
        with pytest.raises(ReplayStop, match="cost_limit"):
            client.post("http://localhost/v1/chat/completions")
        assert meter.cost > 5
        with pytest.raises(ReplayStop, match="cost_limit"):
            client.post("http://localhost/v1/chat/completions")
    assert handler.call_count == 1


def test_fake_transport_error_fraction_stops_after_twenty_calls():
    meter = Meter()

    def handler(request):
        if meter.requests <= 3:
            return httpx.Response(502, json={"error": "transport"})
        return httpx.Response(
            200, json={"response": {"usage": {"input_tokens": 20, "output_tokens": 10}}}
        )

    with httpx.Client(
        transport=MeteredTransport(meter, httpx.MockTransport(handler))
    ) as client:
        for index in range(20):
            client.post("http://localhost/v1/chat/completions")
            meter.finish()
            if index < 19:
                assert meter.stop_reason is None
        assert meter.stop_reason == "transport_error_limit"
        with pytest.raises(ReplayStop):
            client.post("http://localhost/v1/chat/completions")
    assert meter.requests == 20
    assert meter.transport_calls == 3
    assert (meter.prompt, meter.completion) == (340, 170)


def test_missing_usage_fails_closed():
    meter = Meter()
    with httpx.Client(
        transport=MeteredTransport(
            meter,
            httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": []})),
        )
    ) as client:
        with pytest.raises(ReplayStop, match="usage_unavailable"):
            client.post("http://localhost/v1/chat/completions")


def test_422_schema_rejection_is_request_error_with_role_not_transport():
    meter = Meter()
    with httpx.Client(
        transport=MeteredTransport(
            meter,
            httpx.MockTransport(
                lambda req: httpx.Response(
                    422,
                    json={
                        "error": {
                            "code": "upstream_json_validate_failed",
                            "category": "request",
                        }
                    },
                )
            ),
        )
    ) as client:
        response = client.post(
            "http://localhost/v1/chat/completions",
            json={
                "model": "routes/intaris_analysis",
                "response_format": {"type": "json_schema"},
            },
        )
    assert response.status_code == 422
    assert not meter.transport_error
    assert meter.events[0]["role"] == "intaris_analysis"
    assert meter.events[0]["code"] == "upstream_json_validate_failed"


def test_resume_cost_includes_prior_spend():
    meter = Meter()
    meter.prior_cost = 5
    with pytest.raises(ReplayStop, match="cost_limit"):
        meter.before()
    assert meter.requests == 0


def test_resume_excludes_first_twenty_and_refuses_repeat(tmp_path, monkeypatch):
    from intaris import intention, llm

    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(tmp_path / "unused"))
    monkeypatch.setattr(llm, "LLMClient", Mock(return_value=Mock()))
    monkeypatch.setattr(intention, "generate_intention", Mock(return_value=None))
    original = tmp_path / "original"
    original.mkdir()
    private_write(
        original / "paid-input.json",
        [
            {
                "call_id": f"call-{n}",
                "tool": "write",
                "args": {},
                "policy": {},
                "details": {},
                "intention": "Update files",
                "user_messages": [],
                "stratum": "file_writes",
            }
            for n in range(300)
        ],
    )
    (original / "results.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "call_id": f"call-{n}",
                    "cost_usd": 0.001,
                }
            )
            + "\n"
            for n in range(20)
        )
    )
    output = tmp_path / "continuation"
    options = Namespace(approve_paid=True, output=output, resume_from=original)
    run(options)
    entries = [
        json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()
    ]
    assert len(entries) == 280
    assert {r["call_id"] for r in entries} == {f"call-{n}" for n in range(20, 300)}
    assert (
        json.loads((output / "paid-report.json").read_text())[
            "approve_agreement_percent"
        ]
        == 0
    )
    with pytest.raises(FileExistsError):
        run(options)
    with pytest.raises(FileExistsError):
        run(
            Namespace(
                approve_paid=True, output=tmp_path / "another", resume_from=original
            )
        )
    assert entries[-1]["cumulative_cost_usd"] == pytest.approx(0.02)


def test_resume_rejects_duplicate_original_ids_before_reservation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(tmp_path / "unused"))
    private_write(
        tmp_path / "paid-input.json",
        [{"call_id": f"call-{n % 299}"} for n in range(300)],
    )
    (tmp_path / "results.jsonl").write_text(
        "".join(json.dumps({"call_id": f"call-{n}"}) + "\n" for n in range(20))
    )
    with pytest.raises(ValueError, match="original 300"):
        run(
            Namespace(
                approve_paid=True,
                output=tmp_path / "continuation",
                resume_from=tmp_path,
            )
        )
    assert not (tmp_path / "continuation-reserved.json").exists()


def test_expired_token_is_refreshed_once_without_transport_failure(
    tmp_path, monkeypatch
):
    token = tmp_path / "token"
    token.write_text("initial-test-token")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token))

    def refresh():
        token.write_text("refreshed-test-token")
        return True

    def handler(request):
        if request.headers["Authorization"] == "Bearer initial-test-token":
            return httpx.Response(401)
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        )

    meter = Meter()
    with httpx.Client(
        transport=MeteredTransport(meter, httpx.MockTransport(handler), refresh)
    ) as client:
        assert (
            client.post(
                "http://localhost/v1/chat/completions",
                headers={"Authorization": "Bearer initial-test-token"},
            ).status_code
            == 200
        )
    assert meter.requests == 2
    assert not meter.transport_error


def test_profile_failure_is_incremental_and_batch_cannot_rerun(tmp_path, monkeypatch):
    from intaris import intention, llm

    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(tmp_path / "unused"))
    monkeypatch.setattr(llm, "LLMClient", Mock(return_value=Mock()))
    generator = Mock(side_effect=RuntimeError("must not print provider details"))
    monkeypatch.setattr(intention, "generate_intention", generator)
    private_write(
        tmp_path / "paid-input.json",
        [
            {
                "call_id": f"local-{n}",
                "tool": "write",
                "args": {"content": "private"},
                "policy": {},
                "details": {},
                "intention": "Update file",
                "user_messages": [],
                "stratum": "file_writes",
            }
            for n in range(2)
        ],
    )
    run(Namespace(approve_paid=True, output=tmp_path))
    entries = [
        json.loads(line)
        for line in (tmp_path / "results.jsonl").read_text().splitlines()
    ]
    assert len(entries) == 2
    # Profile failure is now optional; the intentionally broken evaluator mock
    # fails separately after raw-source fallback, without stopping the journal.
    assert all(e["profile_errors"] == 1 for e in entries)
    assert all(e["error_type"] != "RuntimeError" for e in entries)
    assert "private" not in (tmp_path / "results.jsonl").read_text()
    for name in ("results.jsonl", "audit.db"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        run(Namespace(approve_paid=True, output=tmp_path))
    assert generator.call_count == 2


def test_jev_transport_meters_usage_errors_and_cost_stop():
    from tools.paid_write_replay import JEV_INPUT_RATE, JevMeteredTransport

    meter = Meter(max_cost=1.0)
    responses = iter(
        [
            httpx.Response(200, json={"usage": {"input_tokens": 1000}}),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"usage": {"input_tokens": 30_000_000}}),
        ]
    )
    with httpx.Client(
        transport=JevMeteredTransport(
            meter, httpx.MockTransport(lambda request: next(responses))
        )
    ) as client:
        client.post("https://openrouter.ai/api/alpha/decisions", json={})
        assert meter.extra_cost == pytest.approx(1000 * JEV_INPUT_RATE / 1e6)
        assert meter.events[-1]["role"] == "jev"
        assert not meter.transport_error
        client.post("https://openrouter.ai/api/alpha/decisions", json={})
        # Missing usage bills the request size rather than nothing.
        assert meter.events[-1]["usage_estimated"]
        assert meter.transport_error
        with pytest.raises(ReplayStop, match="cost_limit"):
            client.post("https://openrouter.ai/api/alpha/decisions", json={})


def test_jev_evaluator_backend_requires_key_and_pins_openrouter(monkeypatch, tmp_path):
    from tools.paid_write_replay import make_evaluator_backend

    assert make_evaluator_backend("llm", Meter()) is None
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    with pytest.raises(ValueError, match="JEV_API_KEY"):
        make_evaluator_backend("jev", Meter())
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    # The runners always set the managed Cognis route for intention and Judge;
    # the evaluator must still be standalone OpenRouter Jev.
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:9/v1")
    token = tmp_path / "token.jwt"
    token.write_text("managed-token")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token))
    client = make_evaluator_backend("jev", Meter())
    try:
        assert client._client.base_url.host == "openrouter.ai"
        assert client._request_path == "/alpha/decisions"
        assert client._model == "typesafe/jev-1.13-20260917"
        assert client._api_key == "test-key"
    finally:
        client.close()
