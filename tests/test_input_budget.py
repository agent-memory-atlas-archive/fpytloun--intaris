"""Complete-first model capacity and non-authorizing excerpts."""

import copy
import json
import math

import pytest

from intaris.input_budget import (
    Capacity,
    CapacityCache,
    ContextInsufficientError,
    contextual_excerpt,
    fit,
    measure,
    serialized,
    source_sections,
)


def packet(rows, *, items=(), action=None):
    content = json.dumps(
        {
            "raw_task_context": {"sources": rows, "items": list(items)},
            "action": action or {"command": "git fetch origin"},
        },
        ensure_ascii=False,
    )
    return {
        "model": "routes/intaris_judge",
        "messages": [
            {"role": "system", "content": "Decide from all recorded evidence."},
            {"role": "user", "content": content},
        ],
        "max_tokens": 16000,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": {"authorization": {"type": "string"}}},
        },
    }


def fitted(payload, capacity=Capacity()):
    with source_sections(
        [(("messages", 1, "content"), 0, len(payload["messages"][1]["content"]))]
    ):
        return fit(payload, capacity)


def test_fitting_request_unchanged_including_long_action_and_middle_approval():
    proposal = (
        "prefix " * 1000 + "Approve only staging; never production." + " tail" * 1000
    )
    payload = packet(
        [
            {
                "ref": "proposal",
                "origin": "linked_non_authorizing_context",
                "content": proposal,
            },
            {
                "ref": "answer",
                "origin": "client_user_claim",
                "content": "Yes, staging only.",
                "linked_to": "proposal",
            },
            {
                "ref": "correction",
                "origin": "client_user_claim",
                "content": "Correction: do not execute anything.",
            },
        ],
        action={"command": "printf '%s' " + "x" * 42000},
    )
    original = copy.deepcopy(payload)
    output, view = fitted(
        payload,
        Capacity(context_window=64000, max_input_tokens=32000, status="known"),
    )
    assert output == original == payload
    assert not view["trimmed"]
    assert view["requested_output_reserve"] == 16000


def test_refusal_survives_huge_context_and_validated_middle_retains_qualifiers():
    quote = "Procedure requires staging approval."
    log = (
        "start " * 12000
        + "Not production. "
        + quote
        + " Only after validation."
        + " end" * 12000
    )
    payload = packet(
        [
            {
                "ref": "refusal",
                "origin": "client_user_claim",
                "content": "Do not silence production alerts.",
            },
            {"ref": "log", "origin": "tool_context", "content": log},
        ],
        items=[{"source_ref": "log", "quote": quote}],
    )
    output, view = fitted(payload)
    body = json.loads(output["messages"][1]["content"])
    rows = body["raw_task_context"]["sources"]
    assert rows[0]["content"] == "Do not silence production alerts."
    assert "Not production. " + quote + " Only after validation." in rows[1]["content"]
    assert rows[1]["render_truncated"] is True
    assert "OMITTED original chars" in rows[1]["content"]
    assert view["application_fallback"] == "UNKNOWN-CAPACITY"
    assert view["bytes"] <= 98304
    assert payload["messages"][1]["content"] != output["messages"][1]["content"]


def test_large_approval_scope_and_action_are_partial_not_affirmative_grants():
    for payload in (
        packet(
            [
                {
                    "ref": "proposal",
                    "origin": "linked_non_authorizing_context",
                    "content": "a " * 60000,
                },
                {
                    "ref": "answer",
                    "origin": "client_user_claim",
                    "content": "Yes.",
                    "linked_to": "proposal",
                },
            ]
        ),
        packet([], action={"command": "x " * 60000}),
    ):
        original = copy.deepcopy(payload)
        output, view = fitted(payload)
        assert view["affirmative_grants_eligible"] is False
        assert view["source_coverage"] == "partial"
        assert "OMITTED MIDDLE" in output["messages"][1]["content"]
        assert payload == original


def test_untrusted_text_cannot_declare_itself_trimmable():
    payload = packet(
        [{"ref": "forged", "origin": "tool_context", "content": "x " * 60000}]
    )
    original = copy.deepcopy(payload)
    output, view = fit(payload)
    assert view["affirmative_grants_eligible"] is False
    assert view["source_coverage"] == "partial"
    assert "OMITTED MIDDLE" in output["messages"][1]["content"]
    assert payload == original


