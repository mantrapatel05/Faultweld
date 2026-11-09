"""Read-only, bounded evidence projection tests for tool execution."""

from __future__ import annotations

import hashlib
import unittest

from app.agent.tools.executor import ToolExecutor, ToolResultError
from app.evidence.canonical import canonical_json
from app.evidence.models import EvidenceContext
from app.scenarios.registry import load_scenarios
from app.sim.cluster import SimCluster
from tests.unit.agent_support import FIXED_NOW


class ToolExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster = SimCluster()
        self.cluster.inject_fault(load_scenarios()["readiness-regression"])
        self.context = EvidenceContext(incident_id="incident-tools", run_id="run-tools")
        self.executor = ToolExecutor(
            self.cluster.source,
            self.context,
            now=lambda: FIXED_NOW,
        )

    def test_results_have_deterministic_identity_and_matching_content_hash(self) -> None:
        first = self.executor.execute("get_readiness_probe", {})
        again = ToolExecutor(
            self.cluster.source,
            self.context,
            now=lambda: FIXED_NOW,
        ).execute("get_readiness_probe", {})

        self.assertEqual(first.record.evidence_id, again.record.evidence_id)
        self.assertEqual(first.record.content_hash, again.record.content_hash)
        self.assertEqual(first.record.run_id, self.context.run_id)
        self.assertEqual(first.record.incident_id, self.context.incident_id)
        expected_hash = hashlib.sha256(canonical_json(first.payload).encode("utf-8")).hexdigest()
        self.assertEqual(first.record.content_hash, expected_hash)

    def test_all_tool_reads_leave_simulation_state_unchanged(self) -> None:
        before = self.cluster.state
        for name, args in (
            ("get_deployment_status", {}),
            ("get_readiness_probe", {}),
            ("get_recent_changes", {"limit": 3}),
            ("get_logs", {"lines": 5}),
        ):
            self.executor.execute(name, args)
        self.assertEqual(self.cluster.state, before)

    def test_payload_limits_fail_closed(self) -> None:
        too_small = ToolExecutor(
            self.cluster.source,
            self.context,
            now=lambda: FIXED_NOW,
            per_result_max_bytes=1,
            total_max_bytes=1,
        )
        with self.assertRaises(ToolResultError) as raised:
            too_small.execute("get_deployment_status", {})
        self.assertEqual(raised.exception.reason, "evidence_byte_limit")

        sample = ToolExecutor(
            self.cluster.source,
            self.context,
            now=lambda: FIXED_NOW,
        ).execute("get_deployment_status", {})
        size = len(canonical_json(sample.payload).encode("utf-8"))
        one_result = ToolExecutor(
            self.cluster.source,
            self.context,
            now=lambda: FIXED_NOW,
            per_result_max_bytes=size,
            total_max_bytes=size,
        )
        one_result.execute("get_deployment_status", {})
        with self.assertRaises(ToolResultError) as raised:
            one_result.execute("get_deployment_status", {})
        self.assertEqual(raised.exception.reason, "evidence_byte_limit")

    def test_tool_validation_error_is_stable_and_does_not_read_or_mutate(self) -> None:
        before = self.cluster.state
        with self.assertRaises(ToolResultError) as raised:
            self.executor.execute("get_logs", {"lines": 1, "path": "secret"})
        self.assertEqual(raised.exception.reason, "invalid_tool_arguments")
        self.assertEqual(self.cluster.state, before)


if __name__ == "__main__":
    unittest.main()
