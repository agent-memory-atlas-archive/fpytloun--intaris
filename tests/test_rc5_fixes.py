"""Anonymized RC5 regressions reconstructed from observed failure classes."""

import pytest

from intaris import read_only
from intaris.classifier import verified_read_only_bash
from intaris.effect_scope import external_action
from intaris.hybrid import evaluate_policy
from intaris.hybrid_runtime import _observe_action, finalize_result
from intaris.policy_assessment import L1_DEFINITIONS, system_prompt
from intaris.policy_evidence import from_observations

# Plan-mode reads denied as unproven, reconstructed with synthetic targets.
GIT_STATUS = (
    "git status --short; git branch --show-current; "
    "git rev-parse HEAD origin/main; git remote -v"
)
JOURNAL = (
    "journalctl --user -u document-sync.service --no-pager -o short-iso "
    "--grep='Bisync successful|Bisync critical error|timed out|Killing' -n 75 "
    "--reverse; /usr/bin/rclone version; df -h /home/tester; df -i /home/tester; "
    "stat -c '%A %U:%G %y %s %n' /home/tester/.local/state/document-sync/workspace; "
    "pgrep -x rclone; systemctl --user list-timers --all --no-pager"
)
# kubectl exec of Python in a synthetic pod (abridged).
POD_EXEC = (
    "kubectl exec -i -n example-app api-0 -c api -- python - <<'PY'\n"
    "print('sidebar')\nPY"
)
PLAN = {"chat_mode": "plan", "chat_mode_source": None, "read_only_required": True}


@pytest.mark.parametrize("command", [GIT_STATUS, JOURNAL])
def test_routine_reads_are_static_read_only(command):
    assert verified_read_only_bash({"command": command})
    assert read_only.observe("bash", {"command": command}) == "read_only"


@pytest.mark.parametrize(
    "command",
    [
        "git branch feature",
        "git branch -D main",
        "journalctl --vacuum-size=1M",
        "journalctl --rotate",
        "journalctl --vacuum-s=1M",
        "journalctl --rot",
        "systemctl --output cat restart nginx.service",
        "systemctl --user -o json restart x",
        "systemctl --user restart document-sync.service",
        "rclone sync a b",
        "/usr/bin/rm -rf /tmp/x",
        "/usr/bin/../bin/stat x",
        POD_EXEC,
    ],
)
def test_mutating_or_unproven_commands_stay_unproven(command):
    assert not verified_read_only_bash({"command": command})


def plan_state(command, **assessment):
    facts = _observe_action("bash", {"command": command}, policy={})
    state = from_observations(facts, read_only=True)
    return state.model_copy(update={"assessment": assessment})


def finalized(state, profile="balanced"):
    result = evaluate_policy([], state, profile, assessed=True)
    return finalize_result(
        result,
        maximum=None,
        minimum=None,
        overlay=PLAN,
        mutation=state.value("mutation", "unclear"),
    )


def test_static_read_passes_the_plan_ceiling():
    state = plan_state(GIT_STATUS, relationship="supports_intent", risk="low")
    assert finalized(state).decision == "approve"


def test_l1_low_risk_read_witness_passes_the_plan_ceiling():
    # The reconstructed L1 response is effects=[read], risk=low.
    state = plan_state(
        POD_EXEC,
        relationship="supports_intent",
        risk="low",
        mutation="unclear",
        effects=["read"],
        target_scope="consistent",
        contextual_restrictions="consistent",
    )
    assert state.value("mutation") == "read_only"
    assert finalized(state).decision == "approve"


@pytest.mark.parametrize(
    "assessment",
    [
        {"effects": ["read"], "risk": "medium"},
        {"effects": ["unknown"], "risk": "low"},
        {"effects": [], "risk": "low"},
        {"risk": "unknown"},
    ],
)
def test_pod_exec_without_read_witness_stays_denied(assessment):
    state = plan_state(
        POD_EXEC, relationship="supports_intent", mutation="unclear", **assessment
    )
    final = finalized(state)
    assert final.decision == "deny"
    assert final.override == "request.read_only_unproven"


@pytest.mark.parametrize(
    "effects,text",
    [
        (["write"], "would make changes"),
        (["delete"], "would delete data"),
        (["transmit"], "would send data or act outside your environment"),
    ],
)
def test_plan_denies_effects_with_specific_explanation(effects, text):
    state = plan_state(
        POD_EXEC, relationship="supports_intent", risk="low", effects=effects
    )
    final = finalized(state)
    assert final.decision == "deny"
    explanation = read_only.explanation(
        final.decision, PLAN, state.value("mutation"), state.value("effects")
    )
    assert text in explanation


def test_l1_definitions_reach_every_projection():
    for question in (
        {"schema": {}, "projection": "lines"},
        {"schema": {}},
    ):
        prompt = system_prompt({**question, "judge": True})
        assert L1_DEFINITIONS in prompt
    assert "local commits, worktrees" in L1_DEFINITIONS
    assert "port-forwards" in L1_DEFINITIONS


@pytest.mark.parametrize(
    "command,external",
    [
        # A local throwaway Postgres container.
        (
            "docker run --detach --rm --name intaris-hybrid-acceptance "
            "-p 127.0.0.1::5432 postgres:17-alpine",
            False,
        ),
        # A local port-forward.
        ("kubectl port-forward -n monitoring svc/alertmanager 29093:9093", False),
        ("rm -rf /tmp/build", True),
        ("git push origin main", True),
    ],
)
def test_external_action_is_narrow(command, external):
    assert external_action("bash", {"command": command}) is external


def test_l1_read_execute_witness_passes_the_plan_ceiling():
    # Read-only Python analysis classified as read + execute.
    state = plan_state(
        "python -B - <<'PY'\nprint(1)\nPY",
        relationship="supports_intent",
        risk="low",
        effects=["execute", "read"],
        target_scope="consistent",
        contextual_restrictions="consistent",
    )
    assert state.value("mutation") == "read_only"
    assert finalized(state).decision == "approve"


@pytest.mark.parametrize("effects", [["read"], ["read", "execute"]])
def test_low_risk_read_witness_satisfies_ceiling_without_permission_proof(effects):
    state = plan_state(
        POD_EXEC,
        relationship="supports_intent",
        risk="low",
        mutation="unclear",
        effects=effects,
    )
    assert state.value("mutation") == "read_only"
    final = finalized(state)
    assert final.decision == "approve"
    assert final.override != "request.read_only_unproven"


def test_l1_read_witness_outside_plan_does_not_lift_explicit_risk():
    # The witness only sets mutation; a high-risk or production read still
    # follows the profile table and is not treated as low-risk.
    state = plan_state(POD_EXEC, risk="high", effects=["read"]).model_copy(
        update={"read_only_required": False}
    )
    assert state.value("mutation") != "read_only"
