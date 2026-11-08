"""The pure policy engine: first matching rule wins, and every rule is in one table.

``evaluate`` never reads a clock, a file, a model or a resource. The caller supplies ``now``
and a read-only snapshot; the same config and input always produce the same decision.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue

from app.domain.schemas import (
    ActionArguments,
    ActionType,
    IncidentStatus,
    NonEmptyId,
    NonNegativeInt,
    PolicyConstraints,
    PolicyDecision,
    PolicyDecisionType,
    ResourceRef,
    ScaleDeploymentArgs,
)
from app.evidence.canonical import canonical_json
from app.policy import reasons
from app.policy.config import PolicyConfig, policy_bundle_digest
from app.sim.cluster import SimSnapshot


class PolicyInput(BaseModel):
    """Trusted, fully resolved inputs to one deterministic policy evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    proposal_id: NonEmptyId | None
    action_type: ActionType | None
    arguments: ActionArguments | None
    target: ResourceRef | None
    scenario_id: NonEmptyId | None
    current_snapshot: SimSnapshot | None
    now: AwareDatetime | None
    proposal_created_at: AwareDatetime | None
    proposal_expires_at: AwareDatetime | None
    environment_id: NonEmptyId | None
    incident_status: IncidentStatus | None
    attempts_so_far: NonNegativeInt | None


@dataclass(frozen=True)
class PolicyRule:
    reason: str
    applies: Callable[[PolicyConfig, PolicyInput], bool]


def _missing_input(config: PolicyConfig, value: PolicyInput) -> bool:
    proposal_id = value.proposal_id
    action_type = value.action_type
    arguments = value.arguments
    target = value.target
    scenario_id = value.scenario_id
    snapshot = value.current_snapshot
    now = value.now
    created_at = value.proposal_created_at
    expires_at = value.proposal_expires_at
    environment_id = value.environment_id
    incident_status = value.incident_status
    attempts = value.attempts_so_far
    if (
        proposal_id is None
        or action_type is None
        or arguments is None
        or target is None
        or scenario_id is None
        or snapshot is None
        or now is None
        or created_at is None
        or expires_at is None
        or environment_id is None
        or incident_status is None
        or attempts is None
    ):
        return True
    if arguments.action_type != action_type:
        return True
    return now < created_at


def _environment_not_allowed(config: PolicyConfig, value: PolicyInput) -> bool:
    environment_id = value.environment_id
    return environment_id is None or environment_id not in config.allowed_environment_ids


def _namespace_not_allowed(config: PolicyConfig, value: PolicyInput) -> bool:
    target = value.target
    return target is None or target.namespace != config.allowed_namespace


def _target_not_allowed(config: PolicyConfig, value: PolicyInput) -> bool:
    target = value.target
    scenario_id = value.scenario_id
    if target is None or scenario_id is None:
        return False
    if target.kind != config.allowed_kind:
        return True
    return target.name not in config.scenario_targets.get(scenario_id, ())


def _target_uid_mismatch(config: PolicyConfig, value: PolicyInput) -> bool:
    target = value.target
    return target is None or target.uid is None or target.uid != config.target_uid


def _rollback_unsupported(config: PolicyConfig, value: PolicyInput) -> bool:
    return value.action_type is ActionType.ROLLBACK_DEPLOYMENT


def _action_not_allowed(config: PolicyConfig, value: PolicyInput) -> bool:
    action_type = value.action_type
    return action_type is not None and action_type not in config.action_allowlist


def _replicas_out_of_bounds(config: PolicyConfig, value: PolicyInput) -> bool:
    if value.action_type is not ActionType.SCALE_DEPLOYMENT:
        return False
    arguments = value.arguments
    if not isinstance(arguments, ScaleDeploymentArgs):
        return False
    return arguments.replicas < config.min_replicas or arguments.replicas > config.max_replicas


def _proposal_expired(config: PolicyConfig, value: PolicyInput) -> bool:
    now = value.now
    expires_at = value.proposal_expires_at
    if now is None or expires_at is None:
        return False
    return now >= expires_at


def _incident_closed(config: PolicyConfig, value: PolicyInput) -> bool:
    return value.incident_status is IncidentStatus.CLOSED


def _attempt_limit_reached(config: PolicyConfig, value: PolicyInput) -> bool:
    attempts = value.attempts_so_far
    return attempts is not None and attempts >= config.max_attempts


