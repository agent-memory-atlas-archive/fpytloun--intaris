"""Harness reads existing public owners only; no debug endpoint/cache injection."""

import asyncio
import copy
import time

from tools.benchmark.matched import digest


def audit_resolution(client, session_id: str, call_id: str) -> dict:
    audit = client.get_audit_record(call_id)
    if audit.get("session_id") != session_id:
        raise ValueError("Resolution belongs to another session")
    resolver = audit.get("resolved_by")
    return {
        "call_id": call_id,
        "origin": resolver,
        "human_source_eligible": resolver == "user",
        "decision": audit.get("user_decision"),
        "tool": audit.get("tool"),
        "args": copy.deepcopy(audit.get("args_redacted")),
        "audit": copy.deepcopy(audit),
    }


async def await_recorded_generation(
    *, barrier, user_id, session_id, read_session, provider_completion, timeout_s=5.0
):
    """A changed intention string or barrier boolean does not prove generation."""
    before = copy.deepcopy(read_session())
    start = time.monotonic()
    try:
        waited = await asyncio.wait_for(
            barrier.wait(user_id, session_id, timeout_override=timeout_s),
            timeout=timeout_s,
        )
    except TimeoutError:
        return {
            "state": "UNESTABLISHED",
            "reason": "actual_barrier_timeout",
            "before": before,
            "elapsed_ms": (time.monotonic() - start) * 1000,
        }
    after = copy.deepcopy(read_session())
    completion = copy.deepcopy(provider_completion())
    ready = (
        isinstance(completion, dict)
        and completion.get("session_id") == session_id
        and completion.get("user_id") == user_id
        and completion.get("completed") is True
        and completion.get("generation_accepted") is True
        and completion.get("applied_source_revision")
        == completion.get("source_revision")
        and completion.get("source_revision") == after.get("latest_user_message_seq")
        and after.get("latest_user_message_seq") is not None
        and time.monotonic() - start <= timeout_s
    )
    return {
        "state": "ready" if ready else "UNESTABLISHED",
        "barrier_returned": waited,
        "before": before,
        "after": after,
        "provider_completion": completion,
        "elapsed_ms": (time.monotonic() - start) * 1000,
    }


class ArtifactBinding:
    """Explicit supplied public TaskContext object; no evaluator private access."""

    def __init__(self, task_context, user_id: str, session_id: str):
        self.owner, self.user_id, self.session_id = task_context, user_id, session_id

    def observe(self, artifact_digest: str) -> dict:
        getter = getattr(self.owner, "observe_artifact", None)
        if not callable(getter):
            return {"state": "UNESTABLISHED", "reason": "readonly_owner_getter_pending"}
        artifact = getter(self.user_id, self.session_id, artifact_digest)
        if artifact is None:
            return {"state": "MISSING", "evidence_level": "readonly_owner_observer"}
        if (
            artifact.get("tenant") != self.user_id
            or artifact.get("session") != self.session_id
            or artifact.get("artifact_digest") != artifact_digest
        ):
            raise ValueError("Observed artifact tenant/session/digest mismatch")
        return {
            "state": "CAPTURED",
            "artifact": copy.deepcopy(artifact),
            "evidence_level": "readonly_owner_observer",
        }

    def generate_component(self, recorded_session: dict) -> dict:
        """Deliberate generation service call, never mislabelled cache observation."""
        if (
            recorded_session.get("user_id") != self.user_id
            or recorded_session.get("session_id") != self.session_id
        ):
            raise ValueError("Component session identity mismatch")
        artifact = self.owner.capture(copy.deepcopy(recorded_session))
        return {
            "state": "CAPTURED",
            "artifact": copy.deepcopy(artifact),
            "evidence_level": "component_generation",
            "end_to_end": False,
        }


def preserved_watermarks(artifact: dict) -> dict:
    """Core digests remain digests, not invented sequence numbers."""
    marks = artifact.get("watermarks", {})
    required = ("messages", "decisions", "latest_user", "intention")
    for key in required:
        value = marks.get(key)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)
        ):
            raise ValueError("Native source watermark unavailable")
    return copy.deepcopy(marks)


def provider_input_evidence(
    payload: dict, artifact: dict | None, *, stage: str
) -> dict:
    """Inspect actual serialized GPT/Jev request, not just displayed messages.

    Returns hashes/booleans only. Bodies and semantic gold are never sent to a
    provider by this observer. No artifact means unavailable, not a clean input.
    """
    import json

    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)

    def strings(value, depth=0):
        if depth > 12:
            raise ValueError("Provider state too deeply nested to verify")
        if isinstance(value, dict):
            return [
                text for child in value.values() for text in strings(child, depth + 1)
            ]
        if isinstance(value, list):
            return [text for child in value for text in strings(child, depth + 1)]
        if isinstance(value, str):
            texts = [value]
            marker = "RICH INTENTION (bounded interpretation):\n"
            if artifact and marker in value:
                start = value.index(marker) + len(marker)
                try:
                    compact, end = json.JSONDecoder().raw_decode(value[start:])
                    if (
                        isinstance(compact, dict)
                        and compact.get("artifact_digest")
                        == artifact.get("artifact_digest")
                        and compact.get("compact_intention")
                        == artifact.get("compact_intention")
                        and isinstance(compact.get("compact_intention"), str)
                    ):
                        # Exempt this approved field, not the same text hidden
                        # elsewhere in serialized state or raw source bodies.
                        compact = {
                            k: v for k, v in compact.items() if k != "compact_intention"
                        }
                        texts = [value[:start] + value[start + end :]]
                        texts.extend(strings(compact, depth + 1))
                except json.JSONDecodeError:
                    pass
            if value.lstrip().startswith(("{", "[")):
                try:
                    texts = strings(json.loads(value), depth + 1)
                except json.JSONDecodeError:
                    pass
            return texts
        return []

    input_strings = strings(payload)
    result = {
        "request_sha256": digest(payload),
        "serialized_bytes": len(serialized.encode()),
        "stage": stage,
        "artifact_state": "MISSING" if artifact is None else "CAPTURED",
        "original_body_leaks": [],
    }
    if artifact is None:
        result["compact_l1_verified"] = None
        return result
    selected = {
        item.get("quote")
        for item in artifact.get("items", [])
        if isinstance(item.get("quote"), str)
    }
    for source in artifact.get("originals", []):
        body = source.get("content")
        if (
            isinstance(body, str)
            and body
            and body not in selected
            and any(body in text for text in input_strings)
        ):
            result["original_body_leaks"].append(source["ref"])
    result["compact_l1_verified"] = stage == "l1" and not result["original_body_leaks"]
    result["source_digest"] = artifact.get("source_digest")
    result["artifact_digest"] = artifact.get("artifact_digest")
    result["judge_originals_allowed"] = stage == "judge"
    return result
