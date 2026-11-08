"""The policy bundle: every rule that decides whether a proposal may proceed.

The bundle is frozen and hashed as a whole. Changing any rule changes both
``policy_bundle_digest`` and every decision made under the new bundle, which is exactly what
an approval binds to.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    field_serializer,
    model_validator,
)

from app.agent.identity import (
    DEPLOYMENT_KIND,
    DEPLOYMENT_NAME,
    DEPLOYMENT_UID,
    ENVIRONMENT_ID,
    NAMESPACE,
)
from app.domain.schemas import ActionType, NonEmptyId
from app.evidence.canonical import canonical_json

POLICY_VERSION = "policy-lite-1"
DEFAULT_PROPOSAL_TTL_SECONDS = 15 * 60
DEFAULT_APPROVAL_TTL_SECONDS = 15 * 60
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_REQUIRED_APPROVAL_ROLES = ("reviewer",)


def _default_scenario_targets() -> dict[str, tuple[str, ...]]:
    return {
        "healthy-replica-loss": (DEPLOYMENT_NAME,),
        "readiness-regression": (DEPLOYMENT_NAME,),
        "deployment-unavailable": (DEPLOYMENT_NAME,),
    }


class PolicyConfig(BaseModel):
    """One reviewable bundle of rules. ``frozen=True`` so no rule changes mid-request.

    ``require_approval=False`` exists so tests can exercise the engine's ``ALLOW`` branch.
    No default bundle uses it: every allowed action in Lite requires a human.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_environment_ids: tuple[NonEmptyId, ...] = Field(
        default=(ENVIRONMENT_ID,), min_length=1
    )
    allowed_namespace: NonEmptyId = NAMESPACE
    allowed_kind: NonEmptyId = DEPLOYMENT_KIND
    allowed_resource: NonEmptyId = DEPLOYMENT_NAME
    target_uid: NonEmptyId = DEPLOYMENT_UID
    scenario_targets: Mapping[NonEmptyId, tuple[NonEmptyId, ...]] = Field(
        default_factory=_default_scenario_targets
    )
    action_allowlist: tuple[ActionType, ...] = (
        ActionType.RESTART_DEPLOYMENT,
        ActionType.SCALE_DEPLOYMENT,
    )
    min_replicas: NonNegativeInt = 1
    max_replicas: NonNegativeInt = 1
    proposal_ttl_seconds: PositiveInt = DEFAULT_PROPOSAL_TTL_SECONDS
    approval_ttl_seconds: PositiveInt = DEFAULT_APPROVAL_TTL_SECONDS
    max_attempts: PositiveInt = DEFAULT_MAX_ATTEMPTS
    required_approval_roles: tuple[NonEmptyId, ...] = DEFAULT_REQUIRED_APPROVAL_ROLES
    policy_version: NonEmptyId = POLICY_VERSION
    require_approval: bool = True

    @model_validator(mode="after")
    def validate_bundle(self) -> PolicyConfig:
        if self.min_replicas > self.max_replicas:
            raise ValueError("min_replicas cannot exceed max_replicas")
        if not self.scenario_targets:
            raise ValueError("scenario_targets must not be empty")
        if any(not targets for targets in self.scenario_targets.values()):
            raise ValueError("every scenario target allow-list must be non-empty")
        if self.require_approval and not self.required_approval_roles:
            raise ValueError("requiring approval needs at least one approver role")
        object.__setattr__(self, "scenario_targets", MappingProxyType(dict(self.scenario_targets)))
        return self

    @field_serializer("scenario_targets")
    def serialize_scenario_targets(
        self, targets: Mapping[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        return dict(targets)


def policy_bundle_digest(config: PolicyConfig) -> str:
    """SHA-256 over the canonical bundle, so any rule change invalidates approvals."""
    return hashlib.sha256(
        canonical_json(config.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PROPOSAL_TTL_SECONDS",
    "DEFAULT_REQUIRED_APPROVAL_ROLES",
    "POLICY_VERSION",
    "PolicyConfig",
    "policy_bundle_digest",
]
