import json
import stat
from unittest.mock import Mock

import pytest

from tools.rc3_replay import prepare, private_write, replay_psql_command, summarize

DB_SETTINGS = {
    "INTARIS_REPLAY_DB_NAMESPACE": "example-data",
    "INTARIS_REPLAY_DB_TARGET": "deployment/replay-store",
    "INTARIS_REPLAY_DB_CONTAINER": "database",
    "INTARIS_REPLAY_DB_USER": "reader",
    "INTARIS_REPLAY_DB_NAME": "example_audit",
}


def test_replay_database_connection_is_explicit(monkeypatch):
    for key, value in DB_SETTINGS.items():
        monkeypatch.setenv(key, value)
    assert replay_psql_command() == [
        "kubectl",
        "-n",
        "example-data",
        "exec",
        "-i",
        "deployment/replay-store",
        "-c",
        "database",
        "--",
        "psql",
        "-U",
        "reader",
        "-d",
        "example_audit",
        "-X",
        "-qAt",
        "-v",
        "ON_ERROR_STOP=1",
    ]


@pytest.mark.parametrize("missing", DB_SETTINGS)
def test_replay_database_requires_configuration_before_query(monkeypatch, missing):
    from tools.static_comparison import query

    for key, value in DB_SETTINGS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing)
    launch = Mock()
    monkeypatch.setattr("tools.static_comparison.subprocess.run", launch)
    with pytest.raises(KeyError, match=missing):
        query("BEGIN READ ONLY; SELECT 1; COMMIT;")
    launch.assert_not_called()


def row(tool, args):
    return {
        "call_id": "local-call",
        "session_id": "local-session",
        "tool": tool,
        "args": args,
        "policy": {"deny_policies": ["Do not send email."]},
        "metadata": {},
        "details": {},
        "decision": "approve",
        "minimum_outcome": None,
        "agent": "test",
        "unattended": True,
        "had_user_message": True,
        "hybrid": False,
    }


def test_export_keeps_no_argument_content_and_counts_only_static(tmp_path):
    rows = [
        prepare(
            row("read", {"file_path": "/repo/public.txt", "content": "never export"})
        ),
        prepare(
            row("write", {"file_path": "/repo/public.txt", "content": "private body"})
        ),
    ]
    assert "never export" not in json.dumps(rows)
    assert "private body" not in json.dumps(rows)
    assert "/repo/public.txt" not in json.dumps(rows)
    assert rows[0]["fast_approved"]
    assert not rows[1]["fast_approved"]
    assert rows[1]["fake_inference_calls"] == 1
    assert summarize(rows)["legacy_approved_fast_percent"] == 50
    target = tmp_path / "export.json"
    private_write(target, rows)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_nested_keys_cannot_export_argument_content():
    secret = "private employee name and confidential filename"
    source = row(
        "read",
        {
            "file_path": "/repo/public.txt",
            secret: {"headers": {secret: secret}, "data": [{secret: 1}]},
        },
    )
    exported = prepare(source)
    assert secret not in json.dumps(exported)
    assert "headers" in json.dumps(exported["args_shape"])
    assert "other_values" in json.dumps(exported["args_shape"])
