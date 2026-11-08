"""Who may decide. Principals are resolved server-side; roles never arrive in a request."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.domain.schemas import NonEmptyId

LOCAL_DEMO_USER_ID = "local-demo-user"
LOCAL_OBSERVER_ID = "local-observer"
AGENT_PRINCIPAL_PREFIX = "agent-run-"

REVIEWER_ROLE = "reviewer"
OBSERVER_ROLE = "observer"
AGENT_ROLE = "agent"


class PrincipalKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    principal_id: NonEmptyId
    role: NonEmptyId
    kind: PrincipalKind


LOCAL_DEMO_USER = Principal(
    principal_id=LOCAL_DEMO_USER_ID,
    role=REVIEWER_ROLE,
    kind=PrincipalKind.HUMAN,
)
LOCAL_OBSERVER = Principal(
    principal_id=LOCAL_OBSERVER_ID,
    role=OBSERVER_ROLE,
    kind=PrincipalKind.HUMAN,
)


def agent_principal(run_id: str) -> Principal:
    """The identity of the agent that produced a proposal for ``run_id``."""
    if not run_id:
        raise ValueError("run_id must not be empty")
    return Principal(
        principal_id=f"{AGENT_PRINCIPAL_PREFIX}{run_id}",
        role=AGENT_ROLE,
        kind=PrincipalKind.AGENT,
    )


def known_principal(principal_id: str) -> Principal | None:
    """Resolve one of the principals this local demo knows about.

    Production replaces this with an identity provider. The important property is that the
    caller supplies only an ID; the role always comes from this server-side table.
    """
    if principal_id == LOCAL_DEMO_USER_ID:
        return LOCAL_DEMO_USER
    if principal_id == LOCAL_OBSERVER_ID:
        return LOCAL_OBSERVER
    if principal_id.startswith(AGENT_PRINCIPAL_PREFIX):
        return agent_principal(principal_id.removeprefix(AGENT_PRINCIPAL_PREFIX))
    return None


__all__ = [
    "AGENT_PRINCIPAL_PREFIX",
    "AGENT_ROLE",
    "LOCAL_DEMO_USER",
    "LOCAL_DEMO_USER_ID",
    "LOCAL_OBSERVER",
    "LOCAL_OBSERVER_ID",
    "OBSERVER_ROLE",
    "Principal",
    "PrincipalKind",
    "REVIEWER_ROLE",
    "agent_principal",
    "known_principal",
]
