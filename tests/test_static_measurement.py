"""Boundary regressions and offline preparation contracts."""

import json
import os
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from intaris.classifier import observe_hybrid, verified_read_only_bash
from tests.test_hybrid_races import race_store as race_store
from tools.paid_write_replay import run, sample
from tools.static_comparison import compare


@pytest.mark.parametrize("tool", ["read", "grep", "glob"])
def test_allowed_sibling_read_is_static_without_assessment(race_store, tool):
    sessions, audit, llm, evaluator, sid = race_store
    sessions.update_session(
        sid, user_id="race-owner", policy={"allow_paths": ["/sibling/**"]}
    )
    args = (
        {"file_path": "/sibling/README"}
        if tool == "read"
        else {"path": "/sibling", "pattern": "README"}
    )
    if tool != "read":
        args["path"] = "/sibling/src"
    llm.generate.side_effect = AssertionError("Allowed read must be static")
    result = evaluator.evaluate(
        user_id="race-owner",
        session_id=sid,
        agent_id=None,
        tool=tool,
        args=args,
        context={"working_directory": "/repo"},
    )
    assert result["decision"] == "approve"
    assert (
        result["evaluation_metadata"]["policy_result"]["permission_basis"]
        == "read_only_static"
    )
    llm.generate.assert_not_called()


def test_allowed_path_does_not_override_explicit_deny_or_secrets():
    for path in ("/sibling/private", "/sibling/.env"):
        facts = observe_hybrid(
            "read",
            {"file_path": path},
            working_directory="/repo",
            policy={"allow_paths": ["/sibling/**"], "deny_paths": ["/sibling/private"]},
        )
        assert facts["configured_deny"] or not facts["static_read"]


@pytest.mark.parametrize(
    "command",
    [
        "ps aux",
        "du -sh /repo",
        "df -h",
        "uname -a",
        "whoami",
        "id",
        "uptime",
        "echo ready",
    ],
)
def test_observational_shell_primitives(command):
    assert verified_read_only_bash({"command": command})


@pytest.mark.parametrize(
    "command", ["ps aux > file", "du /repo; rm file", "echo $(sh x)", "ps | sh"]
)
def test_observation_does_not_bypass_shell_security(command):
    assert not verified_read_only_bash({"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "git status --short --ignored --untracked-files=all",
        "git status --porcelain=v1",
        "git rev-list --count --left-right HEAD...origin/main",
        "git ls-tree -r --name-only HEAD",
        "git log --format=%H -1",
        "git log --decorate --oneline",
    ],
)
def test_read_only_git_presentation_options(command):
    assert verified_read_only_bash({"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "git diff --out=README",
        "git log --output=file",
        "git -c core.pager=sh log",
        "git log --exec=sh",
    ],
)
def test_presentation_options_do_not_authorize_output_or_helpers(command):
    assert not verified_read_only_bash({"command": command})


