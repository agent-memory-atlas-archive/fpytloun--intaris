"""Source-coverage checks for a bounded, full-match English authoring subset."""

from __future__ import annotations

import json
import re


def _eq(field, value):
    return {"op": "eq", "field": field, "value": value}


def _effect(value):
    return {"op": "contains", "field": "effects", "value": value}


def source_constraint(text: str, effect: str) -> dict | None:
    text = text.strip().rstrip(".").strip()
    prefix = (
        r"(?:allow|permit)" if effect == "allow" else r"(?:deny|forbid|do not|never)"
    )
    body = re.fullmatch(prefix + r"\s+(.+)", text, re.I)
    if not body:
        return None
    clause = body[1]
    path = re.fullmatch(
        r"(reads?|deletion)\s+of\s+(?:files|paths)\s+(not\s+)?matching\s+[`\"']?([^`\"'\s]+)[`\"']?",
        clause,
        re.I,
    )
    if path:
        operation = (
            _eq("operation", "read")
            if path[1].lower().startswith("read")
            else _effect("delete")
        )
        quantifier = "any" if effect == "deny" and not path[2] else "all"
        target = {"op": "path_matches", "value": path[3], "quantifier": quantifier}
        if path[2]:
            target = {"op": "not", "children": [target]}
        children = [operation, target]
        if path[1].lower().startswith("read"):
            # A read grant covers the whole call, not just one reported effect.
            children.append(_effect("read"))
            children.extend(
                {"op": "not", "children": [_effect(value)]}
                for value in ("write", "delete", "execute", "transmit")
            )
        return {"op": "all", "children": children}
    environment = re.fullmatch(
        r"(?:modify|modification of|modifications? (?:of|in))\s+(production|development)\s+resources",
        clause,
        re.I,
    )
    if environment:
        return {
            "op": "all",
            "children": [
                _eq("environment", environment[1].lower()),
                {"op": "any", "children": [_effect("write"), _effect("delete")]},
            ],
        }
    action = re.fullmatch(
        r"(read|reads|reading|write|writes|writing|delete|deletion)", clause, re.I
    )
    if action:
        word = action[1].lower()
        return (
            _effect("delete")
            if word.startswith("delet")
            else _eq("operation", "read" if word.startswith("read") else "write")
        )
    tool = re.fullmatch(
        r"(?:tool|tools)\s+matching\s+[`\"']?([^`\"'\s]+)[`\"']?", clause, re.I
    )
    if tool:
        return {"op": "tool_matches", "value": tool[1]}
    return None


def canonical_predicate(node: dict) -> str:
    node = {key: value for key, value in node.items() if value is not None}
    op = node["op"]
    if op == "in" and len(node["value"]) == 1:
        node = {**node, "op": "eq", "value": node["value"][0]}
    if "children" in node:
        children = [
            json.loads(canonical_predicate(child)) for child in node["children"]
        ]
        if op in {"all", "any"}:
            flattened = []
            for child in children:
                flattened.extend(child["children"] if child["op"] == op else [child])
            children = sorted(
                flattened, key=lambda child: json.dumps(child, sort_keys=True)
            )
            if len(children) == 1:
                return canonical_predicate(children[0])
        node["children"] = children
    return json.dumps(node, sort_keys=True, separators=(",", ":"))
