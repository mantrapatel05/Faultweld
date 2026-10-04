from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import quote

from app.scenarios.health import MatchState
from app.scenarios.models import ScenarioDefinition
from app.scenarios.registry import load_scenarios
from app.telemetry.checks import SourceState, SourceVerdict, TelemetryReading

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "scenarios" / "fixtures"
TELEMETRY_PATH = ROOT / "scripts" / "telemetry.py"
CLUSTER_PATH = ROOT / "scripts" / "cluster.py"
MANAGED_NAMESPACE = json.dumps(
    {"metadata": {"name": "faultweld-demo", "labels": {"faultweld.dev/managed": "true"}}}
)


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


telemetry: Any = load_module("faultweld_telemetry", TELEMETRY_PATH)
cluster: Any = load_module("faultweld_cluster_for_telemetry", CLUSTER_PATH)


def fixture(scenario_id: str) -> ScenarioDefinition:
    return load_scenarios(FIXTURES)[scenario_id]


def reading(
    match: MatchState | None, state: SourceState = SourceState.AVAILABLE
) -> TelemetryReading:
    return TelemetryReading(
        verdict=SourceVerdict(state=state, reason="ok" if match else "target_down"), match=match
    )


class ScopeGuardTests(TestCase):
    def test_constants_match_the_cluster_manager(self) -> None:
        self.assertEqual(telemetry.CONTEXT, cluster.CONTEXT)
        self.assertEqual(telemetry.NAMESPACE, cluster.NAMESPACE)
        self.assertEqual(telemetry.SAMPLE_DEPLOYMENT, cluster.SAMPLE_DEPLOYMENT)

    def test_wrong_context_is_refused_before_anything_else_runs(self) -> None:
        with patch.object(telemetry, "run", return_value="kind-other") as run:
            with self.assertRaisesRegex(telemetry.TelemetryError, "refusing"):
                telemetry.require_scope()
        self.assertEqual(run.call_count, 1)

    def test_unlabelled_namespace_is_refused(self) -> None:
        with patch.object(
            telemetry,
            "run",
            side_effect=["kind-faultweld", json.dumps({"metadata": {"labels": {}}})],
        ):
            with self.assertRaisesRegex(telemetry.TelemetryError, "unlabelled"):
                telemetry.require_scope()

    def test_project_scope_is_accepted(self) -> None:
        with patch.object(telemetry, "run", side_effect=["kind-faultweld", MANAGED_NAMESPACE]):
            telemetry.require_scope()


class PrometheusAccessTests(TestCase):
    def test_query_goes_through_the_api_server_proxy_and_is_encoded(self) -> None:
        body = json.dumps(
            {"status": "success", "data": {"resultType": "vector", "result": []}}
        )
        query = "min(up{job='x'})"
        with patch.object(telemetry, "run", return_value=body) as run:
            self.assertIsNone(telemetry.prom_query(query))
        expected = (
            "/api/v1/namespaces/faultweld-demo/services/faultweld-prometheus:9090"
            "/proxy/api/v1/query?query=" + quote(query, safe="")
        )
        self.assertEqual(run.call_args.args[0], ["kubectl", "get", "--raw", expected])

    def test_malformed_response_is_reported_with_the_query(self) -> None:
        with patch.object(telemetry, "run", return_value=json.dumps({"status": "error"})):
            with self.assertRaisesRegex(telemetry.TelemetryError, "query: "):
                telemetry.prom_query("up")


class MutationScopeTests(TestCase):
    def test_scale_only_touches_the_kube_state_metrics_deployment(self) -> None:
        with patch.object(telemetry, "require_scope") as guard:
            with patch.object(telemetry, "run") as run:
                telemetry.scale_ksm(0)
        guard.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [
                "kubectl",
                "-n",
                "faultweld-demo",
                "scale",
                "deployment/faultweld-kube-state-metrics",
                "--replicas=0",
            ],
        )

    def test_scale_is_not_attempted_when_the_scope_guard_fails(self) -> None:
        with patch.object(
            telemetry, "require_scope", side_effect=telemetry.TelemetryError("refusing")
        ):
            with patch.object(telemetry, "run") as run:
                with self.assertRaises(telemetry.TelemetryError):
                    telemetry.scale_ksm(0)
        run.assert_not_called()

    def test_script_never_references_evaluator_labels(self) -> None:
        source = TELEMETRY_PATH.read_text(encoding="utf-8")
        for marker in ("expected_outcome", "allowed_actions", "scenarios/expected", "labels.json"):
            self.assertNotIn(marker, source)


