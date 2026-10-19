"""RC5d: policy round-trip, plan-mode diagnostics, bounded external drift."""

from __future__ import annotations

import pytest

from intaris.classifier import verified_read_only_bash
from intaris.hybrid import evaluate_policy
from intaris.judge_simulator import SYSTEM
from intaris.policy_activation import prepare_policy
from intaris.read_only import observe
from tests.test_rc5c_fixes import _assessed


def test_policy_written_back_with_server_snapshot_fields_is_accepted():
    """Cognis /yolo writes back the policy it read, including server fields."""
    policy = {
        "evaluation_policy_version": "hybrid_v1",
        "profile": "balanced",
        "maximum_outcome": "approve",
    }
    first = prepare_policy(dict(policy), tenant="u")
    echoed = {
        **policy,
        "active_policy": first["active_policy"],
        "policy_hash": first["policy_hash"],
    }
    second = prepare_policy(echoed, tenant="u")
    assert second["policy_hash"] == first["policy_hash"]


@pytest.mark.parametrize(
    "command",
    [
        "kubectl -n example-app logs api-0 --since=30m 2>/dev/null | grep -iE 'x|y' | tail -5",
        "kubectl get pods -A -o wide",
        "kubectl -n x describe pod a 2>&1 | head",
        "kubectl top pods -n example-app",
        "kubectl auth can-i get pods",
        "cat a >/dev/null",
        "grep x f >> /dev/null",
        "echo hi &>/dev/null",
    ],
)
def test_plan_mode_diagnostics_are_read_only(command):
    args = {"command": command}
    assert verified_read_only_bash(args)
    assert observe("bash", args) == "read_only"


@pytest.mark.parametrize(
    "command",
    [
        "kubectl get secret foo -o yaml",
        "kubectl describe secrets",
        "kubectl exec a -- ls",
        "kubectl -n logging port-forward svc/loki 3100:80",
        "kubectl delete pod a",
        "kubectl apply -f x.yml",
        "kubectl auth reconcile -f x.yml",
        "kubectl --cache-dir get delete pod api-0",
        "kubectl -s https://x get delete pod a",
        "kubectl cluster-info dump --output-directory=diagnostics",
    ],
)
def test_kubectl_changes_secrets_and_exec_are_not_static_reads(command):
    assert not verified_read_only_bash({"command": command})


@pytest.mark.parametrize(
    "command", ["cat a > out.txt", "cat a 2>/dev/nullx", "grep x f >> out"]
)
def test_real_redirects_still_mutate(command):
    args = {"command": command}
    assert not verified_read_only_bash(args)
    assert observe("bash", args) == "mutating"


@pytest.mark.parametrize("authorization", ["implicit", "absent", "unclear"])
def test_bounded_aligned_external_needs_no_per_action_grant(authorization):
    state = _assessed(authorization=authorization, risk="medium")
    result = evaluate_policy(
        [],
        state,
        "bounded_autonomy",
        assessed=True,
    )
    assert result.outcome == "approve"
    assert not result.outstanding_obligations


def test_bounded_external_with_explicit_authorization_approves():
    state = _assessed(risk="medium")
    result = evaluate_policy([], state, "bounded_autonomy", assessed=True)
    assert result.outcome == "approve"
    assert not result.outstanding_obligations


def test_common_judge_contract_guards_target_and_actual_instructions():
    assert "target compatibility" in SYSTEM
    assert "actual instruction" in SYSTEM
