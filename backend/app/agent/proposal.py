"""Build a typed action proposal from a closed model draft.

The model supplies only the action type, the action's parameters, a bounded rationale and
the IDs of evidence it relied on. Deterministic code fills every field that carries
authority: identifiers, principal, environment, server-resolved target, timestamps, state
fingerprint, policy placeholders, postconditions, digests and the idempotency key.

The rationale is validated (bounded, non-empty) but deliberately not persisted: free text
written by a model must never become a field that later code trusts. A draft that is
malformed -- including an oversized rationale -- is ``invalid_model_output``; a well-formed
draft whose action parameters are invalid is ``invalid_parameters``. Parameter *bounds*
belong to policy in commit 4; here only the type and non-negativity are checked.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    ValidationError,
)

from app.agent.errors import AgentOutputError
from app.agent.identity import (
    CLUSTER_ID,
    DEPLOYMENT_KIND,
    DEPLOYMENT_NAME,
    DEPLOYMENT_UID,
    ENVIRONMENT_ID,
    NAMESPACE,
)
from app.agent.ids import IdFactory
from app.agent.reasons import (
    INVALID_MODEL_OUTPUT,
    INVALID_PARAMETERS,
    UNKNOWN_EVIDENCE_ID,
    UNSUPPORTED_ACTION,
)
from app.domain.schemas import (
    ActionArguments,
    ActionProposal,
    ActionType,
    NonEmptyId,
    ResourceRef,
    RestartDeploymentArgs,
    ScaleDeploymentArgs,
)
from app.evidence.canonical import canonical_json
from app.evidence.models import EvidenceContext
from app.sim.cluster import SimSnapshot

MAX_RATIONALE_LENGTH = 500
PROPOSAL_TTL = timedelta(minutes=15)

# TODO(commit 4): replace these placeholders with the evaluated policy's version and
# bundle digest. They are constants so no model output can choose them.
POLICY_VERSION_PLACEHOLDER = "policy-lite-pending"
POLICY_BUNDLE_DIGEST_PLACEHOLDER = "0" * 64
ATTEMPT_LIMIT_PLACEHOLDER = 1

EXPECTED_POSTCONDITIONS: Mapping[ActionType, tuple[str, ...]] = {
    ActionType.RESTART_DEPLOYMENT: ("pods-ready",),
    ActionType.SCALE_DEPLOYMENT: ("deployment-available", "pods-ready"),
    ActionType.ROLLBACK_DEPLOYMENT: ("deployment-revision-restored",),
}


class ModelProposal(BaseModel):
    """The only proposal fields a model may write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_type: ActionType | None
    parameters: dict[str, JsonValue]
    rationale: StrictStr = Field(min_length=1, max_length=MAX_RATIONALE_LENGTH)
    evidence_ids: list[NonEmptyId] = Field(default_factory=list)


def parse_proposal(output: Mapping[str, JsonValue]) -> ModelProposal:
    """Parse the proposal response. Unknown fields, including server-owned ones, fail."""
    try:
        return ModelProposal.model_validate(dict(output))
    except ValidationError as error:
        raise AgentOutputError(
            INVALID_MODEL_OUTPUT, "the proposal output does not match its schema"
        ) from error


def _target() -> ResourceRef:
    """The target is chosen by this server, never by the model."""
    return ResourceRef(
        schema_version="0.1",
        cluster_id=CLUSTER_ID,
        namespace=NAMESPACE,
        kind=DEPLOYMENT_KIND,
        name=DEPLOYMENT_NAME,
        uid=DEPLOYMENT_UID,
    )


