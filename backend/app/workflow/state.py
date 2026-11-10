"""JSON-only state carried by the orchestration graph."""

from __future__ import annotations

from typing import TypedDict


class WorkflowState(TypedDict, total=False):
    incident_id: str
    run_id: str
    thread_id: str
    proposal_id: str
    phase: str
    route: str
    model_calls: int
    tool_calls: int
    cycles: int
    reason: str
    approval: str
    gateway_reason: str
    verification: str
    trail: list[str]


GRAPH_NODES = (
    "intake",
    "investigate",
    "diagnose",
    "propose",
    "preflight",
    "await_approval",
    "execute",
    "verify",
    "close_or_escalate",
)