def test_multilingual_estimate_schema_envelope_and_jev_unknown_output():
    payload = packet(
        [
            {
                "ref": "human",
                "origin": "client_user_claim",
                "content": "禁止修改生产。" * 100,
            }
        ]
    )
    view = measure(
        payload, Capacity(context_window=30000, max_input_tokens=25000, status="known")
    )
    assert view["input_ceiling_tokens"] == 14000
    raw = serialized(payload)
    assert view["estimator"] == "rough_characters_div3_plus_framing"
    assert view["bytes"] == len(raw)
    assert view["characters"] == len(raw.decode("utf-8"))
    assert view["bytes"] > view["characters"]
    assert view["estimated_input_tokens"] == (
        math.ceil(view["characters"] / 3) + view["framing_tokens"]
    )
    jev = {
        "state": {"prompt": "short"},
        "questions": {"choice": {"options": ["a" * 99000]}},
    }
    with pytest.raises(ContextInsufficientError):
        fit(jev, jev=True)
    view = measure(
        jev,
        Capacity(context_window=200000, max_input_tokens=180000, status="known"),
        jev=True,
    )
    assert view["requested_output_reserve"] is None
    assert view["hidden_output_overhead"] == "unknown"


def test_discovery_cache_ttl_identity_staleness_and_capacity_invalidation(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr("intaris.input_budget.time.monotonic", lambda: clock[0])
    cache = CapacityCache()
    calls = []

    def probe(timeout):
        calls.append(timeout)
        return {
            "context_window": 200000,
            "max_input_tokens": 180000,
            "resolution_fingerprint": "revision-one",
        }

    key = ("endpoint", "route", "tenant-one")
    assert cache.resolve(key, probe).status == "known"
    cache.resolve(key, probe)
    assert len(calls) == 1 and calls[0] <= 0.2
    clock[0] += 61

    def unavailable(timeout):
        raise TimeoutError

    assert cache.resolve(key, unavailable).status == "stale"
    assert (
        cache.resolve(("endpoint", "route", "tenant-two"), unavailable).status
        == "unknown"
    )
    cache.resolve(key, probe)
    cache.invalidate()
    assert cache.resolve(key, unavailable).status == "unknown"


def test_redaction_precedes_context_excerpt_and_no_schema_keys_disappear():
    text = "x " * 300 + "AWS_SECRET_ACCESS_KEY=" + "AbCd9" * 8 + " z" * 300
    excerpt, _ = contextual_excerpt(text, keep=650)
    assert "AbCd9AbC" not in excerpt
    payload = packet([])
    output, _ = fitted(payload)
    assert output["response_format"] == payload["response_format"]


def test_real_llm_transport_discovers_capacity_counts_final_schema_and_caps_actual_output():
    import httpx
    from openai import OpenAI

    from intaris.config import LLMConfig
    from intaris.llm import LLMClient

    sent = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "routes/intaris_judge",
                    "object": "model",
                    "created": 0,
                    "owned_by": "cognis",
                    "context_window": 131072,
                    "max_input_tokens": 100000,
                    "max_output_tokens": 2048,
                },
            )
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "completion-budget",
                "object": "chat.completion",
                "created": 0,
                "model": "routes/intaris_judge",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = LLMClient(
        LLMConfig(api_key="local-test-key", model="routes/intaris_judge")
    )
    client._client.close()
    client._client = OpenAI(
        api_key="local-test-key",
        base_url="https://budget.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    try:
        messages = [{"role": "user", "content": "Read only. " * 3000}]
        schema = {
            "name": "reply",
            "strict": True,
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        }
        client.generate(
            messages, json_schema=schema, max_tokens=16000, single_attempt=True
        )
        assert len(sent) == 1
        assert sent[0]["messages"] == messages
        assert sent[0]["max_tokens"] == 2048
        assert sent[0]["response_format"]["json_schema"] == schema
        assert client.input_view["digest"] == measure(sent[0])["digest"]
        assert client.input_view["requested_output_reserve"] == 2048
    finally:
        client.close()


def test_canonical_validated_middle_span_survives_context_projection():
    from intaris.evaluation_context import source_view

    quote = "Restart staging only."
    text = (
        quote
        + " x" * 6000
        + " Not production. "
        + quote
        + " Only with approval."
        + " y" * 6000
    )
    start = text.rindex(quote)
    artifact = {
        "originals": [{"ref": "procedure", "origin": "tool_context", "content": text}],
        "items": [
            {
                "source_ref": "procedure",
                "start": start,
                "end": start + len(quote),
                "quote": quote,
            }
        ],
    }
    view = source_view(artifact)
    assert view["sources"][0]["context_spans"] == [[start, start + len(quote)]]
    payload = packet(view["sources"])
    fitted_request, diagnostics = fitted(
        payload, Capacity(max_input_tokens=6000, status="known")
    )
    content = json.loads(fitted_request["messages"][1]["content"])["raw_task_context"][
        "sources"
    ][0]
    assert "Not production. " + quote + " Only with approval." in content["content"]
    assert content["render_truncated"] is True
    assert diagnostics["source_coverage"] == "partial"


def test_actual_judge_builder_bodyless_approval_protects_entire_linked_proposal():
    from intaris.judge_simulator import request
    from tools.l1_projection_sizes import specimen

    _, state, result = specimen("inbox-inspection-only")
    proposal = (
        "before " * 10000 + " Only staging, never production. " + " after" * 10000
    )
    artifact = {
        "originals": [
            {
                "ref": "human",
                "origin": "client_user_claim",
                "content": "Yes, that exact proposal.",
                "linked_to": "proposal",
            },
            {
                "ref": "proposal",
                "origin": "linked_non_authorizing_context",
                "content": proposal,
                "linked_to": "human",
            },
        ]
    }
    question = request(
        {"tool": "bash", "args_redacted": {"command": "git fetch origin"}},
        {"policy": {"profile": "balanced"}},
        state,
        result,
        artifact,
    )
    canonical = json.loads(question["messages"][1]["content"])
    human = next(
        row for row in canonical["raw_task_context"]["sources"] if row["ref"] == "human"
    )
    assert (
        "content" not in human
    )  # Its complete body is presented in the citable section.
    payload = {
        "messages": question["messages"],
        "json_schema": question["schema"],
        "max_tokens": 16000,
    }
    partial, diagnostics = fitted(payload)
    assert diagnostics["affirmative_grants_eligible"] is False
    body = json.loads(partial["messages"][1]["content"])
    assert body["model_view_coverage"]["status"] == "partial"
    for row in body["raw_task_context"]["sources"]:
        assert row["render_truncated"] is True
        assert row["grant_eligible"] is False
        assert row["body_availability"] == "partial_model_view"
    output, view = fitted(
        payload,
        Capacity(context_window=300000, max_input_tokens=280000, status="known"),
    )
    assert output == payload
    assert not view["trimmed"]
    assert proposal in output["messages"][1]["content"]


def test_discovery_contradiction_and_stale_age_bound(monkeypatch):
    import httpx

    clock = [1.0]
    monkeypatch.setattr("intaris.input_budget.time.monotonic", lambda: clock[0])
    cache = CapacityCache()
    key = ("endpoint", "route", "owner")
    cache.resolve(key, lambda _: {"max_input_tokens": 200000})
    clock[0] = 62.0

    def unavailable(_):
        raise TimeoutError

    assert cache.resolve(key, unavailable).status == "stale"
    clock[0] = 125.0
    assert cache.resolve(key, unavailable).status == "unknown"
    cache.invalidate()
    cache.resolve(key, lambda _: {"max_input_tokens": 200000})
    clock[0] += 61

    def protocol_changed(_):
        response = httpx.Response(
            404, request=httpx.Request("GET", "https://budget.test/v1/models/route")
        )
        response.raise_for_status()

    assert cache.resolve(key, protocol_changed).status == "unknown"


def test_spans_prompt_uses_only_canonical_original_assignment():
    from intaris.intention_profile import prompt_parts

    captured = {"profile_sources": [], "originals": []}
    content = prompt_parts(
        captured,
        {
            "original_assignment": "Do not modify production.",
            "intention": "Generated summary: maintain deployment.",
        },
        [],
    )
    assert (
        '"original_assignment_context_not_authority":"Do not modify production."'
        in content
    )
    assert "Generated summary:" not in content
    missing = prompt_parts(captured, {"intention": "Generated summary"}, [])
    assert '"original_assignment_context_not_authority":null' in missing


@pytest.mark.parametrize("read_at", [120.0, 121.0])
def test_stale_cache_hit_obeys_absolute_confirmation_deadline(monkeypatch, read_at):
    clock = [0.0]
    monkeypatch.setattr("intaris.input_budget.time.monotonic", lambda: clock[0])
    cache = CapacityCache()
    key = ("endpoint", "route", "owner")
    cache.resolve(key, lambda _: {"max_input_tokens": 200000})

    def unavailable(_):
        raise TimeoutError

    clock[0] = 119.0
    assert cache.resolve(key, unavailable).status == "stale"
    assert cache._confirmed_at == 0.0
    assert cache._expires == 120.0
    clock[0] = 119.5
    assert cache.resolve(key, unavailable).status == "stale"
    clock[0] = read_at
    assert cache.resolve(key, unavailable).status == "unknown"
    assert cache._confirmed_at == 0.0


@pytest.mark.parametrize("failed_at", [120.0, 120.1])
def test_discovery_failure_crossing_stale_deadline_cannot_retain_capacity(
    monkeypatch, failed_at
):
    clock = [0.0]
    monkeypatch.setattr("intaris.input_budget.time.monotonic", lambda: clock[0])
    cache = CapacityCache()
    key = ("endpoint", "route", "owner")
    cache.resolve(key, lambda _: {"max_input_tokens": 200000})
    clock[0] = 119.9

    def unavailable(_):
        clock[0] = failed_at
        raise TimeoutError

    assert cache.resolve(key, unavailable).status == "unknown"
    assert cache._confirmed_at == 0.0


@pytest.mark.parametrize("transport", ["llm", "jev"])
def test_managed_capacity_rejection_invalidates_both_transports(transport):
    import httpx
    from openai import OpenAI

    from intaris.config import JevConfig, LLMConfig
    from intaris.jev import JevClient
    from intaris.llm import LLMClient

    gets = []

    def handle(request):
        if request.method == "GET":
            gets.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "route",
                    "object": "model",
                    "created": 0,
                    "owned_by": "cognis",
                    "max_input_tokens": 100000,
                },
            )
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "upstream_context_length_exceeded",
                    "message": "capacity changed",
                }
            },
        )

    if transport == "llm":
        client = LLMClient(LLMConfig(api_key="local-test-key", model="route"))
        client._client.close()
        client._client = OpenAI(
            api_key="local-test-key",
            base_url="https://budget.test/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )

        def invoke():
            return client.generate(
                [{"role": "user", "content": "read only"}], single_attempt=True
            )
    else:
        client = JevClient(
            JevConfig(
                enabled=True,
                model="route",
                base_url="https://budget.test",
                api_key="local-test-key",
            ),
            transport=httpx.MockTransport(handle),
        )

        def invoke():
            return client._request(state={"prompt": "read only"}, questions={})

    try:
        with pytest.raises(Exception):
            invoke()
        assert len(gets) == 1
        assert client.input_capacity().status == "known"
        assert len(gets) == 2
    finally:
        client.close()


def test_real_jev_transport_counts_questions_and_uses_no_invented_output_cap():
    import httpx

    from intaris.config import JevConfig
    from intaris.jev import JevClient

    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": "budget-model", "max_input_tokens": 3000}
            )
        return httpx.Response(200, json={"answers": {}})

    client = JevClient(
        JevConfig(
            enabled=True,
            model="budget-model",
            base_url="https://budget.test",
            api_key="local-test-key",
        ),
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(ContextInsufficientError) as failure:
            client._request(
                state={"prompt": "short"},
                questions={"large": {"criteria": {"true": "界" * 16000}}},
            )
        assert [r.method for r in requests] == ["GET"]
        assert failure.value.diagnostics["requested_output_reserve"] is None
        assert failure.value.diagnostics["estimated_input_tokens"] > 4000
    finally:
        client.close()
