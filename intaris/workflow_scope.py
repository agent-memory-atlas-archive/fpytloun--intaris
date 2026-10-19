"""Controller-declared configured scope, never a human permission source."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from intaris.hybrid import digest
from intaris.policy_activation import HybridError

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    origin: Literal["controller_workflow_step"]
    session_id: Identifier
    task_id: Identifier
    workflow_id: Identifier
    step_name: Identifier
    step_run_id: Identifier
    logical_call_id: Identifier
    workflow_definition_digest: Digest
    step_definition_digest: Digest
    rendered_args_digest: Digest
    tool: Identifier
    evaluated_args_digest: Digest
    executor_id: Identifier
    authorized_by: Literal["owner_configured", "agent_composed"]
    projection: Literal["exact", "unsupported"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_step_declarations (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    logical_call_id TEXT NOT NULL,
    declaration_digest TEXT NOT NULL,
    body TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    governing_execution_id TEXT NOT NULL,
    governing_snapshot_id TEXT NOT NULL,
    admission_revision INTEGER NOT NULL,
    PRIMARY KEY (user_id, session_id, logical_call_id),
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
        ON DELETE CASCADE
)
"""


def registry_revision(cur, user_id, session_id):
    cur.execute(
        "SELECT COALESCE(MAX(admission_revision), 0) AS revision "
        "FROM workflow_step_declarations WHERE user_id = ? AND session_id = ?",
        (user_id, session_id),
    )
    return cur.fetchone()["revision"]


class WorkflowScopeStore:
    def __init__(self, db):
        self.db = db

    def install(self, body: WorkflowStep, *, user_id, agent_id, claims):
        payload = body.model_dump()
        declaration_digest = digest(payload)
        bindings = (
            agent_id,
            claims["governing_execution_id"],
            claims["governing_snapshot_id"],
        )
        with self.db.cursor() as cur:
            # Serialize registry mutation with admission and authorization writers.
            cur.execute(
                "UPDATE sessions SET state_revision = state_revision "
                "WHERE user_id = ? AND session_id = ? AND agent_id = ?",
                (user_id, body.session_id, agent_id),
            )
            if cur.rowcount != 1:
                raise HybridError(
                    "workflow_step_conflict",
                    "Existing agent-bound session required",
                    409,
                )
            cur.execute(
                "SELECT * FROM workflow_step_declarations "
                "WHERE user_id = ? AND session_id = ? AND logical_call_id = ?",
                (user_id, body.session_id, body.logical_call_id),
            )
            old = cur.fetchone()
            if old:
                if old["declaration_digest"] != declaration_digest or bindings != (
                    old["agent_id"],
                    old["governing_execution_id"],
                    old["governing_snapshot_id"],
                ):
                    raise HybridError(
                        "workflow_step_conflict",
                        "Logical call already has a different declaration",
                        409,
                    )
                return {
                    "ok": True,
                    "declaration_digest": declaration_digest,
                    "admission_revision": old["admission_revision"],
                }
            cur.execute(
                "SELECT state_revision, policy FROM sessions WHERE user_id = ? AND session_id = ?",
                (user_id, body.session_id),
            )
            session = cur.fetchone()
            if (
                json.loads(session["policy"] or "{}").get("evaluation_policy_version")
                != "hybrid_v1"
            ):
                # Not a conflict: the session's engine does not support
                # declarations. 422 lets clients evaluate undeclared.
                raise HybridError(
                    "workflow_scope_unsupported",
                    "Workflow scope requires an existing hybrid session",
                    422,
                )
            revision = session["state_revision"]
            cur.execute(
                "UPDATE sessions SET state_revision = state_revision + 1 "
                "WHERE user_id = ? AND session_id = ? AND state_revision = ?",
                (user_id, body.session_id, revision),
            )
            if cur.rowcount != 1:
                raise HybridError(
                    "workflow_step_conflict", "Authorization revision changed", 409
                )
            revision += 1
            cur.execute(
                "INSERT INTO workflow_step_declarations "
                "(user_id, session_id, logical_call_id, declaration_digest, body, agent_id, "
                "governing_execution_id, governing_snapshot_id, admission_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    body.session_id,
                    body.logical_call_id,
                    declaration_digest,
                    json.dumps(payload, ensure_ascii=False),
                    *bindings,
                    revision,
                ),
            )
            return {
                "ok": True,
                "declaration_digest": declaration_digest,
                "admission_revision": revision,
            }

    def match(
        self,
        *,
        user_id,
        session_id,
        agent_id,
        logical_call_id,
        tool,
        args,
        controller_context,
    ):
        with self.db.cursor() as cur:
            revision = registry_revision(cur, user_id, session_id)
            cur.execute(
                "SELECT * FROM workflow_step_declarations "
                "WHERE user_id = ? AND session_id = ? AND logical_call_id = ?",
                (user_id, session_id, logical_call_id),
            )
            row = cur.fetchone()
        result = {
            "registry_revision": revision,
            "declared": bool(revision),
            "matched": False,
            "missing": False,
            "mismatched": False,
            "unsupported_projection": False,
            "reason_code": None,
        }
        if not revision:
            return result
        if row is None or not controller_context:
            result.update(missing=True, reason_code="workflow_step.declaration_missing")
            return result
        body = json.loads(row["body"])
        if body["projection"] != "exact":
            result.update(
                unsupported_projection=True,
                reason_code="workflow_step.argument_projection_unsupported",
            )
        elif body["authorized_by"] != "owner_configured":
            result.update(
                mismatched=True, reason_code="workflow_step.definition_unverified"
            )
        elif (
            row["agent_id"] != agent_id
            or body["session_id"] != session_id
            or body["tool"] != tool
            or body["logical_call_id"] != logical_call_id
            or body["evaluated_args_digest"] != digest(args)
            or body["executor_id"] != controller_context.get("executor_id")
            or any(
                row[key] != controller_context.get(key)
                for key in ("governing_execution_id", "governing_snapshot_id")
            )
        ):
            result.update(
                mismatched=True, reason_code="workflow_step.declaration_mismatch"
            )
        else:
            result.update(
                matched=True,
                declaration=body,
                declaration_digest=row["declaration_digest"],
            )
        return result
