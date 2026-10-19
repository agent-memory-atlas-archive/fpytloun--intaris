"""Guarded fixture preparation does not run runtime code or model calls."""

from tools.benchmark.hybrid_cases import prepare
from tools.benchmark.matched import freeze


def test_prepared_map_preserves_baseline_and_optional_protocol(tmp_path):
    contract = tmp_path / "contract.md"
    contract.write_text(
        "hybrid_v1 require_approval evaluation_policy_version path_matches No controller"
    )
    manifest = freeze()
    mapping = prepare(contract, manifest)
    assert mapping["baseline_manifest_sha256"] == manifest["sha256"]
    assert len(mapping["profile_matrix"]) == 36
    assert len(mapping["compilation_cases"]) == 7
    assert (
        mapping["existing_cohort_activation"]["server_default_old_requests"][
            "new_request_fields"
        ]
        == []
    )
    for item in mapping["profile_matrix"]:
        assert not item["auto_resolve"]
        assert set(item["policy"]) == {
            "evaluation_policy_version",
            "profile",
            "rules",
            "intention_gate",
        }
        assert item["policy"]["intention_gate"] == "disabled"
        assert "controller_context" not in item["request"]
    mandatory = [
        item
        for item in mapping["profile_matrix"]
        if item["family"] == "mandatory_human"
    ]
    assert len(mandatory) == 9
    assert all(item["expected_terminal"] == "escalate" for item in mandatory)
    assert mapping["status"].startswith("prepared_only")
