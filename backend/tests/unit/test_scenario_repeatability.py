from __future__ import annotations

import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from app.scenarios.health import HealthObservation, MatchState, observed_state
from app.scenarios.models import ScenarioDefinition
from app.scenarios.registry import load_scenarios
from app.scenarios.repeatability import (
    EXPECTED_PHASES,
    RunPhase,
    build_cycle,
    build_report,
    compare_phase,
    summarize,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "scenarios" / "fixtures"
EXPECTED_LABELS = ROOT / "scenarios" / "expected" / "labels.json"
CONTROLLER_PATH = ROOT / "scripts" / "scenario.py"
ALL_FIXTURES = ("deployment-unavailable", "healthy-replica-loss", "readiness-regression")


def load_controller() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "faultweld_scenario_repeatability", CONTROLLER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller: Any = load_controller()


def fixture(scenario_id: str) -> ScenarioDefinition:
    return load_scenarios(FIXTURES)[scenario_id]


def healthy_observation() -> HealthObservation:
    return HealthObservation(desired_replicas=1, available_replicas=1, ready_replicas=1)


def faulted_observation() -> HealthObservation:
    return HealthObservation(desired_replicas=0, available_replicas=0, ready_replicas=0)


def passing_cycle(scenario: ScenarioDefinition) -> Any:
    return build_cycle(
        scenario,
        1,
        [healthy_observation(), faulted_observation(), healthy_observation(),
         healthy_observation()],
    )


class SimulatedCluster:
    """Minimal model of the faultweld-sample deployment on a Recreate strategy.

    Only the state that the scenario fixtures can change is modelled: the
    replica count, the readiness probe path, and pod presence. The observed
    ``status`` block is derived from that state exactly as a real deployment
    would report it for a single replica.
    """

    def __init__(self, *, ignore_first_fault: bool = False) -> None:
        self.replicas = 1
        self.readiness_path = "/"
        self.pod_exists = True
        self.generation = 1
        self.ignore_first_fault = ignore_first_fault
        self.faults_ignored = 0
        self.commands: list[list[str]] = []

    @property
    def available(self) -> bool:
        return self.pod_exists and self.readiness_path == "/"

    def reset(self) -> None:
        self.replicas = 1
        self.readiness_path = "/"
        self.pod_exists = True

    def _maybe_skip_fault(self) -> bool:
        if self.ignore_first_fault and self.faults_ignored == 0:
            self.faults_ignored += 1
            return True
        return False

    def run(self, command: Sequence[str], *, capture: bool = False) -> str:
        del capture
        cmd = list(command)
        self.commands.append(cmd)
        if cmd[0] == "python":
            self.reset()
            return ""
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return "kind-faultweld"
        if cmd[1:3] == ["get", "namespace"]:
            return json.dumps(
                {
                    "metadata": {
                        "name": "faultweld-demo",
                        "labels": {"faultweld.dev/managed": "true"},
                    }
                }
            )
        verb = cmd[3] if len(cmd) > 3 else ""
        if verb == "scale":
            self.replicas = int(cmd[-1].removeprefix("--replicas="))
            self.pod_exists = self.replicas > 0
            return ""
        if verb == "patch":
            self.readiness_path = str(json.loads(cmd[-1])[0]["value"])
            self.pod_exists = True
            return ""
        if verb == "delete":
            if self._maybe_skip_fault():
                return ""
            self.pod_exists = False
            return ""
        if cmd[3:5] == ["get", "deployment/faultweld-sample"]:
            self.generation += 1
            return json.dumps(
                {
                    "spec": {"replicas": self.replicas},
                    "status": {
                        "availableReplicas": 1 if self.available else 0,
                        "readyReplicas": 1 if self.available else 0,
                        "observedGeneration": self.generation,
                    },
                }
            )
        raise AssertionError(f"unexpected command: {cmd}")


def drive(scenario: ScenarioDefinition, sim: SimulatedCluster, repeats: int) -> list[Any]:
    with patch.object(controller, "run", side_effect=sim.run):
        return [
            controller.build_cycle(scenario, cycle, controller.run_cycle(scenario))
            for cycle in range(1, repeats + 1)
        ]


class RepeatabilityToleranceTests(TestCase):
    def test_three_state_classifier_uses_both_declared_bounds(self) -> None:
        scenario = fixture("healthy-replica-loss")
        healthy = HealthObservation(desired_replicas=1, available_replicas=1, ready_replicas=1)
        faulted = HealthObservation(desired_replicas=0, available_replicas=0, ready_replicas=0)
        partial = HealthObservation(desired_replicas=1, available_replicas=1, ready_replicas=0)
        self.assertEqual(observed_state(scenario, healthy), MatchState.HEALTHY)
        self.assertEqual(observed_state(scenario, faulted), MatchState.FAULTED)
        self.assertEqual(observed_state(scenario, partial), MatchState.INDETERMINATE)

    def test_indeterminate_observation_fails_the_phase(self) -> None:
        scenario = fixture("healthy-replica-loss")
        partial = HealthObservation(desired_replicas=1, available_replicas=1, ready_replicas=0)
        result = compare_phase(scenario, RunPhase.FAULTED, partial)
        self.assertFalse(result.matched)
        reason = result.failure_reason()
        assert reason is not None
        self.assertIn("indeterminate", reason)
        self.assertIn("faulted expected", reason)

    def test_cycle_requires_one_observation_per_phase(self) -> None:
        scenario = fixture("healthy-replica-loss")
        with self.assertRaisesRegex(ValueError, "needs 4 observations"):
            build_cycle(scenario, 1, [healthy_observation()] * 3)
        self.assertEqual(len(EXPECTED_PHASES), 4)

    def test_repeated_reset_phase_is_part_of_the_cycle(self) -> None:
        self.assertIn(RunPhase.RESET_AGAIN, EXPECTED_PHASES)
        scenario = fixture("readiness-regression")
        cycle = passing_cycle(scenario)
        self.assertEqual([phase.phase for phase in cycle.phases], list(EXPECTED_PHASES))
        self.assertTrue(cycle.passed)
        self.assertEqual([phase.expected.value for phase in cycle.phases],
                         ["healthy", "faulted", "healthy", "healthy"])

    def test_a_healthy_reading_at_the_faulted_phase_fails(self) -> None:
        scenario = fixture("healthy-replica-loss")
        cycle = build_cycle(
            scenario,
            1,
            [healthy_observation(), healthy_observation(), healthy_observation(),
             healthy_observation()],
        )
        self.assertFalse(cycle.passed)
        self.assertEqual(len(cycle.failures), 1)
        self.assertIn("faulted expected", cycle.failures[0])

    def test_summarize_requires_at_least_one_cycle(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one cycle"):
            summarize(fixture("healthy-replica-loss"), [])

    def test_report_rejects_mismatched_verdicts(self) -> None:
        first = fixture("healthy-replica-loss")
        second = fixture("readiness-regression")
        verdict = summarize(first, [passing_cycle(first)])
        with self.assertRaisesRegex(ValueError, "does not match scenario"):
            build_report([first, second], [verdict, verdict], 1, "0.2")

    def test_report_rejects_an_empty_scenario_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one scenario"):
            build_report([], [], 1, "0.2")


class FixtureRepeatabilityTests(TestCase):
    def test_every_fixture_reproduces_healthy_fault_and_reset(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                scenario = fixture(scenario_id)
                sim = SimulatedCluster()
                cycles = drive(scenario, sim, repeats=3)
                self.assertEqual(len(cycles), 3)
                for cycle in cycles:
                    self.assertEqual(cycle.failures, ())
                    self.assertTrue(cycle.passed)
                verdict = summarize(scenario, cycles)
                self.assertEqual(verdict.repeats, 3)
                self.assertEqual(verdict.cycles_passed, 3)
                self.assertTrue(verdict.passed)

    def test_repeated_runs_produce_identical_verdicts(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                scenario = fixture(scenario_id)
                first = summarize(scenario, drive(scenario, SimulatedCluster(), repeats=2))
                second = summarize(scenario, drive(scenario, SimulatedCluster(), repeats=2))
                self.assertEqual(first.model_dump(), second.model_dump())

    def test_cleanup_is_idempotent_across_repeats(self) -> None:
        scenario = fixture("readiness-regression")
        sim = SimulatedCluster()
        drive(scenario, sim, repeats=3)
        self.assertEqual(sim.replicas, 1)
        self.assertEqual(sim.readiness_path, "/")
        self.assertTrue(sim.available)
        reset_calls = [cmd for cmd in sim.commands if cmd[0] == "python"]
        self.assertEqual(len(reset_calls), 3 * 4)

    def test_a_fault_that_does_not_take_effect_is_a_failure_not_a_pass(self) -> None:
        scenario = fixture("deployment-unavailable")
        sim = SimulatedCluster(ignore_first_fault=True)
        cycles = drive(scenario, sim, repeats=2)
        verdict = summarize(scenario, cycles)
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.cycles_passed, 1)
        self.assertEqual(len(verdict.failures), 1)
        self.assertIn("cycle 1:", verdict.failures[0])
        self.assertIn("faulted expected", verdict.failures[0])
        self.assertIn("observed healthy", verdict.failures[0])

    def test_report_covers_every_fixture_in_sorted_order(self) -> None:
        sim = SimulatedCluster()
        with patch.object(controller, "run", side_effect=sim.run):
            report = controller.verify_all(repeats=1)
        payload = controller.report_payload(report)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["namespace"], "faultweld-demo")
        self.assertEqual(payload["target_deployment"], "faultweld-sample")
        self.assertEqual(payload["schema_version"], "0.2")
        self.assertEqual(
            [item["scenario_id"] for item in payload["scenarios"]], sorted(ALL_FIXTURES)
        )
        for item in payload["scenarios"]:
            self.assertTrue(item["passed"])
            self.assertEqual(item["fixture_version"], "1")
            self.assertEqual(item["failures"], [])

    def test_report_payload_excludes_evaluator_labels(self) -> None:
        sim = SimulatedCluster()
        with patch.object(controller, "run", side_effect=sim.run):
            payload = controller.report_payload(controller.verify_all(repeats=1))
        serialized = json.dumps(payload)
        for marker in ("expected_outcome", "allowed_actions", "escalate"):
            self.assertNotIn(marker, serialized)
        labels = json.loads(EXPECTED_LABELS.read_text(encoding="utf-8"))
        self.assertEqual(set(labels), set(ALL_FIXTURES))

    def test_verify_scope_guard_rejects_out_of_scope_scenario(self) -> None:
        scenario = fixture("healthy-replica-loss").model_copy(
            update={"namespace": "kube-system", "target_deployment": "other"}
        )
        sim = SimulatedCluster()
        with patch.object(controller, "run", side_effect=sim.run):
            with self.assertRaisesRegex(controller.ScenarioSafetyError, "refusing"):
                controller.run_cycle(scenario)
        self.assertEqual(sim.commands, [])
