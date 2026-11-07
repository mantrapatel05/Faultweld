"""Parse and validate the model's diagnosis into a typed domain object.

Model JSON is untrusted input. It is parsed into closed pydantic models (unknown fields are
rejected, scalar types are strict), then every cited evidence ID is checked against the
evidence that was actually supplied. Only then does deterministic code build the domain
``Diagnosis``: IDs are server-generated and confidence is informational and clamped.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictStr,
    ValidationError,
)

from app.agent.errors import AgentOutputError
from app.agent.ids import IdFactory
from app.agent.reasons import INVALID_MODEL_OUTPUT, UNKNOWN_EVIDENCE_ID
from app.domain.schemas import Diagnosis, DiagnosisHypothesis, NonEmptyId

MAX_STATEMENT_LENGTH = 500


class ModelHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    statement: StrictStr = Field(min_length=1, max_length=MAX_STATEMENT_LENGTH)
    confidence: StrictFloat
    supporting_evidence_ids: list[NonEmptyId]
    contradicting_evidence_ids: list[NonEmptyId] = Field(default_factory=list)


class ModelDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    hypotheses: list[ModelHypothesis] = Field(min_length=1)
    missing_evidence: list[NonEmptyId] = Field(default_factory=list)
    confidence: StrictFloat
    is_sufficient: StrictBool


def clamp_confidence(value: float) -> float:
    """Confidence is informational; anything outside [0, 1] is clamped, not trusted."""
    return min(1.0, max(0.0, value))


def _require_supplied(
    evidence_ids: Collection[str], supplied: frozenset[str], kind: str
) -> None:
    for evidence_id in evidence_ids:
        if evidence_id not in supplied:
            raise AgentOutputError(
                UNKNOWN_EVIDENCE_ID,
                f"the model cited {kind} evidence {evidence_id!r} that was not supplied",
            )


def build_diagnosis(
    output: Mapping[str, JsonValue],
    *,
    run_id: str,
    evidence_ids: Collection[str],
    ids: IdFactory,
) -> Diagnosis:
    """Validate a diagnosis response and build the domain object.

    Raises :class:`AgentOutputError` with a stable reason for any invalid output.
    """
    try:
        draft = ModelDiagnosis.model_validate(dict(output))
    except ValidationError as error:
        raise AgentOutputError(
            INVALID_MODEL_OUTPUT, "the diagnosis output does not match its schema"
        ) from error

    supplied = frozenset(evidence_ids)
    hypotheses: list[DiagnosisHypothesis] = []
    for hypothesis in draft.hypotheses:
        _require_supplied(hypothesis.supporting_evidence_ids, supplied, "supporting")
        _require_supplied(hypothesis.contradicting_evidence_ids, supplied, "contradicting")
        hypotheses.append(
            DiagnosisHypothesis(
                schema_version="0.1",
                hypothesis_id=ids("hypothesis"),
                statement=hypothesis.statement,
                confidence=clamp_confidence(hypothesis.confidence),
                supporting_evidence_ids=tuple(hypothesis.supporting_evidence_ids),
                contradicting_evidence_ids=tuple(hypothesis.contradicting_evidence_ids),
            )
        )
    return Diagnosis(
        schema_version="0.1",
        diagnosis_id=ids("diagnosis"),
        run_id=run_id,
        hypotheses=tuple(hypotheses),
        missing_evidence=tuple(draft.missing_evidence),
        confidence=clamp_confidence(draft.confidence),
        is_sufficient=draft.is_sufficient,
    )


# Kept out of the public surface but imported by tests that exercise value bounds.
__all__ = ["MAX_STATEMENT_LENGTH", "ModelDiagnosis", "ModelHypothesis", "build_diagnosis"]
