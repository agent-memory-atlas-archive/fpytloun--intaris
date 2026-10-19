"""New-session defaults change; persisted and explicitly selected modes do not."""

import pytest

from intaris.policy_activation import default_version, prepare_policy


def test_unconfigured_new_session_uses_hybrid_balanced(monkeypatch):
    monkeypatch.delenv("EVALUATION_POLICY_VERSION", raising=False)
    assert default_version() == "hybrid_v1"
    policy = prepare_policy(None, tenant="release-test")
    assert policy["evaluation_policy_version"] == "hybrid_v1"
    assert policy["profile"] == "balanced"
    assert policy["active_policy"]["profile"] == "balanced"


@pytest.mark.parametrize("policy", [None, {"deny_tools": ["publish"]}])
def test_existing_unversioned_legacy_session_is_not_silently_migrated(
    monkeypatch, policy
):
    monkeypatch.delenv("EVALUATION_POLICY_VERSION", raising=False)
    assert (
        prepare_policy(policy, tenant="release-test", existing_version="legacy")
        == policy
    )


def test_explicit_legacy_and_deployment_opt_in_remain_supported(monkeypatch):
    monkeypatch.delenv("EVALUATION_POLICY_VERSION", raising=False)
    policy = {"evaluation_policy_version": "legacy", "deny_tools": ["publish"]}
    assert prepare_policy(policy, tenant="release-test") == policy
    monkeypatch.setenv("EVALUATION_POLICY_VERSION", "legacy")
    assert prepare_policy(None, tenant="release-test") is None
    explicit = prepare_policy(
        {"evaluation_policy_version": "hybrid_v1"}, tenant="release-test"
    )
    assert explicit["profile"] == "balanced"


def test_explicit_profile_is_not_overwritten(monkeypatch):
    monkeypatch.delenv("EVALUATION_POLICY_VERSION", raising=False)
    assert (
        prepare_policy({"profile": "strict"}, tenant="release-test")["profile"]
        == "strict"
    )


def test_persisted_legacy_session_survives_restart_and_policy_update(
    monkeypatch, tmp_path
):
    from intaris.config import DBConfig
    from intaris.db import Database
    from intaris.session import SessionStore

    config = DBConfig(path=str(tmp_path / "policy-upgrade.db"))
    monkeypatch.setenv("EVALUATION_POLICY_VERSION", "legacy")
    original = Database(config)
    SessionStore(original).create(
        user_id="owner", session_id="pre-upgrade", intention="Review local code"
    )
    original.close()
    monkeypatch.delenv("EVALUATION_POLICY_VERSION")
    upgraded = Database(config)
    try:
        sessions = SessionStore(upgraded)
        assert sessions.get("pre-upgrade", user_id="owner")["policy"] is None
        changed = sessions.update_session(
            "pre-upgrade", user_id="owner", policy={"deny_tools": ["publish"]}
        )
        assert changed["policy"] == {"deny_tools": ["publish"]}
        created = sessions.create(
            user_id="owner", session_id="post-upgrade", intention="Review local code"
        )
        assert created["policy"]["evaluation_policy_version"] == "hybrid_v1"
        assert created["policy"]["profile"] == "balanced"
    finally:
        upgraded.close()
