"""The typed investigation: intake, diagnose, propose, done.

The investigator can read a snapshot and call an injected model provider. It has no writer,
no cluster handle and no tools. Every exit is either a validated ``Diagnosis`` plus a typed
``ActionProposal`` or a stable escalation reason. Limits are checked before every model
call; output that is produced over budget is discarded rather than used.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from app.agent.diagnosis import build_diagnosis
from app.agent.errors import AgentOutputError
from app.agent.evidence_view import snapshot_evidence
from app.agent.ids import IdFactory, uuid_id
from app.agent.proposal import build_proposal, parse_proposal
from app.agent.provider import (
    ModelEvidence,
    ModelProvider,
    ModelProviderError,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
)
from app.agent.reasons import (
    BUDGET_EXHAUSTED_MODEL_CALLS,
    BUDGET_EXHAUSTED_TOKENS,
    BUDGET_EXHAUSTED_WALL_CLOCK,
    INSUFFICIENT_EVIDENCE,
    NO_ACTION_PROPOSED,
    NO_EVIDENCE,
    provider_error_reason,
)
from app.domain.schemas import (
    ActionProposal,
    Diagnosis,
    DomainModel,
    NonEmptyId,
    NonNegativeInt,
)
from app.evidence.models import EvidenceContext, EvidenceItem
from app.sim.cluster import SimSnapshot

WORKFLOW_VERSION = "investigation-lite-1"


class InvestigationStatus(StrEnum):
    PROPOSED = "proposed"
    ESCALATED = "escalated"


class InvestigationBudget(BaseModel):
    """Hard limits for one investigation. All values are checked before each call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_model_calls: NonNegativeInt = 4
    max_tokens: NonNegativeInt = 8000
    max_wall_clock_seconds: NonNegativeInt = 120


