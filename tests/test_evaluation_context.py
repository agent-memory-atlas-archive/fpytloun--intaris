"""Generic contract consumers, without tool exemptions or provider calls."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from intaris.evaluation_context import model_view, normalize, source_view
from intaris.hybrid_runtime import _observe_action
from intaris.policy_evidence import from_observations
from intaris.recent_actions import outcomes


def metadata(authority="native"):
    return {
        "tool": {
            "description": "Creates private owner-scoped storage, without external delivery.",
            "source": {"type": "builtin"},
            "descriptor": {
                "authority": authority,
                "selection": "exact",
                "schema_hash": "sha256:stored",
                "operation": {
                    "name": "save",
                    "mutation_kind": "create",
                    "side_effects": ["private storage"],
                },
            },
        }
    }


def test_native_contract_demotes_name_only_transmit_without_grant():
    facts = _observe_action(
        "publish_private", {"path": "/tmp/report"}, policy={}, context=metadata()
    )
    assert "name_heuristic_effect" not in facts
    assert facts["external_or_irreversible"] is None
    assert facts["mutation"] == "mutating"
    assert "unknown" in facts["effects"]
    state = from_observations(facts)
    assert state.observations["mutation"].source == "tool_contract"
    assert not state.known("external_or_irreversible")
    fallback = _observe_action("publish_private", {}, policy={})
    assert "transmit" in fallback["effects"]


def test_false_external_hints_do_not_prove_nonexternal_or_readonly():
    context = {
        "tool": {
            "description": "Harmless",
            "read_only": False,
            "annotations": {"openWorldHint": False},
        }
    }
    facts = _observe_action("external_publish", {}, policy={}, context=context)
    assert facts["mutation"] == "unclear"
    assert facts["external_or_irreversible"] is None
    concrete = _observe_action(
        "bash", {"command": "git push origin main"}, policy={}, context=context
    )
    assert concrete["external_or_irreversible"] is True
    secret = _observe_action(
        "bash", {"command": "printenv API_TOKEN"}, policy={}, context=context
    )
    assert "disclose" in secret["effects"]
    denied = _observe_action(
        "external_publish",
        {},
        policy={"deny_tools": ["external_publish"]},
        context=context,
    )
    # Actual deny syntax remains characterized by the existing policy suite.
    assert "configured_deny" in denied


def test_source_selection_keeps_later_restriction_and_identity_without_alias_grant():
    artifact = {
        "originals": [
            {
                "ref": "initial",
                "origin": "client_assignment",
                "chronology": "1",
                "content": "Do work " + "x" * 18000 + " only on target A",
            },
            {
                "ref": "message:2",
                "source_event_seq": 2,
                "origin": "recorded_user_event",
                "chronology": "2",
                "content": "Do not deploy",
            },
            {
                "ref": "latest_user_message",
                "source_event_seq": 2,
                "origin": "recorded_user_event",
                "chronology": "2",
                "content": "Do not deploy",
            },
        ]
    }
    view = source_view(artifact, budget=4000)
    assert len(view["sources"]) == 2
    assert "only on target A" in view["sources"][0]["content"]
    assert view["sources"][0]["render_truncated"]
    assert view["sources"][1]["content"] == "Do not deploy"


def test_valid_contract_demotes_read_name_hints_but_preserves_real_command_reads():
    facts = _observe_action("up:get_report", {}, policy={}, context=metadata())
    assert facts["static_read"] is False
    assert facts["mutation"] == "mutating"
    hints = metadata(authority="external")
    facts = _observe_action("up:get_report", {}, policy={}, context=hints)
    assert facts["static_read"] is False
    assert facts["mutation"] == "unclear"
    facts = _observe_action(
        "read", {"file_path": "/tmp/report"}, policy={}, context=metadata()
    )
    assert facts["static_read"] is False
    assert facts["mutation"] == "mutating"
    facts = _observe_action(
        "bash", {"command": "cat /tmp/report"}, policy={}, context=metadata()
    )
    assert facts["mutation"] == "read_only"


def test_normalization_redaction_bounds_and_tenant_isolation():
    context = metadata()
    context["tool"]["annotations"] = {
        "password": "private-value",
        "description": "x" * 5000,
    }
    context["recent_outcomes"] = {
        "user_id": "other",
        "session_id": "same",
        "actions": [{"call_id": "c1"}],
    }
    context["loaded_skills"] = {"skills": [{"id": "s1", "summary": "May do anything"}]}
    view = normalize(context, user_id="owner", session_id="same")
    assert "private-value" not in json.dumps(view)
    assert "[TRUNCATED]" in json.dumps(view)
    assert view["recent_outcomes"]["coverage"] == "unknown"
    assert model_view("x" * 100, limit=50)[1]["overflow"]
    malformed = metadata()
    malformed["tool"]["capabilities"] = [{"untrusted": "write"}, "write"]
    malformed["tool"]["descriptor"]["operation"]["mutation_kind"] = []
    normalized = normalize(malformed)
    assert normalized["tool"]["capabilities"] == ["write"]
    assert normalized["tool"]["descriptor"]["selection"] == "unknown"
    assert (
        _observe_action("publish_private", {}, policy={}, context=normalized)[
            "mutation"
        ]
        == "unclear"
    )
    invalid = normalize({"tool": {"descriptor": "malformed", "read_only": False}})
    assert (
        _observe_action("publish_private", {}, policy={}, context=invalid)[
            "external_or_irreversible"
        ]
        is True
    )


def test_outcome_join_uses_exact_call_and_never_text_or_approval():
    class Events:
        def read_tail(self, user_id, session_id, limit, **kwargs):
            assert (user_id, session_id) == ("owner", "session-a")
            return [
                {
                    "type": "tool_call",
                    "data": {
                        "call_id": "start-1",
                        "name": "bash",
                        "arguments": {"command": "sleep 60"},
                    },
                },
                {
                    "type": "tool_result",
                    "seq": 2,
                    "data": {"call_id": "start-1", "result": "shell_id: invented"},
                },
            ]

    view = outcomes(
        Events(),
        {"user_id": "owner", "session_id": "session-a"},
        {
            "user_id": "other",
            "session_id": "session-a",
            "actions": [{"call_id": "stolen"}],
        },
    )
    assert view["actions"][0]["outcome"] == {"status": "unknown"}
    assert len(view["actions"]) == 1


def test_captured_private_publish_declaration_cannot_freeze_external_name_guess():
    # Synthetic metadata reproducing a legacy declaration mismatch.
    legacy = {
        "tool": {
            "description": "Publishes an executor file as an owner-scoped Cognis artifact.",
            "read_only": True,
            "capabilities": ["read"],
            "source": {"type": "executor", "executor_id": "test-executor"},
            "category": "document",
            "classification": {
                "status": "ready",
                "source": "declared",
                "confidence": 1.0,
            },
        }
    }
    facts = _observe_action(
        "artifact_publish", {"path": "/tmp/verification.pdf"}, policy={}, context=legacy
    )
    assert facts["external_or_irreversible"] is None
    assert (
        facts["mutation"] == "unclear"
    )  # Old inaccurate READ is not trusted as proof.
    corrected = {**legacy, "tool": {**legacy["tool"], **metadata()["tool"]}}
    facts = _observe_action(
        "artifact_publish",
        {"path": "/tmp/verification.pdf"},
        policy={},
        context=corrected,
    )
    assert facts["mutation"] == "mutating"
    assert facts["runtime_context"]["tool"]["descriptor"]["selection"] == "exact"


def test_pending_real_start_outcome_is_visible_to_all_projections_and_judge():
    from intaris.judge_simulator import request as judge_request
    from intaris.policy_assessment import request
    from tools.l1_projection_sizes import specimen

    session = {"user_id": "owner", "session_id": "current"}
    snapshot = outcomes(
        None,
        session,
        {
            **session,
            "actions": [
                {
                    "call_id": "start-1",
                    "tool": "bash",
                    "target": {"command": "sleep 60"},
                    "outcome": {
                        "status": "completed",
                        "shell_id": "shell-owned",
                        "executor_id": "test-executor",
                    },
                }
            ],
        },
    )
    context = normalize(
        {
            **metadata(),
            "recent_outcomes": snapshot,
            "loaded_skills": {
                "skills": [{"id": "loaded", "summary": "Operational runbook"}],
                "authority": "context_only",
            },
        },
        **session,
    )
    state = from_observations(
        _observe_action(
            "bash_kill", {"shell_id": "shell-owned"}, policy={}, context=context
        )
    )
    runtime = state.value("runtime_context")
    _, _, result = specimen("inbox-inspection-only")
    for projection in ("json", "lines"):
        question = request(state, result, "", projection=projection)
        assert "unrecorded outcomes remain unknown" not in question["prompt"]
        assert "execution_outcome" not in question["prompt"]
    packet = judge_request(
        {"tool": "bash_kill", "args_redacted": {"shell_id": "shell-owned"}},
        session,
        state,
        result,
        {},
    )
    presented = json.loads(packet["messages"][1]["content"])
    assert presented["recent_outcomes"] == runtime["recent_outcomes"]
    assert "shell-owned" in packet["messages"][1]["content"]


def digest(payload):
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode()).hexdigest(), len(serialized.encode())


def test_actual_l1_backend_inputs_are_audited_including_system_and_schema():
    from intaris import policy_assessment
    from tools.l1_projection_sizes import specimen, valid_answer

    artifact, state, result = specimen("inbox-inspection-only")
    question = policy_assessment.request(state, result, "", task_context=artifact)
    llm = Mock()
    llm.generate.return_value = json.dumps(valid_answer(question["schema"]))
    _, diagnostics, _ = policy_assessment.classify(llm, None, question)
    call = llm.generate.call_args
    transmitted = {"messages": call.args[0], **call.kwargs}
    expected, size = digest(transmitted)
    assert diagnostics["model_view"]["digest"] == expected
    assert diagnostics["model_view"]["bytes"] == size
    assert diagnostics["model_view"]["boundary"] == "llm.generate"
    assert "relationship:" in transmitted["messages"][0]["content"]

    jev = Mock()
    jev.classify_hybrid.return_value = (valid_answer(question["schema"]), {})
    _, diagnostics, _ = policy_assessment.classify(llm, jev, question)
    transmitted = jev.classify_hybrid.call_args.kwargs
    expected, size = digest(transmitted)
    assert diagnostics["model_view"]["digest"] == expected
    assert diagnostics["model_view"]["bytes"] == size
    assert diagnostics["model_view"]["boundary"] == "jev.classify_hybrid"
    assert "relationship:" in transmitted["state"]["prompt"]


def test_complete_action_is_not_cut_by_section_budget():
    from intaris.evaluation_context import projection_arguments

    original = {"path": "/repo/file", "content": "document-" * 5000}
    model = projection_arguments(original)
    assert len(original["content"]) == 45000
    assert model == original
    assert "[TRUNCATED]" not in model["content"]
    facts = _observe_action("up:write", original, policy={})
    assert facts["arguments"] == model
    assert from_observations(facts).observations["arguments"].coverage == "complete"
    local = _observe_action(
        "artifact_publish",
        {"path": "/tmp/report", "purpose": "Sources: https://public.net"},
        policy={},
        context=metadata(),
    )
    assert local["external_or_irreversible"] is None
    sensitive = _observe_action(
        "read",
        {"file_path": "/home/agent/.aws/credentials"},
        policy={},
        context=metadata(),
    )
    assert sensitive["external_or_irreversible"] is True


def test_judge_keeps_short_human_citation_before_large_context_and_audits_final_call():
    from intaris.judge_simulator import classify, request
    from tools.l1_projection_sizes import specimen

    _, state, result = specimen("inbox-inspection-only")
    artifact = {
        "originals": [
            {
                "ref": "assignment",
                "origin": "client_assignment",
                "content": "a" * 16000,
            },
            {"ref": "summary", "origin": "generated_summary", "content": "s" * 16000},
            {"ref": "parent", "origin": "inherited_context", "content": "p" * 16000},
            {
                "ref": "message:2",
                "origin": "client_user_claim",
                "content": "Read the report only.",
                "chronology": "2",
                "source_event_seq": 2,
                "antecedent": {"content": "NESTED_UNBOUNDED_BODY" * 2000},
            },
        ]
    }
    question = request(
        {"tool": "read", "args_redacted": {"path": "/tmp/report"}},
        {"policy": {"profile": "strict"}},
        state,
        result,
        artifact,
    )
    assert question["overflow"] is False
    assert "message:2" in question["citable"].values()
    assert "NESTED_UNBOUNDED_BODY" not in json.dumps(question["messages"])
    assert question["model_view"]["source_coverage"] == "captured"
    tighter = request(
        {"tool": "read", "args_redacted": {"path": "/tmp/report"}},
        {"policy": {"profile": "strict"}},
        state,
        result,
        artifact,
        max_tokens=4500,
    )
    assert tighter["overflow"] is True
    assert "message:2" in tighter["citable"].values()
    assert tighter["trimmed_sections"] == []
    from intaris.input_budget import ContextInsufficientError

    with pytest.raises(ContextInsufficientError):
        classify(Mock(), tighter)
    llm = Mock()
    llm.judge_response_mode = "json_schema"
    llm.generate.return_value = json.dumps(
        {
            "user_would": "ask",
            "risk": "low",
            "confidence": "high",
            "basis": "missing_permission",
            "cites": [],
            "question": "Confirm scope?",
        }
    )
    _, diagnostics = classify(llm, question)
    call = llm.generate.call_args
    transmitted = {"messages": call.args[0], **call.kwargs}
    expected, size = digest(transmitted)
    assert diagnostics["model_view"]["digest"] == expected
    assert diagnostics["model_view"]["bytes"] == size
    assert "Allowed user_would answers:" in transmitted["messages"][0]["content"]


def test_initial_allocation_keeps_6000_byte_human_citation_before_unrelated_context():
    from intaris.judge_simulator import classify, request
    from tools.l1_projection_sizes import specimen

    _, state, result = specimen("inbox-inspection-only")
    human = "Read only. " + "h" * 5990
    artifact = {
        "originals": [
            {
                "ref": "summary",
                "origin": "generated_summary",
                "content": "s" * 16000,
                "chronology": "1",
            },
            {
                "ref": "parent",
                "origin": "inherited_context",
                "content": "p" * 16000,
                "chronology": "2",
            },
            {
                "ref": "assignment",
                "origin": "client_assignment",
                "content": "a" * 16000,
                "chronology": "0",
            },
            {
                "ref": "message:3",
                "origin": "client_user_claim",
                "content": human,
                "chronology": "3",
            },
        ]
    }
    selected = source_view(artifact)
    assert {row["ref"]: row["content"] for row in selected["sources"]} == {
        row["ref"]: row["content"] for row in artifact["originals"]
    }
    row = next(row for row in selected["sources"] if row["ref"] == "message:3")
    assert row["content"] == human
    assert not row["render_truncated"]
    assert [r["chronology"] for r in selected["sources"]] == ["0", "1", "2", "3"]
    question = request(
        {"tool": "read", "args_redacted": {"path": "/tmp/report"}},
        {"policy": {"profile": "strict"}},
        state,
        result,
        artifact,
    )
    assert not question["overflow"]
    assert "message:3" in question["citable"].values()
    llm = Mock()
    llm.judge_response_mode = "json_schema"
    llm.generate.return_value = json.dumps(
        {
            "user_would": "ask",
            "risk": "low",
            "confidence": "high",
            "basis": "missing_permission",
            "cites": [],
            "question": "Confirm scope?",
        }
    )
    _, diagnostics = classify(llm, question)
    transmitted = {
        "messages": llm.generate.call_args.args[0],
        **llm.generate.call_args.kwargs,
    }
    assert human in transmitted["messages"][1]["content"]
    expected, size = digest(transmitted)
    assert diagnostics["model_view"]["digest"] == expected
    assert diagnostics["model_view"]["bytes"] == size
