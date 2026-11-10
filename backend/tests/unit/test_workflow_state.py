from __future__ import annotations

import json

from app.workflow.state import WorkflowState


def test_workflow_state_is_json_primitive_only() -> None:
    state: WorkflowState = {
        "incident_id": "incident-1",
        "run_id": "run-1",
        "thread_id": "inc/incident-1/run/run-1",
        "proposal_id": "proposal-1",
        "phase": "await_approval",
        "model_calls": 1,
        "tool_calls": 0,
        "cycles": 1,
        "trail": ["intake", "investigate"],
    }
    assert json.loads(json.dumps(state)) == state
