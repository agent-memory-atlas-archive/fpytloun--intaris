"""One bounded source-text contract for extraction, validation and citations."""

import os

MAX_SOURCE_CHARS = int(os.environ.get("INTARIS_SOURCE_MAX_CHARS", "16000"))
MAX_CAPTURE_CHARS = 48000
if not 1 <= MAX_SOURCE_CHARS <= MAX_CAPTURE_CHARS // 2:
    raise ValueError("INTARIS_SOURCE_MAX_CHARS must be between 1 and 24000")

USER_ORIGINS = {"client_user_claim", "recorded_user_event"}


def user_reasoning_role_sql(backend: str) -> str:
    """Use recorded role, retaining legacy prefix-only clients when absent."""
    return (
        "COALESCE(args_redacted::jsonb #>> '{context,role}', "
        "args_redacted::jsonb ->> 'role', 'user')"
        if backend == "postgresql"
        else "COALESCE(json_extract(args_redacted, '$.context.role'), "
        "json_extract(args_redacted, '$.role'), 'user')"
    )


def capture_text(text, *, limit=MAX_SOURCE_CHARS):
    selected = text[: min(limit, MAX_SOURCE_CHARS)]
    return {
        "content": selected,
        "truncated": selected != text,
        "original_length": len(text),
        "captured_length": len(selected),
    }


def bound_sources(rows):
    """Mutate captured rows once; preserve newest user and its latest alias first.

    The total counts every presented body, including duplicate latest-message
    aliases. Two per-source slots are reserved by the configuration ceiling.
    Remaining bodies are consumed newest-first, without quote-only substitutes.
    """
    users = [
        r
        for r in rows
        if r["origin"] in USER_ORIGINS and r["ref"] != "latest_user_message"
    ]
    newest = max(
        users, key=lambda r: (r.get("chronology") or "", r["ref"]), default=None
    )
    ordered = sorted(
        rows,
        key=lambda r: (
            r is newest or r["ref"] == "latest_user_message",
            r.get("chronology") or "",
            r["ref"],
        ),
        reverse=True,
    )
    budget = MAX_CAPTURE_CHARS
    for row in ordered:
        old = row["content"]
        selected = old[: min(MAX_SOURCE_CHARS, max(0, budget))]
        row.update(
            content=selected,
            truncated=row.get("truncated", False) or old != selected,
            original_length=row.get("original_length", len(old)),
            captured_length=len(selected),
        )
        budget -= len(selected)
    return rows


def complete_source(row):
    """Citation admission never treats a prefix as a complete user statement."""
    return (
        not row.get("truncated")
        and len(row["content"]) <= MAX_SOURCE_CHARS
        and row.get("original_length", len(row["content"])) == len(row["content"])
    )