def _typed_arguments(
    action_type: ActionType, parameters: Mapping[str, JsonValue]
) -> ActionArguments:
    if action_type is ActionType.ROLLBACK_DEPLOYMENT:
        raise AgentOutputError(UNSUPPORTED_ACTION, "rollback is not supported in Lite")
    if action_type is ActionType.RESTART_DEPLOYMENT:
        if parameters:
            raise AgentOutputError(
                INVALID_PARAMETERS, "restart_deployment takes no parameters"
            )
        return RestartDeploymentArgs(schema_version="0.1", target=_target())
    if action_type is ActionType.SCALE_DEPLOYMENT:
        if set(parameters) != {"replicas"}:
            raise AgentOutputError(
                INVALID_PARAMETERS, "scale_deployment requires exactly a replicas parameter"
            )
        replicas = parameters["replicas"]
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
            raise AgentOutputError(
                INVALID_PARAMETERS, "replicas must be a non-negative integer"
            )
        return ScaleDeploymentArgs(
            schema_version="0.1", target=_target(), replicas=replicas
        )
    raise AgentOutputError(UNSUPPORTED_ACTION, "the action is not supported in Lite")


def _require_supplied(evidence_ids: Collection[str], supplied: frozenset[str]) -> None:
    for evidence_id in evidence_ids:
        if evidence_id not in supplied:
            raise AgentOutputError(
                UNKNOWN_EVIDENCE_ID,
                f"the proposal cited evidence {evidence_id!r} that was not supplied",
            )


def canonical_parameter_digest(
    action_type: ActionType, arguments: ActionArguments
) -> str:
    """SHA-256 over the action type and its typed arguments, canonically encoded."""
    material: dict[str, JsonValue] = {
        "action_type": action_type.value,
        "arguments": arguments.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def proposal_digest(proposal: ActionProposal) -> str:
    """SHA-256 over the whole proposal. Commit 4 binds approvals to this value."""
    return hashlib.sha256(
        canonical_json(proposal.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def bind_policy_metadata(
    proposal: ActionProposal,
    *,
    policy_version: str,
    bundle_digest: str,
    proposal_ttl_seconds: int,
    attempt_limit: int,
) -> ActionProposal:
    """Bind a typed proposal to trusted policy values at the composition root.

    The investigator stays independent of the policy package. This function accepts only
    values supplied by trusted application code and replaces the commit 3 placeholders
    before the proposal is evaluated or persisted.
    """
    return proposal.model_copy(
        update={
            "policy_version": policy_version,
            "policy_bundle_digest": bundle_digest,
            "expires_at": proposal.created_at + timedelta(seconds=proposal_ttl_seconds),
            "attempt_limit": attempt_limit,
        }
    )


def build_proposal(
    draft: ModelProposal,
    *,
    context: EvidenceContext,
    snapshot: SimSnapshot,
    observed_at: datetime,
    supplied_evidence_ids: Collection[str],
    ids: IdFactory,
) -> ActionProposal | None:
    """Build a proposal, or return ``None`` when the model proposed no action.

    Raises :class:`AgentOutputError` with a stable reason for any invalid draft.
    """
    if draft.action_type is None:
        return None
    _require_supplied(draft.evidence_ids, frozenset(supplied_evidence_ids))
    arguments = _typed_arguments(draft.action_type, draft.parameters)
    proposal_id = ids("proposal")
    return ActionProposal(
        schema_version="0.1",
        proposal_id=proposal_id,
        incident_id=context.incident_id,
        run_id=context.run_id,
        principal_id=f"agent-run-{context.run_id}",
        environment_id=ENVIRONMENT_ID,
        action_type=draft.action_type,
        arguments=arguments,
        created_at=observed_at,
        expires_at=observed_at + PROPOSAL_TTL,
        expected_state_fingerprint=snapshot.state_fingerprint,
        state_observed_at=observed_at,
        policy_version=POLICY_VERSION_PLACEHOLDER,
        policy_bundle_digest=POLICY_BUNDLE_DIGEST_PLACEHOLDER,
        source_evidence_ids=tuple(draft.evidence_ids),
        expected_postconditions=EXPECTED_POSTCONDITIONS[draft.action_type],
        canonical_parameter_digest=canonical_parameter_digest(draft.action_type, arguments),
        idempotency_key=(
            "idem-" + hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()
        ),
        attempt_limit=ATTEMPT_LIMIT_PLACEHOLDER,
    )
