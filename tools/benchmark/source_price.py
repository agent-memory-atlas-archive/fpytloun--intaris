"""Conservative public-tariff reservation; not an invoice/cost prediction."""

import json
from pathlib import Path

MANIFEST = Path(__file__).parent / "fixtures/source-context-public-pricing-v1.json"


def request_bound(model: str, payload: dict) -> float:
    pricing = json.loads(MANIFEST.read_text())["models"]
    if model not in pricing:
        raise ValueError("Unpriced model blocked")
    limits = pricing[model]
    outputs = [
        payload[key]
        for key in ("max_tokens", "max_completion_tokens")
        if key in payload
    ]
    if any(
        type(value) is not int or not 0 < value <= limits["output_token_ceiling"]
        for value in outputs
    ):
        raise ValueError("Output limit exceeds priced native bound")
    # Full native input capacity, no cache discount or guessed text/token ratio.
    cap = max(outputs) if outputs else limits["output_token_ceiling"]
    return (
        limits["input_token_ceiling"] * limits["input_per_million"]
        + cap * limits["output_per_million"]
    ) / 1_000_000


def global_bound() -> float:
    pricing = json.loads(MANIFEST.read_text())["models"]
    return max(request_bound(model, {}) for model in pricing)