class InvestigationResult(DomainModel):
    """The outcome of one investigation, deterministic for deterministic inputs."""

    status: InvestigationStatus
    diagnosis: Diagnosis | None = None
    proposal: ActionProposal | None = None
    escalation_reason: NonEmptyId | None = None
    model_calls: NonNegativeInt
    tokens_used: NonNegativeInt
    evidence_ids: tuple[NonEmptyId, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> InvestigationResult:
        if self.status is InvestigationStatus.PROPOSED:
            if self.diagnosis is None or self.proposal is None:
                raise ValueError("a proposed result needs a diagnosis and a proposal")
            if self.escalation_reason is not None:
                raise ValueError("a proposed result cannot carry an escalation reason")
        else:
            if self.escalation_reason is None:
                raise ValueError("an escalated result needs a reason")
            if self.proposal is not None:
                raise ValueError("an escalated result cannot carry a proposal")
        if (
            self.diagnosis is not None
            and self.proposal is not None
            and self.diagnosis.run_id != self.proposal.run_id
        ):
            raise ValueError("the diagnosis and proposal belong to different runs")
        return self


@dataclass
class _Usage:
    model_calls: int = 0
    tokens_used: int = 0


class _Escalation(Exception):
    """Internal control flow: stop the state machine with a stable reason."""

    def __init__(self, reason: str, diagnosis: Diagnosis | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.diagnosis = diagnosis


@dataclass(frozen=True)
class _Run:
    context: EvidenceContext
    items: tuple[EvidenceItem, ...]
    supplied_evidence_ids: frozenset[str]
    started_at: datetime
    observed_at: datetime
    usage: _Usage


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Investigator:
    """A deterministic state machine around one injected model provider."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        budgets: InvestigationBudget | None = None,
        now: Callable[[], datetime] = _utc_now,
        ids: IdFactory = uuid_id,
        max_correction_retries: int = 1,
    ) -> None:
        if max_correction_retries < 0:
            raise ValueError("max_correction_retries must not be negative")
        self._provider = provider
        self._budgets = budgets or InvestigationBudget()
        self._now = now
        self._ids = ids
        self._max_correction_retries = max_correction_retries

    def investigate(
        self,
        *,
        incident_id: str,
        run_id: str,
        snapshot: SimSnapshot | None = None,
        evidence: Sequence[EvidenceItem] = (),
    ) -> InvestigationResult:
        """Run intake -> diagnose -> propose -> done and return the typed outcome."""
        started_at = self._now()
        context = EvidenceContext(incident_id=incident_id, run_id=run_id)
        items: list[EvidenceItem] = []
        if snapshot is not None:
            items.append(snapshot_evidence(snapshot, context=context, observed_at=started_at))
        items.extend(evidence)
        run = _Run(
            context=context,
            items=tuple(items),
            supplied_evidence_ids=frozenset(item.record.evidence_id for item in items),
            started_at=started_at,
            observed_at=started_at,
            usage=_Usage(),
        )
        evidence_ids = tuple(item.record.evidence_id for item in run.items)
        diagnosis: Diagnosis | None = None
        try:
            if not run.items:
                raise _Escalation(NO_EVIDENCE)
            diagnosis = self._diagnose(run)
            if not diagnosis.is_sufficient:
                raise _Escalation(INSUFFICIENT_EVIDENCE, diagnosis)
            if snapshot is None:
                # Evidence exists but none of it describes live cluster state, so no action
                # can be grounded in a fingerprint or a server-resolved target.
                raise _Escalation(INSUFFICIENT_EVIDENCE, diagnosis)
            proposal = self._propose(run, snapshot)
            if proposal is None:
                raise _Escalation(NO_ACTION_PROPOSED, diagnosis)
            return self._result(
                InvestigationStatus.PROPOSED,
                run=run,
                evidence_ids=evidence_ids,
                diagnosis=diagnosis,
                proposal=proposal,
            )
        except _Escalation as escalation:
            return self._result(
                InvestigationStatus.ESCALATED,
                run=run,
                evidence_ids=evidence_ids,
                diagnosis=escalation.diagnosis or diagnosis,
                reason=escalation.reason,
            )

    def _request(self, purpose: ModelPurpose, run: _Run) -> ModelRequest:
        return ModelRequest(
            purpose=purpose,
            incident_id=run.context.incident_id,
            run_id=run.context.run_id,
            evidence=tuple(
                ModelEvidence(record=item.record, payload=item.payload) for item in run.items
            ),
        )

    def _diagnose(self, run: _Run) -> Diagnosis:
        request = self._request(ModelPurpose.DIAGNOSE, run)
        attempts = 0
        while True:
            response = self._call(request, run)
            try:
                return build_diagnosis(
                    response.output,
                    run_id=run.context.run_id,
                    evidence_ids=run.supplied_evidence_ids,
                    ids=self._ids,
                )
            except AgentOutputError as error:
                if attempts >= self._max_correction_retries:
                    raise _Escalation(error.reason) from error
                attempts += 1

    def _propose(self, run: _Run, snapshot: SimSnapshot) -> ActionProposal | None:
        request = self._request(ModelPurpose.PROPOSE, run)
        attempts = 0
        while True:
            response = self._call(request, run)
            try:
                draft = parse_proposal(response.output)
                return build_proposal(
                    draft,
                    context=run.context,
                    snapshot=snapshot,
                    observed_at=run.observed_at,
                    supplied_evidence_ids=run.supplied_evidence_ids,
                    ids=self._ids,
                )
            except AgentOutputError as error:
                if attempts >= self._max_correction_retries:
                    raise _Escalation(error.reason) from error
                attempts += 1

    def _call(self, request: ModelRequest, run: _Run) -> ModelResponse:
        reason = self._budget_reason(run)
        if reason is not None:
            raise _Escalation(reason)
        run.usage.model_calls += 1
        try:
            response = self._provider.complete(request)
        except ModelProviderError as error:
            raise _Escalation(provider_error_reason(error.code)) from error
        run.usage.tokens_used += response.usage.total_tokens
        if run.usage.tokens_used > self._budgets.max_tokens:
            raise _Escalation(BUDGET_EXHAUSTED_TOKENS)
        if self._elapsed(run) > self._budgets.max_wall_clock_seconds:
            raise _Escalation(BUDGET_EXHAUSTED_WALL_CLOCK)
        return response

    def _budget_reason(self, run: _Run) -> str | None:
        if self._elapsed(run) > self._budgets.max_wall_clock_seconds:
            return BUDGET_EXHAUSTED_WALL_CLOCK
        if run.usage.model_calls >= self._budgets.max_model_calls:
            return BUDGET_EXHAUSTED_MODEL_CALLS
        if run.usage.tokens_used >= self._budgets.max_tokens:
            return BUDGET_EXHAUSTED_TOKENS
        return None

    def _elapsed(self, run: _Run) -> float:
        return (self._now() - run.started_at).total_seconds()

    def _result(
        self,
        status: InvestigationStatus,
        *,
        run: _Run,
        evidence_ids: tuple[str, ...],
        diagnosis: Diagnosis | None = None,
        proposal: ActionProposal | None = None,
        reason: str | None = None,
    ) -> InvestigationResult:
        return InvestigationResult(
            schema_version="0.1",
            status=status,
            diagnosis=diagnosis,
            proposal=proposal,
            escalation_reason=reason,
            model_calls=run.usage.model_calls,
            tokens_used=run.usage.tokens_used,
            evidence_ids=evidence_ids,
        )


__all__ = [
    "InvestigationBudget",
    "InvestigationResult",
    "InvestigationStatus",
    "Investigator",
    "WORKFLOW_VERSION",
]
