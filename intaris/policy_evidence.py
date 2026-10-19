"""Evidence boundary: model inferences never replace controller observations."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AUTHORITY_FIELDS = frozenset(
    {"authorization", "authorization_source", "authorization_scope"}
)


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Observation(Frozen):
    value: Any
    source: str
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    references: tuple[str, ...] = ()


class Obligation(Frozen):
    id: str
    basis: str
    fields: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    requirement: Literal["assessment", "independent_review", "human"] = "assessment"
    protected: bool = False


class CorrectedAssessment(Frozen):
    """Optional factual Judge reassessment; absence never invents scope."""

    relationship: (
        Literal[
            "directly_serves_intent",
            "supports_intent",
            "unrelated",
            "conflicts_with_intent",
            "unclear",
        ]
        | None
    ) = None
    contextual_restrictions: Literal["consistent", "conflicts", "unresolved"] | None = (
        None
    )
    restriction_reference: str | None = None
    target_scope: Literal["consistent", "conflicts", "unresolved"] | None = None
    target_reference: str | None = None
    authorization: Literal["explicit", "implicit", "absent", "unclear"] | None = None
    authorization_scope: Literal["within_scope", "outside_scope", "unclear"] | None = (
        None
    )
    authorization_source: str | None = None

    def validated_groups(
        self, state: "Evidence", evidence_refs: list[str]
    ) -> list[dict]:
        """Project coherent corrections without borrowing old status or witnesses.

        Shared by live receipt construction and stored receipt application.
        Contradicting a protected observed member rejects its whole group;
        matching witnesses still permit correction of a model interpretation.
        """
        groups = []
        if self.relationship is not None:
            groups.append({"relationship": self.relationship})
        restrictions = set(state.sources) | {
            clause["id"] for clause in state.advisory_clauses
        }
        targets = {"action"} | {
            ref
            for observation in state.observations.values()
            for ref in observation.references
        }
        for status, reference, admissible in (
            ("target_scope", "target_reference", targets),
            ("contextual_restrictions", "restriction_reference", restrictions),
        ):
            value, ref = getattr(self, status), getattr(self, reference)
            if value is not None and ref in admissible:
                groups.append({status: value, reference: ref})
        if self.authorization in {"absent", "unclear"}:
            # Never inherit positive authorization from a negative reassessment.
            groups.append(
                {
                    "authorization": self.authorization,
                    "authorization_source": "none",
                    "authorization_scope": "unclear",
                }
            )
        elif self.authorization in {"explicit", "implicit"}:
            source = state.sources.get(self.authorization_source, {})
            if (
                self.authorization_source in evidence_refs
                and self.authorization_scope is not None
                and source.get("independent_grant") is True
                and source.get("origin")
                in {
                    "client_user_claim",
                    "recorded_client_user_message",
                    "recorded_user_event",
                    "human_resolution",
                }
            ):
                groups.append(
                    {
                        "authorization": self.authorization,
                        "authorization_source": self.authorization_source,
                        "authorization_scope": self.authorization_scope,
                    }
                )
        return [
            group
            for group in groups
            if all(
                key not in state.observations
                or state.observations[key].value in (None, "unknown", "unclear")
                or state.observations[key].value == value
                for key, value in group.items()
            )
        ]


class Review(Frozen):
    """Controller-owned receipt, never a field in a model output schema."""

    obligation_ids: list[str]
    evidence_refs: list[str]
    # Read compatibility for persisted builtin-v7 audits only; v8 policy ignores
    # these removed second-checker fields. Drop after audit format migration.
    scope_support_refs: list[str] = Field(default_factory=list)
    routine_scope_verified: bool = False
    user_would: Literal["approve", "ask", "refuse"] | None = None
    risk: Literal["low", "medium", "high", "critical"] | None = None
    basis: str | None = None
    # Old persisted receipts have no factual corrections: retain their scope
    # uncertainty instead of deriving alignment or permission from a vote.
    corrected_assessment: CorrectedAssessment | None = None
    # Persisted-format compatibility only: pending escalations recorded before
    # advisory clauses became L1 context re-validate their stored Evidence.
    # Remove once no pre-change hybrid escalation can still be resolved.
    operator_policy_ids: list[str] = Field(default_factory=list)


class Evidence(Frozen):
    observations: dict[str, Observation]
    assessment: dict[str, Any] = Field(default_factory=dict)
    confidences: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, dict] = Field(default_factory=dict)
    review: Review | None = None
    human_approved: bool = False
    read_only_required: bool = False
    assessment_failed: bool = False
    advisory_clauses: list[dict] = Field(default_factory=list)
    workflow_scope: dict | None = None
    process_lifecycle: dict | None = None

    def value(self, key: str, default=None):
        if (
            key == "risk"
            and self.review is not None
            and self.review.obligation_ids
            and self.review.risk is not None
        ):
            # Severity is a judgment, including when a static heuristic supplied
            # it. The validated Judge receipt owns the final interpretation.
            # Keep the original observation and L1 assessment intact for audit.
            return self.review.risk
        if key == "mutation" and self.assessment.get("mutation") == "mutating":
            return "mutating"
        if key == "effects":
            observed = self.observations.get(key)
            effects = set(observed.value if observed else []) | set(
                self.assessment.get(key, [])
            )
            if (
                "effects" in self.assessment
                and "unknown" not in self.assessment["effects"]
            ):
                effects.discard("unknown")
            if effects:
                return sorted(effects)
        if key == "mutation" and set(self.value("effects", [])) & {
            "write",
            "delete",
            "transmit",
        }:
            return "mutating"
        observation = self.observations.get(key)
        if observation is not None and observation.value not in (
            None,
            "unknown",
            "unclear",
            ["unknown"],
        ):
            return observation.value
        if (
            key == "mutation"
            and self.assessment.get("mutation") in (None, "unclear")
            and self.assessment.get("effects")
            and set(self.value("effects", ["unknown"])) <= {"read", "execute"}
            and self.value("risk") == "low"
        ):
            # L1 found no write, delete or external effect at low risk.
            return "read_only"
        return self.assessment.get(key, default)

    def known(self, key: str) -> bool:
        observation = self.observations.get(key)
        return observation is not None and observation.coverage == "complete"

    def comparison(self) -> dict:
        """Ephemeral rule input; never persisted as authoritative evidence."""
        return {
            key: self.value(key)
            for key in set(self.assessment) | set(self.observations)
        }


def from_observations(values: dict, *, sources=None, read_only=False) -> Evidence:
    observations = {}
    for key, value in values.items():
        coverage = (
            "unknown"
            if value in (None, "unknown", "unclear", ["unknown"])
            else "complete"
        )
        if key == "paths":
            coverage = "complete" if values.get("paths_complete") else "partial"
        if key == "effects" and "unknown" in value:
            coverage = "partial"
        if key == "arguments" and values.get("arguments_complete") is False:
            coverage = "partial"
        observations[key] = Observation(
            value=value,
            source="secret_argument"
            if values.get("secret_argument")
            and key
            in {"effects", "risk", "operation", "static_read", "secret_argument"}
            else "name_heuristic"
            if values.get("name_heuristic_effect")
            and key in {"effects", "mutation", "name_heuristic_effect"}
            else "tool_contract"
            if values.get("operation_contract") and key in {"mutation", "effects"}
            else "runtime_context"
            if key == "runtime_context"
            else "static_observation",
            coverage=coverage,
            references=tuple(f"target:{index}" for index, _ in enumerate(value))
            if key == "paths"
            else ("action",),
        )
    return Evidence(
        observations=observations, sources=sources or {}, read_only_required=read_only
    )


def fields_for(predicate) -> set[str]:
    if predicate.children:
        return set().union(*(fields_for(child) for child in predicate.children))
    return {
        "path_matches": {"paths", "paths_complete"},
        "tool_matches": {"tool"},
        "command_matches": {"command"},
    }.get(predicate.op, {predicate.field} if predicate.field else set())


def audit_evidence(state: Evidence) -> dict:
    from intaris.redactor import redact

    payload = state.model_dump()
    payload["observations"] = redact(payload["observations"])
    payload["sources"] = redact(payload["sources"])
    return payload
