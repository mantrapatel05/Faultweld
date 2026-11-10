"""Store-authoritative start and resume guards."""

from __future__ import annotations

from collections.abc import Callable

from app.workflow.graph import WorkflowGraph
from app.workflow.reasons import WORKFLOW_ALREADY_STARTED, WORKFLOW_NOT_PAUSED
from app.workflow.state import WorkflowState


class DuplicateWorkflowError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def start_incident(
    graph: WorkflowGraph,
    state: WorkflowState,
    *,
    thread_exists: Callable[[str], bool],
) -> WorkflowState:
    thread_id = state["thread_id"]
    if thread_exists(thread_id):
        raise DuplicateWorkflowError(WORKFLOW_ALREADY_STARTED)
    return graph.invoke(state, thread_id=thread_id)


def resume_incident(
    graph: WorkflowGraph,
    state: WorkflowState,
    *,
    is_awaiting_approval: Callable[[str], bool],
) -> WorkflowState:
    """Resume only when SQLite still says the incident is paused."""
    incident_id = state["incident_id"]
    if not is_awaiting_approval(incident_id):
        raise DuplicateWorkflowError(WORKFLOW_NOT_PAUSED)
    return graph.resume(thread_id=state["thread_id"])


__all__ = ["DuplicateWorkflowError", "resume_incident", "start_incident"]
