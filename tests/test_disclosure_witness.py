import json
from unittest.mock import Mock

import pytest

from intaris import judge_simulator as simulator
from intaris.classifier import observe_hybrid, secret_argument_disclosure
from intaris.hybrid import evaluate_policy
from intaris.l1_projection import render
from intaris.policy_evidence import from_observations
from tests.test_policy_evidence_runtime import runtime as runtime
from tests.test_user_simulator import answer, state


@pytest.mark.parametrize(
    "command,expected",
    [
        ("printenv ORBIT_SERVICE_TOKEN", True),
        ("cat .env", True),
        ("echo $AWS_SECRET_ACCESS_KEY", True),
        ("printenv PATH", False),
        ("cat README.md", False),
        ("head /tmp/private_key.pem", True),
        ("tail credentials.txt", True),
        ("less id_rsa", True),
        ("more passwd.txt", True),
        ("env API_KEY", True),
        ("cat PaSsWoRd.txt", True),
        ("printf hello", False),
        ("grep cat TOKEN", False),
        ("pwd && /usr/bin/printenv ORBIT_SERVICE_TOKEN", True),
        ('echo ";" "$AWS_SECRET_ACCESS_KEY"', True),
        ("pwd\nprintenv ORBIT_SERVICE_TOKEN", True),
    ],
)
def test_secret_argument_observation(command, expected):
    assert secret_argument_disclosure(command) is expected
    facts = observe_hybrid("bash", {"command": command}, policy={})
    if expected:
        assert facts["static_read"] is False
        assert facts["operation"] == "disclose"
        assert facts["risk"] in {"high", "critical"}
        assert "disclose" in facts["effects"]
        evidence = from_observations(facts)
        assert evidence.observations["effects"].source == "secret_argument"
    else:
        assert "secret_argument" not in facts


@pytest.mark.parametrize(
    "confidence,risk,disclose,expected",
    [
        ("medium", "low", False, "ask"),
        ("high", "medium", False, "ask"),
        ("high", "low", False, "approve"),
        ("high", "low", True, "ask"),
    ],
)
def test_model_conflict_approval_gate(confidence, risk, disclose, expected):
    evidence = state()
    evidence = evidence.model_copy(
        update={
            "assessment": {
                **evidence.assessment,
                "contextual_restrictions": "conflicts",
            },
        }
    )
    if disclose:
        facts = observe_hybrid(
            "bash", {"command": "printenv ORBIT_SERVICE_TOKEN"}, policy={}
        )
        evidence = evidence.model_copy(
            update={
                "observations": from_observations(facts).observations,
                # state() grants an inbox deletion. That action-specific grant
                # cannot be reused as authorization to print a credential.
                "assessment": {
                    **evidence.assessment,
                    "authorization": "absent",
                    "authorization_source": "none",
                    "authorization_scope": "unclear",
                },
            }
        )
    prior = evaluate_policy([], evidence, "bounded_autonomy")
    assert any(o.id == "model.denial" for o in prior.outstanding_obligations)
    artifact = {
        "status": "ready",
        "source_metadata": [],
        "items": [],
        "originals": [
            {
                "ref": "message:one",
                "origin": "client_user_claim",
                "content": "Use the configured runtime credential reference.",
            }
        ],
    }
    question = simulator.request({"tool": "bash"}, {}, evidence, prior, artifact)
    vote = answer(risk=risk).model_copy(update={"confidence": confidence})
    mapped, _ = simulator.classify(
        Mock(generate=Mock(return_value=vote.model_dump_json())), question
    )
    assert mapped.user_would == "approve"
    reviewed = evidence.model_copy(
        update={
            "review": simulator.receipt(evidence, prior, mapped, question["citable"]),
        }
    )
    result = evaluate_policy([], reviewed, "bounded_autonomy")
    # The validated Judge decision resolves this model-derived conflict. It
    # does not erase observed credential disclosure/risk requirements.
    assert not any(o.id == "model.denial" for o in result.outstanding_obligations)
    assert result.outcome == ("escalate" if disclose else "approve")
    if disclose:
        payload = json.loads(question["messages"][1]["content"])
        assert "disclose" in payload["controller_facts"]["effects"]
        assert payload["controller_facts"]["secret_argument"]
        projected = render(artifact, evidence, {})
        assert "Observed.effects: disclose" in projected
        assert "Observed.secret_argument: True" in projected


@pytest.mark.parametrize(
    "command",
    [
        "printenv ORBIT_SERVICE_TOKEN",
        'echo ";" "$AWS_SECRET_ACCESS_KEY"',
        "pwd\nprintenv ORBIT_SERVICE_TOKEN",
    ],
)
def test_disclosure_survives_runtime_observers_and_persistence(runtime, command):
    _, audit, _, evaluator, sid = runtime
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert result["decision"] == "escalate"
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    observations = record["evaluation_metadata"]["policy_evidence"]["observations"]
    assert "disclose" in observations["effects"]["value"]
    assert observations["effects"]["source"] == "secret_argument"
    assert observations["static_read"]["value"] is False
    assert observations["risk"]["value"] == "high"
    from intaris.policy_evidence import Evidence

    evidence = Evidence.model_validate(record["evaluation_metadata"]["policy_evidence"])
    artifact = {
        "originals": [
            {
                "ref": "message:one",
                "origin": "client_user_claim",
                "content": "Inspect the configuration.",
            }
        ]
    }
    q = simulator.request(
        record,
        {},
        evidence,
        evaluate_policy([], evidence, "bounded_autonomy"),
        artifact,
    )
    vote, _ = simulator.classify(
        Mock(generate=Mock(return_value=answer().model_dump_json())), q
    )
    reviewed = evidence.model_copy(
        update={
            "review": simulator.receipt(
                evidence,
                evaluate_policy([], evidence, "bounded_autonomy"),
                vote,
                q["citable"],
            ),
        }
    )
    assert evaluate_policy([], reviewed, "bounded_autonomy").outcome != "approve"
    assert reviewed.observations["risk"].value == "high"
    assert "disclose" in reviewed.value("effects")
