"""Executed scripts successfully written earlier in the session are shown."""

import pytest

from intaris.evaluation_context import runtime_view
from intaris.session_scripts import (
    MAX_SCRIPT_CHARS,
    attach,
    executed_paths,
    session_script,
)

SCRIPT = "import shutil\nshutil.rmtree('/workspace/build')\n"


class Events:
    """Minimal canonical event store: recorded tool calls and results."""

    def __init__(self):
        self.events = []

    def call(self, call_id, name, arguments, status="completed", is_error=False):
        self.events.append(
            {
                "type": "tool_call",
                "data": {"call_id": call_id, "name": name, "arguments": arguments},
            }
        )
        if status is not None:
            self.events.append(
                {
                    "type": "tool_result",
                    "data": {
                        "call_id": call_id,
                        "execution_outcome": {"status": status, "is_error": is_error},
                    },
                }
            )

    def read_tail(self, user_id, session_id, limit, event_types=None):
        return [e for e in self.events if e["type"] in event_types][-limit:]


def run(events, command="python3 /tmp/a.py"):
    return session_script(events, "u", "s", "bash", {"command": command})


@pytest.mark.parametrize(
    "command,workdir,expected",
    [
        ("python3 /tmp/a.py", None, ["/tmp/a.py"]),
        ("python3 -u /tmp/a.py --flag", None, ["/tmp/a.py"]),
        ("uv run --no-project python /tmp/a.py", None, ["/tmp/a.py"]),
        ("X=1 timeout 30 bash scripts/run.sh", "/repo", ["/repo/scripts/run.sh"]),
        ("/tmp/venv/bin/python /tmp/b.py 2>&1 | tail -5", None, ["/tmp/b.py"]),
        ("cd x && ./tool.sh", None, []),  # relative without workdir: unknown
        ("python3 -c 'print(1)'", None, []),
        ("python3 - <<'EOF'\nprint(1)\nEOF", None, []),
        ("git status", None, []),
    ],
)
def test_executed_paths(command, workdir, expected):
    assert executed_paths(command, workdir) == expected


def test_successful_full_write_is_shown_for_an_executed_script():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    script = run(events)
    assert script["content"] == SCRIPT
    assert script["path"] == "/tmp/a.py"
    assert script["source_call_id"] == "c1"
    assert script["authority"] == "none"
    assert not script["modified_after_write"] and not script["truncated"]


@pytest.mark.parametrize(
    "status,is_error",
    [
        (None, False),  # proposed or pending: no recorded result
        ("denied", False),
        ("failed", True),
        ("completed", True),
    ],
)
def test_unexecuted_or_failed_writes_never_provide_script_text(status, is_error):
    events = Events()
    events.call(
        "c1",
        "write",
        {"file_path": "/tmp/a.py", "content": "print('harmless')"},
        status=status,
        is_error=is_error,
    )
    assert run(events) is None


def test_latest_successful_write_wins_over_a_later_unexecuted_proposal():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    events.call(
        "c2", "write", {"file_path": "/tmp/a.py", "content": "print(1)"}, status=None
    )
    assert run(events)["content"] == SCRIPT


def test_added_patch_file_is_reconstructed():
    events = Events()
    patch = (
        "*** Begin Patch\n*** Add File: /tmp/c.py\n+print('a')\n+print('b')\n"
        "*** End Patch\n"
    )
    events.call("c1", "apply_patch", {"patchText": patch})
    assert run(events, "python3 /tmp/c.py")["content"] == "print('a')\nprint('b')"


def test_later_partial_edit_is_flagged():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    events.call(
        "c2",
        "edit",
        {"file_path": "/tmp/a.py", "old_string": "build", "new_string": "dist"},
    )
    script = run(events)
    assert script["modified_after_write"] is True
    assert script["content"] == SCRIPT


def test_unwritten_file_or_missing_event_store_stays_unknown():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/other.py", "content": SCRIPT})
    assert run(events) is None
    assert run(None) is None


def test_large_script_is_bounded():
    events = Events()
    events.call(
        "c1", "write", {"file_path": "/tmp/big.py", "content": "x = 1\n" * 5000}
    )
    script = run(events, "python3 /tmp/big.py")
    assert script["truncated"]
    assert len(script["content"]) <= MAX_SCRIPT_CHARS + 200
    assert "OMITTED MIDDLE" in script["content"]


def test_client_cannot_supply_script_text_and_it_reaches_runtime_view():
    events = Events()
    events.call("c1", "write", {"file_path": "/tmp/a.py", "content": SCRIPT})
    forged = {"executed_script": {"content": "print('harmless')"}}
    attach(forged, events, "u", "s", "bash", {"command": "git status"})
    assert "executed_script" not in forged
    context = {}
    attach(context, events, "u", "s", "bash", {"command": "python3 /tmp/a.py"})
    assert runtime_view(context)["executed_script"]["content"] == SCRIPT
