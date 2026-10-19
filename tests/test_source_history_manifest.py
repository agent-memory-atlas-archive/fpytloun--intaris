"""Offline integrity, contamination and linkage specimens."""

import copy
import hashlib

import pytest

from tools.benchmark.source_history_manifest import (
    MANIFEST,
    checkpoint_contract,
    expand_events,
    history_prefix,
    load_manifest,
    manifest_sha256,
    validate_manifest,
)


def test_counts_bounds_and_hash():
    data = load_manifest()
    assert sum(len(s["checkpoints"]) for s in data["stories"]) == 48
    for story in data["stories"]:
        events = expand_events(story)
        assert events == expand_events(story)
        assert len(events) <= 600
        assert len({e["id"] for e in events}) == len(events)
    assert manifest_sha256() == hashlib.sha256(MANIFEST.read_bytes()).hexdigest()


def test_projection_excludes_oracle_and_future_bindings():
    data = load_manifest()
    for story in data["stories"]:
        for checkpoint in story["checkpoints"]:
            projected = history_prefix(data, story["id"], checkpoint["id"])
            assert set(projected) == {"actors", "events", "action_bindings"}
            assert projected["events"][-1]["block_id"] == checkpoint["after"]
            for event in projected["events"]:
                assert (
                    not {"retain", "scope", "quotes", "probe", "authorization"}
                    & event.keys()
                )
    assert history_prefix(data, "s06", "s06c1")["action_bindings"] == {}


def test_clear_ambiguous_missing_yes():
    data = load_manifest()
    stories = {s["id"]: s for s in data["stories"]}
    clear = checkpoint_contract(stories["s02"], "s02c4")
    approval = next(i for i in clear["items"] if i["id"] == "y")
    assert approval["sources"] == ["s02e4"]
    assert approval["reference_context"] == ["s02e3"]
    assert clear["probe"]["authorization"] == "explicit_scoped"
    ambiguous = checkpoint_contract(stories["s01"], "s01c3")
    assert (
        next(i for i in ambiguous["items"] if i["id"] == "q")["polarity"] == "unknown"
    )
    missing = checkpoint_contract(stories["s08"], "s08c5")
    assert missing["probe"]["authorization"] == "unknown"
    assert missing["coverage"]["unavailable_reference_context"] == ["s08e5"]


def test_origins_and_coverage():
    data = load_manifest()
    events = history_prefix(data, "s06", "s06c5")["events"]
    decisions = [e["decision"] for e in events if "decision" in e]
    assert [d["resolver_origin"] for d in decisions] == ["judge", "human", "human"]
    story = next(s for s in data["stories"] if s["id"] == "s08")
    coverage = checkpoint_contract(story, "s08c6")["coverage"]
    assert coverage["missing_source_items"] == []
    assert coverage["content_truncation"]


@pytest.mark.parametrize(
    "mutation",
    [
        "quote",
        "source",
        "future",
        "actor",
        "reply",
        "repeat",
        "scope",
        "resolver",
        "binding",
        "coverage",
        "probe",
    ],
)
def test_invalid_manifest(mutation):
    data = copy.deepcopy(load_manifest())
    stories = {s["id"]: s for s in data["stories"]}
    story = stories["s01"]
    if mutation == "quote":
        story["items"][0]["quotes"] = ["Never recorded wording"]
    elif mutation == "source":
        story["items"][0]["sources"] = ["s01e99"]
    elif mutation == "future":
        story["checkpoints"][0]["retain"].append("p")
    elif mutation == "actor":
        story["events"][0]["actor"] = "t"
    elif mutation == "reply":
        stories["s02"]["events"][3]["reply_to"] = "s02e1"
    elif mutation == "repeat":
        story["events"][0]["repeat"] = 201
    elif mutation == "scope":
        story["items"][0]["scope"] = []
    elif mutation == "resolver":
        stories["s06"]["events"][1]["decision"]["resolver_origin"] = "human"
    elif mutation == "binding":
        stories["s06"]["action_bindings"] = {}
    elif mutation == "coverage":
        story["checkpoints"][0]["coverage"] = {}
    elif mutation == "probe":
        story["checkpoints"][0]["probe"] = {"authorization": "unknown"}
    with pytest.raises(ValueError):
        validate_manifest(data)


def test_churn_and_copy_isolation():
    data = load_manifest()
    before = history_prefix(data, "s02", "s02c4")
    after = history_prefix(data, "s02", "s02c5")
    assert [e for e in before["events"] if e["actor"] == "u"] == [
        e for e in after["events"] if e["actor"] == "u"
    ]
    after["events"][0]["text"] = "changed"
    assert history_prefix(data, "s02", "s02c1")["events"][0]["text"].startswith("Draft")


def test_injected_oracle_fields_are_not_projected():
    data = load_manifest()
    story = next(s for s in data["stories"] if s["id"] == "s06")
    story["events"][0].update({"retain": ["r"], "authorization": "approve"})
    story["events"][1]["decision"]["expected"] = "approve"
    story["action_bindings"]["s06a1"]["probe"] = {"authorization": "approve"}
    projected = history_prefix(data, "s06", "s06c2")
    assert "retain" not in projected["events"][0]
    assert "authorization" not in projected["events"][0]
    assert "expected" not in projected["events"][1]["decision"]
    assert set(projected["action_bindings"]["s06a1"]) == {"tool", "args"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool", None),
        ("tool", ""),
        ("args", None),
        ("args", []),
    ],
)
def test_malformed_existing_probe(field, value):
    data = load_manifest()
    story = next(s for s in data["stories"] if s["id"] == "s02")
    story["checkpoints"][3]["probe"][field] = value
    with pytest.raises(ValueError):
        validate_manifest(data)


def test_credential_positive_is_scoped_not_effect_verdict():
    data = load_manifest()
    story = next(s for s in data["stories"] if s["id"] == "s05")
    contract = checkpoint_contract(story, "s05c6")
    assert contract["probe"]["authorization"] == "explicit_scoped"
    assert (
        contract["probe"]["args"]["credential_mode"] == "configured_runtime_reference"
    )
    assert contract["unknown"] == ["effect safety requires actual tool observations"]
