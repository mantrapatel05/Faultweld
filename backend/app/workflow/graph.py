"""Single approval-gated LangGraph orchestration path.

The graph stores only identifiers, counters and route names. Trusted services supplied by
the composition root own the database, investigator and gateway; graph nodes never receive a
writer or a mutable simulator.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.workflow.reasons import (
    APPROVAL_REJECTED,
    EXECUTION_REFUSED,
    EXECUTION_UNKNOWN,
    INVALID_PROPOSAL,
)
from app.workflow.state import GRAPH_NODES, WorkflowState


class WorkflowServices(Protocol):
    """Trusted, writer-free service seam used by graph nodes."""

    def intake(self, state: WorkflowState) -> Mapping[str, object]: ...

    def investigate(self, state: WorkflowState) -> Mapping[str, object]: ...

    def diagnose(self, state: WorkflowState) -> Mapping[str, object]: ...

    def propose(self, state: WorkflowState) -> Mapping[str, object]: ...

    def preflight(self, state: WorkflowState) -> Mapping[str, object]: ...

    def approval(self, proposal_id: str) -> str | None: ...

    def execute(self, proposal_id: str) -> str: ...

    def verify(self, state: WorkflowState) -> str: ...

    def close_or_escalate(self, state: WorkflowState) -> Mapping[str, object]: ...


class GraphIntrospection(Protocol):
    nodes: Mapping[str, object]


class CompiledWorkflow(Protocol):
    def invoke(self, input: object, config: object | None = None) -> object: ...

    def get_graph(self) -> GraphIntrospection: ...


def _append(state: WorkflowState, phase: str, updates: Mapping[str, object]) -> dict[str, object]:
    trail = list(state.get("trail", []))
    trail.append(phase)
    result: dict[str, object] = dict(updates)
    result["phase"] = updates.get("phase", phase)
    result["trail"] = trail
    return result


@dataclass(frozen=True)
class WorkflowGraph:
    """Compiled graph and its service seam."""

    compiled: CompiledWorkflow
    services: WorkflowServices

    def invoke(self, state: WorkflowState, *, thread_id: str) -> WorkflowState:
        result = self.compiled.invoke(
            state, {"configurable": {"thread_id": thread_id}}
        )
        return cast(WorkflowState, result)

    def resume(self, *, thread_id: str) -> WorkflowState:
        result = self.compiled.invoke(
            Command(resume=True), {"configurable": {"thread_id": thread_id}}
        )
        return cast(WorkflowState, result)


def build_graph(
    services: WorkflowServices,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
) -> WorkflowGraph:
    """Build the fixed topology with no automatic node retry."""
    builder = StateGraph(WorkflowState)

    def node(name: str, operation: Callable[[WorkflowState], Mapping[str, object]]):
        def run(state: WorkflowState) -> dict[str, object]:
            return _append(state, name, operation(state))

        return run

    builder.add_node("intake", node("intake", services.intake))
    builder.add_node("investigate", node("investigate", services.investigate))
    builder.add_node("diagnose", node("diagnose", services.diagnose))
    builder.add_node("propose", node("propose", services.propose))
    builder.add_node("preflight", node("preflight", services.preflight))

    def await_approval(state: WorkflowState) -> dict[str, object]:
        proposal_id = state.get("proposal_id")
        if not proposal_id:
            return _append(state, "await_approval", {"route": INVALID_PROPOSAL})
        interrupt({"proposal_id": proposal_id, "incident_id": state["incident_id"]})
        decision = services.approval(proposal_id)
        if decision == "approve":
            return _append(state, "await_approval", {"approval": decision, "route": "execute"})
        if decision == "reject":
            return _append(
                state,
                "await_approval",
                {"approval": decision, "reason": APPROVAL_REJECTED, "route": "close"},
            )
        return _append(state, "await_approval", {"route": "await_approval"})

    builder.add_node("await_approval", await_approval)

    def execute(state: WorkflowState) -> dict[str, object]:
        proposal_id = state.get("proposal_id")
        if not proposal_id:
            return _append(state, "execute", {"route": EXECUTION_REFUSED})
        reason = services.execute(proposal_id)
        route = "verify" if reason in {"executed", "replayed", "reconciled"} else "close"
        return _append(
            state,
            "execute",
            {
                "gateway_reason": reason,
                "route": route if reason != EXECUTION_UNKNOWN else EXECUTION_UNKNOWN,
            },
        )

    builder.add_node("execute", execute)
    builder.add_node(
        "verify",
        lambda state: _append(
            state, "verify", {"verification": services.verify(state), "route": "close"}
        ),
    )
    builder.add_node("close_or_escalate", node("close_or_escalate", services.close_or_escalate))

    builder.add_edge(START, "intake")
    builder.add_conditional_edges(
        "intake",
        lambda s: (
            "investigate"
            if s.get("route", "investigate") == "investigate"
            else "close_or_escalate"
        ),
        {"investigate": "investigate", "close_or_escalate": "close_or_escalate"},
    )
    builder.add_conditional_edges(
        "investigate",
        lambda s: "diagnose" if s.get("route", "diagnose") == "diagnose" else "close_or_escalate",
        {"diagnose": "diagnose", "close_or_escalate": "close_or_escalate"},
    )
    builder.add_conditional_edges(
        "diagnose",
        lambda s: "propose" if s.get("route", "propose") == "propose" else "close_or_escalate",
        {"propose": "propose", "close_or_escalate": "close_or_escalate"},
    )
    builder.add_edge("propose", "preflight")
    builder.add_conditional_edges(
        "preflight",
        lambda s: (
            "await_approval"
            if s.get("route", "await_approval") == "await_approval"
            else "close_or_escalate"
        ),
        {"await_approval": "await_approval", "close_or_escalate": "close_or_escalate"},
    )
    builder.add_conditional_edges(
        "await_approval",
        lambda s: cast(str, s.get("route", "await_approval")),
        {"await_approval": "await_approval", "execute": "execute", "close": "close_or_escalate"},
    )
    builder.add_conditional_edges(
        "execute",
        lambda s: "verify" if s.get("route") == "verify" else "close_or_escalate",
        {"verify": "verify", "close_or_escalate": "close_or_escalate"},
    )
    builder.add_edge("verify", "close_or_escalate")
    builder.add_edge("close_or_escalate", END)

    compiled = cast(
        CompiledWorkflow,
        builder.compile(checkpointer=checkpointer or InMemorySaver()),
    )
    return WorkflowGraph(compiled=compiled, services=services)


__all__ = ["GRAPH_NODES", "WorkflowGraph", "WorkflowServices", "build_graph"]
