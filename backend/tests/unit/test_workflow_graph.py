from __future__ import annotations

from collections.abc import Mapping

from app.workflow.graph import WorkflowServices, build_graph
from app.workflow.state import GRAPH_NODES, WorkflowState


class FakeServices(WorkflowServices):
    def __init__(self) -> None:
        self.decision: str | None = None
        self.executions = 0

    def intake(self, state: WorkflowState) -> Mapping[str, object]:
        return {"route": "investigate"}

    def investigate(self, state: WorkflowState) -> Mapping[str, object]:
        return {"route": "diagnose", "model_calls": 1}

    def diagnose(self, state: WorkflowState) -> Mapping[str, object]:
        return {"route": "propose"}

    def propose(self, state: WorkflowState) -> Mapping[str, object]:
        return {"proposal_id": "proposal-1"}

    def preflight(self, state: WorkflowState) -> Mapping[str, object]:
        return {"route": "await_approval", "phase": "await_approval"}

    def approval(self, proposal_id: str) -> str | None:
        return self.decision

    def execute(self, proposal_id: str) -> str:
        self.executions += 1
        return "executed"

    def verify(self, state: WorkflowState) -> str:
        return "recovered"

    def close_or_escalate(self, state: WorkflowState) -> Mapping[str, object]:
        return {"route": "closed"}


def test_graph_has_exact_topology_and_pauses() -> None:
    services = FakeServices()
    workflow = build_graph(services)
    graph_nodes = set(workflow.compiled.get_graph().nodes) - {"__start__", "__end__"}
    assert graph_nodes == set(GRAPH_NODES)

    paused = workflow.invoke(
        {"incident_id": "incident-1", "run_id": "run-1", "thread_id": "thread-1"},
        thread_id="thread-1",
    )
    assert paused["phase"] == "await_approval"
    assert paused["proposal_id"] == "proposal-1"
    assert services.executions == 0


def test_resume_reads_approval_and_executes_once() -> None:
    services = FakeServices()
    workflow = build_graph(services)
    workflow.invoke(
        {"incident_id": "incident-1", "run_id": "run-1", "thread_id": "thread-1"},
        thread_id="thread-1",
    )
    services.decision = "approve"

    result = workflow.resume(thread_id="thread-1")

    assert result["gateway_reason"] == "executed"
    assert result["verification"] == "recovered"
    assert services.executions == 1
