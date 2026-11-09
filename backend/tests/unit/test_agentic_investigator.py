from __future__ import annotations

from unittest import TestCase

from app.agent.ids import SequentialIdFactory
from app.agent.investigator import (
    InvestigationStatus,
    Investigator,
)
from app.agent.provider import ModelPurpose, ScriptedModelProvider, scripted_response
from app.agent.tools.registry import ToolRegistry
from app.domain.schemas import ActionType
from app.scenarios.registry import load_scenarios
from app.sim.cluster import SimCluster

from .agent_support import CONTEXT, FIXED_NOW, diagnosis_output, proposal_output, snapshot_item


class AgenticInvestigatorTests(TestCase):
    def test_scripted_agentic_trajectories_match_all_three_fixtures(self) -> None:
        expected = {
            "healthy-replica-loss": (InvestigationStatus.PROPOSED, ActionType.SCALE_DEPLOYMENT),
            "readiness-regression": (
                InvestigationStatus.PROPOSED,
                ActionType.RESTART_DEPLOYMENT,
            ),
            "deployment-unavailable": (InvestigationStatus.ESCALATED, None),
        }
        for scenario_id, (expected_status, expected_action) in expected.items():
            with self.subTest(scenario=scenario_id):
                cluster = SimCluster()
                cluster.inject_fault(load_scenarios()[scenario_id])
                initial_state = cluster.state
                snapshot = cluster.snapshot()
                evidence = snapshot_item(snapshot)
                diagnosis = diagnosis_output([evidence.record.evidence_id])
                proposal = proposal_output(
                    [evidence.record.evidence_id],
                    action_type=(
                        None
                        if expected_action is None
                        else expected_action.value
                    ),
                    parameters=(
                        {"replicas": 1}
                        if expected_action is ActionType.SCALE_DEPLOYMENT
                        else {}
                    ),
                )
                provider = ScriptedModelProvider(
                    [
                        scripted_response(
                            {
                                "type": "tool_call",
                                "tool": "get_deployment_status",
                                "arguments": {},
                            }
                        ),
                        scripted_response({"type": "final", "diagnosis": diagnosis}),
                        scripted_response(proposal),
                    ]
                )
                investigator = Investigator(
                    provider,
                    now=lambda: FIXED_NOW,
                    ids=SequentialIdFactory(),
                    source=cluster.source,
                    agentic=True,
                )

                result = investigator.investigate(
                    incident_id=CONTEXT.incident_id,
                    run_id=CONTEXT.run_id,
                    snapshot=snapshot,
                    alert_summary="The healthy service target is one replica.",
                )

                self.assertEqual(result.status, expected_status)
                self.assertEqual(
                    None if result.proposal is None else result.proposal.action_type,
                    expected_action,
                )
                self.assertEqual(
                    [request.purpose for request in provider.requests],
                    [
                        ModelPurpose.INVESTIGATE_STEP,
                        ModelPurpose.INVESTIGATE_STEP,
                        ModelPurpose.PROPOSE,
                    ],
                )
                self.assertEqual(cluster.state, initial_state)

    def test_agentic_investigator_requires_the_read_only_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "read-only EvidenceSource"):
            Investigator(ScriptedModelProvider(), agentic=True)

    def test_registry_contains_only_the_four_read_tools(self) -> None:
        self.assertEqual(
            set(ToolRegistry().names),
            {
                "get_deployment_status",
                "get_readiness_probe",
                "get_recent_changes",
                "get_logs",
            },
        )