@pytest.mark.parametrize("command", ["ps auxe", "ps eww", "ps -o environ"])
def test_process_environments_cannot_be_fast_approved(race_store, command):
    _, _, _, evaluator, sid = race_store
    result = evaluator.evaluate(
        user_id="race-owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert not verified_read_only_bash({"command": command})
    assert (
        result["evaluation_metadata"]["policy_result"]["permission_basis"]
        != "read_only_static"
    )


@pytest.mark.parametrize(
    "command",
    [
        "ps aux",
        "du -sh /repo",
        "df -h",
        "uname -a",
        "whoami",
        "id",
        "uptime",
        "echo ready",
    ],
)
@pytest.mark.parametrize("denied", [False, True])
def test_new_shell_reads_through_evaluator(race_store, command, denied):
    sessions, _, llm, evaluator, sid = race_store
    if denied:
        sessions.update_session(
            sid, user_id="race-owner", policy={"deny_commands": [command]}
        )
    result = evaluator.evaluate(
        user_id="race-owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert result["decision"] == ("deny" if denied else "approve")
    llm.generate.assert_not_called()


@pytest.mark.parametrize(
    "tool,denied",
    [
        ("knowledgebase_list_artifacts", False),
        ("knowledgebase_list_artifacts", True),
        ("knowledgebase_list_artifacts_and_delete", False),
    ],
)
def test_exact_knowledgebase_observation(race_store, tool, denied):
    sessions, _, llm, evaluator, sid = race_store
    if denied:
        sessions.update_session(
            sid, user_id="race-owner", policy={"deny_tools": [tool]}
        )
    result = evaluator.evaluate(
        user_id="race-owner", session_id=sid, agent_id=None, tool=tool, args={}
    )
    if tool.endswith("_delete"):
        assert (
            result["evaluation_metadata"]["policy_result"]["permission_basis"]
            != "read_only_static"
        )
    else:
        assert result["decision"] == ("deny" if denied else "approve")
        llm.generate.assert_not_called()


def test_sampling_stratifies_without_inference():
    rows = [
        {
            "call_id": f"call-{n}",
            "tool": tool,
            "decision": "approve",
            "hybrid": False,
            "evaluation_path": "llm",
        }
        for n, tool in enumerate(
            ["bash"] * 200 + ["edit"] * 200 + ["mcp_mail__send_message"] * 5
        )
    ]
    chosen = sample(rows)
    assert len(chosen) == len({r["call_id"] for r in chosen}) == 300
    assert sum(r["stratum"] == "mcp_writes" for r in chosen) == 5
    assert chosen == sample(rows)


def test_paid_execution_is_opt_in(tmp_path):
    with pytest.raises(ValueError, match="--approve-paid"):
        run(Namespace(approve_paid=False, output=tmp_path))


def test_estimate_uses_measured_actions_and_session_profile_amortization():
    from tools.paid_write_replay import estimate_cost

    rows = [
        {
            "call_id": f"call-{n}",
            "session_id": "shared-session",
            "tool": "write",
            "args": {"path": "/repo/file"},
            "intention": "Update file",
            "user_messages": [],
        }
        for n in range(3)
    ]
    estimate = estimate_cost(rows)
    assert estimate["actions_including_judge_usd"] == 0.001236
    assert estimate["original_sessions"] == 1
    assert estimate["isolated_runner_profile_calls"] == 3
    assert estimate["session_amortized_estimate_usd"] < estimate["estimated_usd"]
    assert estimate["upper_bound_usd"] > estimate["estimated_usd"]
    assert estimate["action_context_tokens"]["mean"] < 100
    assert estimate["paid_calls_run"] == 0


def test_paid_runner_wiring_offline(tmp_path, monkeypatch):
    from intaris import intention, llm
    from tools.rc3_replay import private_write

    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(tmp_path / "not-read"))
    mocked = Mock()
    from tools.l1_projection_sizes import valid_answer

    mocked.generate.side_effect = lambda *args, **kwargs: json.dumps(
        valid_answer(kwargs["json_schema"]["schema"])
    )
    monkeypatch.setattr(llm, "LLMClient", Mock(return_value=mocked))
    generator = Mock(spec=intention.generate_intention)
    monkeypatch.setattr(intention, "generate_intention", generator)
    private_write(
        tmp_path / "paid-input.json",
        [
            {
                "call_id": "local-paid-test",
                "tool": "write",
                "args": {"file_path": "/repo/file"},
                "policy": {},
                "details": {},
                "intention": "Inspect project",
                "user_messages": [
                    {
                        "content": "User message: Inspect project.",
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ],
    )
    run(Namespace(approve_paid=True, output=tmp_path))
    report = json.loads((tmp_path / "paid-report.json").read_text())
    assert report["legacy_approved"] == 1
    assert report["counts"].get("error", 0) == 0, (
        tmp_path / "paid-results.json"
    ).read_text()
    generator.assert_called_once()


def test_exact_legacy_worker_no_provider():
    configured = os.getenv("INTARIS_STATIC_BASELINE_PATH")
    if not configured:
        pytest.skip("Explicit qualified static baseline path required")
    baseline = Path(configured)
    if not baseline.exists():
        pytest.skip("Exact local baseline worktree not installed")
    rows = [
        {
            "call_id": "local-comparison",
            "session_id": "local-session",
            "tool": "read",
            "args": {
                "file_path": "/sibling/file",
                "_intaris_context": {"working_directory": "/repo"},
            },
            "policy": {"allow_paths": ["/sibling/**"]},
            "metadata": {},
            "details": {},
            "minimum_outcome": None,
            "decision": "approve",
            "hybrid": False,
        }
    ]
    _, report = compare(rows, baseline)
    assert report["legacy_static"] == report["rc3_static"] == 1