class VerifyFlowTests(TestCase):
    def test_passing_cycle_runs_reset_start_reset_reset_in_order(self) -> None:
        scenario = fixture("healthy-replica-loss")
        readings = [
            reading(MatchState.HEALTHY),
            reading(MatchState.FAULTED),
            reading(MatchState.HEALTHY),
        ]
        with patch.object(telemetry, "run_scenario") as run_scenario:
            with patch.object(telemetry, "read_reading", side_effect=readings):
                result = telemetry.verify_scenario(
                    scenario, source_timeout=5.0, fault_timeout=5.0
                )
        self.assertTrue(result["passed"])
        self.assertEqual(
            [call.args for call in run_scenario.call_args_list],
            [("reset", scenario.scenario_id), ("start", scenario.scenario_id)]
            + [("reset", scenario.scenario_id)] * 2,
        )

    def test_a_missed_fault_is_a_failure_not_a_pass(self) -> None:
        scenario = fixture("healthy-replica-loss")
        readings = [reading(MatchState.HEALTHY)] * 3
        with patch.object(telemetry, "run_scenario"):
            with patch.object(telemetry, "read_reading", side_effect=readings):
                result = telemetry.verify_scenario(
                    scenario, source_timeout=5.0, fault_timeout=0.0
                )
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("faulted", result["failures"][0])
        self.assertIsNone(result["fault_observed_after_seconds"])

    def test_an_unavailable_source_cannot_satisfy_the_baseline(self) -> None:
        scenario = fixture("healthy-replica-loss")
        readings = [
            reading(None, SourceState.UNAVAILABLE),
            reading(MatchState.FAULTED),
            reading(MatchState.HEALTHY),
        ]
        with patch.object(telemetry, "run_scenario"):
            with patch.object(telemetry, "read_reading", side_effect=readings):
                result = telemetry.verify_scenario(
                    scenario, source_timeout=0.0, fault_timeout=5.0
                )
        self.assertFalse(result["passed"])
        self.assertTrue(result["failures"][0].startswith("baseline:"))

    def test_source_outage_always_restores_kube_state_metrics(self) -> None:
        scenario = fixture("healthy-replica-loss")
        with patch.object(telemetry, "scale_ksm") as scale:
            with patch.object(telemetry, "read_reading", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    telemetry.verify_source_outage(scenario, source_timeout=1.0)
        self.assertEqual([call.args[0] for call in scale.call_args_list], [0, 1])

    def test_detected_outage_then_recovery_passes(self) -> None:
        scenario = fixture("healthy-replica-loss")
        readings = [
            reading(None, SourceState.UNAVAILABLE),
            reading(MatchState.HEALTHY),
        ]
        with patch.object(telemetry, "scale_ksm"), patch.object(telemetry, "await_ksm_rollout"):
            with patch.object(telemetry, "read_reading", side_effect=readings):
                result = telemetry.verify_source_outage(scenario, source_timeout=5.0)
        self.assertTrue(result["passed"])
        self.assertEqual(result["detected_as"], "unavailable/target_down")

    def test_undetected_outage_fails(self) -> None:
        scenario = fixture("healthy-replica-loss")
        readings = [reading(MatchState.HEALTHY), reading(MatchState.HEALTHY)]
        with patch.object(telemetry, "scale_ksm"), patch.object(telemetry, "await_ksm_rollout"):
            with patch.object(telemetry, "read_reading", side_effect=readings):
                result = telemetry.verify_source_outage(scenario, source_timeout=0.0)
        self.assertFalse(result["passed"])
        self.assertIn("still looked available", result["failures"][0])

    def test_transient_fixture_is_reported_as_not_asserted(self) -> None:
        self.assertIn("deployment-unavailable", telemetry.NOT_ASSERTED)
        self.assertNotIn("deployment-unavailable", telemetry.PERSISTENT_SCENARIOS)
        for scenario_id in telemetry.PERSISTENT_SCENARIOS:
            self.assertIn(scenario_id, load_scenarios(FIXTURES))