# Order is the contract. Missing input first; specific authorization checks before the
# generic action allow-list; time/incident/attempt checks last. Tests assert the exact code.
DEFAULT_RULES: tuple[PolicyRule, ...] = (
    PolicyRule(reasons.MISSING_INPUT, _missing_input),
    PolicyRule(reasons.ENVIRONMENT_NOT_ALLOWED, _environment_not_allowed),
    PolicyRule(reasons.NAMESPACE_NOT_ALLOWED, _namespace_not_allowed),
    PolicyRule(reasons.TARGET_NOT_ALLOWED, _target_not_allowed),
    PolicyRule(reasons.TARGET_UID_MISMATCH, _target_uid_mismatch),
    PolicyRule(reasons.ACTION_NOT_SUPPORTED, _rollback_unsupported),
    PolicyRule(reasons.ACTION_NOT_ALLOWED, _action_not_allowed),
    PolicyRule(reasons.REPLICAS_OUT_OF_BOUNDS, _replicas_out_of_bounds),
    PolicyRule(reasons.PROPOSAL_EXPIRED, _proposal_expired),
    PolicyRule(reasons.INCIDENT_CLOSED, _incident_closed),
    PolicyRule(reasons.ATTEMPT_LIMIT_REACHED, _attempt_limit_reached),
)


def _material(
    config: PolicyConfig,
    value: PolicyInput,
    decision: PolicyDecisionType,
    reason_codes: tuple[str, ...],
) -> dict[str, JsonValue]:
    arguments: JsonValue = None
    if value.arguments is not None:
        dump = getattr(value.arguments, "model_dump", None)
        arguments = dump(mode="json") if callable(dump) else {"invalid": True}
    target: JsonValue = None
    if value.target is not None:
        target = value.target.model_dump(mode="json")
    material: dict[str, JsonValue] = {
        "policy_version": config.policy_version,
        "policy_bundle_digest": policy_bundle_digest(config),
        "decision": decision.value,
        "reason_codes": list(reason_codes),
        "proposal_id": value.proposal_id,
        "action_type": (
            value.action_type.value
            if isinstance(value.action_type, ActionType)
            else str(value.action_type) if value.action_type is not None else None
        ),
        "arguments": arguments,
        "target": target,
        "scenario_id": value.scenario_id,
        "state_fingerprint": (
            value.current_snapshot.state_fingerprint
            if value.current_snapshot is not None
            else None
        ),
        "now": value.now.isoformat() if value.now is not None else None,
        "proposal_created_at": (
            value.proposal_created_at.isoformat()
            if value.proposal_created_at is not None
            else None
        ),
        "proposal_expires_at": (
            value.proposal_expires_at.isoformat()
            if value.proposal_expires_at is not None
            else None
        ),
        "environment_id": value.environment_id,
        "incident_status": (
            value.incident_status.value if value.incident_status is not None else None
        ),
        "attempts_so_far": value.attempts_so_far,
    }
    return material


def _decision_id(
    config: PolicyConfig,
    value: PolicyInput,
    decision: PolicyDecisionType,
    reason_codes: tuple[str, ...],
) -> str:
    digest = hashlib.sha256(
        canonical_json(_material(config, value, decision, reason_codes)).encode("utf-8")
    ).hexdigest()
    return f"pd-{digest[:24]}"


def _constraints(config: PolicyConfig) -> PolicyConstraints:
    return PolicyConstraints(
        schema_version="0.1",
        allowed_namespace=config.allowed_namespace,
        allowed_resource=config.allowed_resource,
        min_replicas=config.min_replicas,
        max_replicas=config.max_replicas,
        max_attempts=config.max_attempts,
    )


def _decision(
    config: PolicyConfig,
    value: PolicyInput,
    decision: PolicyDecisionType,
    reason_codes: tuple[str, ...],
    *,
    required_approval_roles: tuple[str, ...] = (),
) -> PolicyDecision:
    return PolicyDecision(
        schema_version="0.1",
        decision_id=_decision_id(config, value, decision, reason_codes),
        proposal_id=(
            value.proposal_id if value.proposal_id is not None else "missing-proposal"
        ),
        decision=decision,
        reason_codes=reason_codes,
        policy_version=config.policy_version,
        policy_bundle_digest=policy_bundle_digest(config),
        constraints=_constraints(config),
        required_approval_roles=required_approval_roles,
    )


def evaluate(config: PolicyConfig, value: PolicyInput) -> PolicyDecision:
    """Classify one proposal. Pure: no clock, I/O, model or mutable state."""
    for rule in DEFAULT_RULES:
        if rule.applies(config, value):
            return _decision(
                config, value, PolicyDecisionType.DENY, (rule.reason,)
            )
    if config.require_approval:
        return _decision(
            config,
            value,
            PolicyDecisionType.REQUIRE_APPROVAL,
            (reasons.APPROVAL_REQUIRED,),
            required_approval_roles=config.required_approval_roles,
        )
    return _decision(config, value, PolicyDecisionType.ALLOW, (reasons.ALLOWED,))


__all__ = ["DEFAULT_RULES", "PolicyRule", "evaluate"]
