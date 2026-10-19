"""Test-only projection into selective schemas; never upgrades authorization."""

import json
from unittest.mock import Mock

from intaris.hybrid import Assessment


class EvidenceMock(Mock):
    def __call__(self, *args, **kwargs):
        value = super().__call__(*args, **kwargs)
        schema = kwargs.get("json_schema") or {}
        if schema.get("name") not in {
            "hybrid_selective_assessment",
            "hybrid_obligation_review",
            "user_simulator",
        } or not isinstance(value, str):
            return value
        try:
            old = json.loads(value)
        except ValueError:
            return value
        if not isinstance(old, dict) or "relationship" not in old:
            return value
        if set(old) - set(Assessment.model_fields):
            return value  # Keep unknown-field negative controls invalid.
        if schema.get("name") == "user_simulator":
            return json.dumps(
                {
                    "user_would": "refuse"
                    if old.get("relationship") == "conflicts_with_intent"
                    else "ask",
                    "risk": old["risk"] if old["risk"] != "unknown" else "medium",
                    "confidence": "low",
                    "basis": "missing_permission",
                    "cites": [],
                    "question": "Confirm the action scope?",
                    "action_summary": "Perform the proposed action",
                    "explanation": "The test assessment has not established action permission.",
                    "restriction_quote": "",
                }
            )
        conflict = old.get("relationship") == "conflicts_with_intent"
        inputs = {
            **old,
            "contextual_restrictions": "conflicts" if conflict else "consistent",
            "restriction_reference": "session_intention",
            "target_scope": "conflicts"
            if old.get("authorization_scope") == "outside_scope"
            else "consistent",
            "target_reference": "action",
            "consequence": "consequential" if old.get("risk") == "high" else "routine",
        }
        body = schema["schema"]
        fields = body["properties"]
        if "classification" in fields:
            fields = fields["classification"]["properties"]
        projected = {key: inputs[key] for key in fields if key in inputs}
        if "obligations" in body["properties"]:
            projected = {"classification": projected, "obligations": {}}
            for identifier, shape in body["properties"]["obligations"][
                "properties"
            ].items():
                refs = shape["properties"]["reference"]["enum"]
                projected["obligations"][identifier] = {
                    "status": "conflicts" if conflict else "consistent",
                    "reference": "session_intention"
                    if "session_intention" in refs
                    else "action",
                }
        return json.dumps(projected)
