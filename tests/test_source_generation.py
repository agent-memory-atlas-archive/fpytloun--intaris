"""Offline selector invariants, not model semantic quality claims."""

import pytest

from intaris.task_context import (
    QuoteExtraction,
    exact_quote_span,
    extraction_wire_schema,
)


@pytest.mark.parametrize(
    "content,quote",
    [
        ("Keep origin", "eep origin"),
        ("yes yes", "yes"),
        ("Use /repo/private/file", "/private/file"),
        ("mail dev@example.test", "example.test"),
        ("Use draft-123", "draft"),
        ("No deployment", "deployment missing"),
    ],
)
def test_invalid_quote_selector(content, quote):
    with pytest.raises(ValueError):
        exact_quote_span(content, quote)


def test_quote_selector_scope_and_wire_no_generated_identifiers():
    text = "Keep existing origin. Do not deploy."
    start, end = exact_quote_span(text, "Do not deploy.")
    assert text[start:end] == "Do not deploy."
    schema = extraction_wire_schema(QuoteExtraction, ["session_intention"])
    item = schema["$defs"]["QuoteItem"]
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False
    assert item["properties"]["source_ref"]["enum"] == ["session_intention"]
    assert not {"start", "end", "scope_refs"} & item["properties"].keys()
