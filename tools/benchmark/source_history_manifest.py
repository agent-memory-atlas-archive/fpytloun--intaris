"""Offline synthetic history utilities. No runtime or provider calls."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

MANIFEST = Path(__file__).parent / "fixtures" / "source-history-v1.json"
ORIGINS = {
    "client_attributed_user",
    "agent",
    "tool",
    "human_resolver",
    "judge_resolver",
}


def load_manifest(path: Path = MANIFEST) -> dict:
    data = json.loads(path.read_text())
    validate_manifest(data)
    return data


def manifest_sha256(path: Path = MANIFEST) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expand_events(story: dict) -> list[dict]:
    events = []
    for event in story["events"]:
        for index in range(event.get("repeat", 1)):
            record = copy.deepcopy(event)
            record.pop("repeat", None)
            record["block_id"] = event["id"]
            if event["kind"] == "filler":
                record["id"] = f"{event['id']}.{index + 1:03d}"
            events.append(record)
    return events


def history_prefix(data: dict, story_id: str, checkpoint_id: str) -> dict:
    """Allowlisted upstream records only; never send scoring expectations."""
    validate_manifest(data)
    story = next(s for s in data["stories"] if s["id"] == story_id)
    checkpoint = next(c for c in story["checkpoints"] if c["id"] == checkpoint_id)
    end = next(
        i for i, e in enumerate(story["events"]) if e["id"] == checkpoint["after"]
    )
    events = []
    for event in expand_events({"events": story["events"][: end + 1]}):
        record = {
            key: value
            for key, value in event.items()
            if key
            in {
                "id",
                "actor",
                "kind",
                "text",
                "block_id",
                "reply_to",
                "reference_context_status",
            }
        }
        if "decision" in event:
            record["decision"] = {
                key: value
                for key, value in event["decision"].items()
                if key in {"resolver_origin", "resolution", "action_binding", "scope"}
            }
        events.append(record)
    bindings = {e["decision"]["action_binding"] for e in events if "decision" in e}
    return copy.deepcopy(
        {
            "actors": data["actors"],
            "events": events,
            "action_bindings": {
                key: {"tool": value["tool"], "args": value["args"]}
                for key, value in story.get("action_bindings", {}).items()
                if key in bindings
            },
        }
    )


def checkpoint_contract(story: dict, checkpoint_id: str) -> dict:
    """Oracle-only; not an ideal intention for model input."""
    checkpoint = next(c for c in story["checkpoints"] if c["id"] == checkpoint_id)
    return copy.deepcopy(
        {
            **checkpoint,
            "items": [i for i in story["items"] if i["id"] in checkpoint["retain"]],
            "coverage": checkpoint.get(
                "coverage",
                {
                    "missing_source_items": [],
                    "unavailable_reference_context": [],
                    "content_truncation": [],
                },
            ),
        }
    )


def validate_manifest(data: dict) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    require(data["version"] == "source-history-v1", "Unsupported version")
    require(set(data["actors"].values()) == ORIGINS, "Invalid origins")
    require(
        data["bounds"]
        == {
            "stories": 8,
            "checkpoints_per_story": 6,
            "max_events": 600,
            "max_text_chars": 4096,
            "max_filler_repeat": 200,
        },
        "Invalid frozen bounds",
    )
    require(len(data["stories"]) == 8, "Expected eight stories")
    identifiers = set()
    for story in data["stories"]:
        events = {e["id"]: e for e in story["events"]}
        order = {e["id"]: n for n, e in enumerate(story["events"])}
        items = {i["id"]: i for i in story["items"]}
        require(len(events) == len(story["events"]), "Duplicate event")
        require(len(items) == len(story["items"]), "Duplicate item")
        require(len(story["checkpoints"]) == 6, "Expected six checkpoints")
        for binding in story.get("action_bindings", {}).values():
            require(
                isinstance(binding.get("tool"), str) and bool(binding["tool"]),
                "Missing binding tool",
            )
            require(isinstance(binding.get("args"), dict), "Missing binding args")
        for identifier in [
            story["id"],
            *events,
            *(c["id"] for c in story["checkpoints"]),
        ]:
            require(identifier not in identifiers, "Duplicate identifier")
            identifiers.add(identifier)
        for event in events.values():
            require(event["actor"] in data["actors"], "Unknown actor")
            origin = data["actors"][event["actor"]]
            require(
                event["kind"]
                in {
                    "request",
                    "clarification",
                    "reasoning",
                    "question",
                    "tool_result",
                    "decision",
                    "filler",
                },
                "Invalid kind",
            )
            require(
                isinstance(event["text"], str) and 0 < len(event["text"]) <= 4096,
                "Text bound",
            )
            repeat = event.get("repeat", 1)
            require(type(repeat) is int and 1 <= repeat <= 200, "Repeat bound")
            require(
                repeat == 1
                or (event["kind"] == "filler" and origin in {"agent", "tool"}),
                "Source cannot repeat",
            )
            if "reply_to" in event:
                target = event["reply_to"]
                require(
                    target in order and order[target] < order[event["id"]],
                    "Invalid reply reference",
                )
                require(
                    origin == "client_attributed_user"
                    and data["actors"][events[target]["actor"]] == "agent",
                    "Invalid reply provenance",
                )
            if event["kind"] == "decision":
                decision = event["decision"]
                require(
                    origin in {"human_resolver", "judge_resolver"}
                    and decision["resolver_origin"] == origin.removesuffix("_resolver"),
                    "Resolver mismatch",
                )
                require(
                    decision["action_binding"] in story.get("action_bindings", {}),
                    "Missing binding",
                )
                require(bool(decision["scope"]), "Empty decision scope")
        require(len(expand_events(story)) <= 600, "Expanded bound")
        for item in items.values():
            require(
                item["kind"]
                in {
                    "request",
                    "restriction",
                    "approval",
                    "revocation",
                    "durable_context",
                    "uncertainty",
                    "decision_context",
                },
                "Invalid item kind",
            )
            require(bool(item["scope"]), "Empty scope is not all")
            require(
                item["polarity"] in {"positive", "negative", "neutral", "unknown"},
                "Invalid polarity",
            )
            require(
                bool(item["sources"]) and len(item["sources"]) == len(item["quotes"]),
                "Missing quote references",
            )
            for source, quote in zip(item["sources"], item["quotes"], strict=True):
                require(source in events, "Missing source")
                require(
                    bool(quote) and quote in events[source]["text"], "Invalid quote"
                )
                origin = data["actors"][events[source]["actor"]]
                require(
                    origin
                    in (
                        {"judge_resolver"}
                        if item["kind"] == "decision_context"
                        else {"client_attributed_user", "human_resolver"}
                    ),
                    "Non-authorizing origin",
                )
                if item["status"] == "recorded_human":
                    require(origin == "human_resolver", "Human origin mismatch")
                for reference in item.get("reference_context", []):
                    require(
                        reference in events
                        and events[source].get("reply_to") == reference,
                        "Unlinked reference context",
                    )
        previous = -1
        checkpoints = {}
        for checkpoint in story["checkpoints"]:
            require(checkpoint["after"] in order, "Missing boundary")
            end = order[checkpoint["after"]]
            require(end > previous, "Checkpoints must evolve")
            previous = end
            require(isinstance(checkpoint["unknown"], list), "Missing uncertainty")
            require(
                ("probe" in checkpoint) != (checkpoint.get("no_action") is True),
                "One probe or explicit no_action required",
            )
            if "probe" in checkpoint:
                require(
                    isinstance(checkpoint["probe"].get("tool"), str)
                    and bool(checkpoint["probe"]["tool"]),
                    "Missing probe tool",
                )
                require(
                    isinstance(checkpoint["probe"].get("args"), dict),
                    "Missing probe args",
                )
                require(
                    checkpoint["probe"]["authorization"]
                    in {
                        "explicit_scoped",
                        "outside_scope",
                        "revoked",
                        "unknown",
                    },
                    "Invalid authorization",
                )
            for item_id in checkpoint["retain"]:
                require(item_id in items, "Missing item")
                for source in items[item_id]["sources"] + items[item_id].get(
                    "reference_context", []
                ):
                    require(order[source] <= end, "Future reference")
            coverage = checkpoint_contract(story, checkpoint["id"])["coverage"]
            require(
                set(coverage)
                == {
                    "missing_source_items",
                    "unavailable_reference_context",
                    "content_truncation",
                },
                "Invalid coverage",
            )
            for source in (
                coverage["missing_source_items"]
                + coverage["unavailable_reference_context"]
            ):
                require(
                    source in order and order[source] <= end, "Invalid coverage source"
                )
            for span in coverage["content_truncation"]:
                require(
                    span["source"] in order and order[span["source"]] <= end,
                    "Invalid truncation source",
                )
                require(
                    0
                    <= span["start"]
                    < span["end"]
                    <= len(events[span["source"]]["text"]),
                    "Invalid truncation span",
                )
            if "unchanged_authorizing_sources_from" in checkpoint:
                prior = checkpoint["unchanged_authorizing_sources_from"]
                require(prior in checkpoints, "Missing previous checkpoint")
                require(
                    checkpoint["retain"] == checkpoints[prior]["retain"],
                    "Churn changed retained sources",
                )
            checkpoints[checkpoint["id"]] = checkpoint
    for plan in data["invariant_plans"]:
        require(
            plan["kind"] == "invariant"
            and bool(plan["steps"])
            and bool(plan["assertions"]),
            "Invalid invariant plan",
        )
