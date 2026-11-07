from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import TestCase

from app.scenarios.registry import load_scenarios
from app.sim.cluster import REPLACEMENT_TICKS, SimCluster, SimError

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "sim.py"
ALL_FIXTURES = ("deployment-unavailable", "healthy-replica-loss", "readiness-regression")


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("faultweld_sim_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sim: Any = load_script()


class VerifyTests(TestCase):
    def test_every_fixture_passes_in_sorted_order(self) -> None:
        payload = sim.report_payload(sim.verify_all(3))
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["mode"], "sim")
        self.assertEqual(payload["repeats"], 3)
        self.assertEqual(payload["namespace"], "faultweld-demo")
        self.assertEqual(payload["target_deployment"], "faultweld-sample")
        self.assertEqual([item["scenario_id"] for item in payload["scenarios"]], list(ALL_FIXTURES))
        for item in payload["scenarios"]:
            self.assertEqual((item["cycles_passed"], item["failures"]), (3, []))

    def test_the_report_carries_no_evaluator_labels(self) -> None:
        serialized = json.dumps(sim.report_payload(sim.verify_all(1)))
        for marker in ("expected_outcome", "allowed_actions", "escalate"):
            self.assertNotIn(marker, serialized)

    def test_a_cycle_records_four_observations_in_order(self) -> None:
        scenario = load_scenarios()["healthy-replica-loss"]
        observations = sim.run_cycle(SimCluster(), scenario)
        self.assertEqual(
            [item.available_replicas for item in observations], [1, 0, 1, 1]
        )


class RunAndWatchTests(TestCase):
    def test_run_walks_baseline_fault_reset_and_second_reset(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                rows = sim.run_payload(load_scenarios()[scenario_id])
                self.assertEqual(
                    [row["step"] for row in rows],
                    ["baseline", "after_fault", "after_reset", "after_second_reset"],
                )
                self.assertEqual(
                    [row["state"] for row in rows], ["healthy", "faulted", "healthy", "healthy"]
                )
                self.assertEqual(rows[2]["resource_version"], rows[3]["resource_version"])

    def test_watch_shows_the_transient_fault_healing_on_the_expected_tick(self) -> None:
        rows = sim.watch_payload(load_scenarios()["deployment-unavailable"], REPLACEMENT_TICKS + 1)
        states = [row["state"] for row in rows]
        self.assertEqual(states[0], "faulted")
        self.assertEqual(states[REPLACEMENT_TICKS - 1], "faulted")
        self.assertEqual(states[REPLACEMENT_TICKS], "healthy")
        self.assertEqual([row["tick"] for row in rows], list(range(REPLACEMENT_TICKS + 2)))

    def test_watch_shows_persistent_faults_staying_faulted(self) -> None:
        for scenario_id in ("healthy-replica-loss", "readiness-regression"):
            with self.subTest(scenario=scenario_id):
                rows = sim.watch_payload(load_scenarios()[scenario_id], 10)
                self.assertEqual({row["state"] for row in rows}, {"faulted"})

    def test_out_of_scope_scenarios_are_refused(self) -> None:
        scenario = load_scenarios()["healthy-replica-loss"].model_copy(
            update={"namespace": "kube-system"}
        )
        with self.assertRaises(SimError):
            sim.run_payload(scenario)


class ScriptSafetyTests(TestCase):
    def test_the_script_never_touches_a_cluster_or_the_labels(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for marker in (
            "expected_outcome",
            "allowed_actions",
            "scenarios/expected",
            "labels.json",
            "kubectl",
            "subprocess",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)
