from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Any
from unittest import TestCase

from app.scenarios.health import MatchState
from app.scenarios.models import ScenarioDefinition, ScenarioFault
from app.scenarios.registry import load_scenarios
from app.scenarios.repeatability import build_cycle, summarize
from app.sim.cluster import (
    POD_SELECTOR,
    REPLACEMENT_TICKS,
    EvidenceSource,
    SimCluster,
    SimError,
    SimSnapshot,
    SimSource,
    fingerprint_of,
)

ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = ROOT / "backend" / "app" / "sim"
ALL_FIXTURES = ("deployment-unavailable", "healthy-replica-loss", "readiness-regression")


def fixture(scenario_id: str) -> ScenarioDefinition:
    return load_scenarios()[scenario_id]


def public(instance: object) -> set[str]:
    return {name for name in dir(instance) if not name.startswith("_")}


class BaselineTests(TestCase):
    def test_a_new_cluster_is_healthy_for_every_fixture(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                cluster = SimCluster()
                self.assertIs(cluster.classify(fixture(scenario_id)), MatchState.HEALTHY)
                observation = cluster.observe()
                self.assertEqual(
                    (
                        observation.desired_replicas,
                        observation.available_replicas,
                        observation.ready_replicas,
                    ),
                    (1, 1, 1),
                )

    def test_the_baseline_state_is_exact(self) -> None:
        state = SimCluster().state
        self.assertEqual(
            (
                state.desired_replicas,
                state.pod_present,
                state.readiness_path,
                state.readiness_port,
                state.revision,
                state.resource_version,
                state.tick,
                state.replacement_due_tick,
            ),
            (1, True, "/", "http", 1, 1, 0, None),
        )


class FaultInjectionTests(TestCase):
    def test_every_fault_is_observed_immediately_after_injection(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                cluster = SimCluster()
                scenario = fixture(scenario_id)
                cluster.inject_fault(scenario)
                self.assertIs(cluster.classify(scenario), MatchState.FAULTED)

    def test_scale_and_readiness_faults_persist_without_intervention(self) -> None:
        for scenario_id in ("healthy-replica-loss", "readiness-regression"):
            with self.subTest(scenario=scenario_id):
                cluster = SimCluster()
                scenario = fixture(scenario_id)
                cluster.inject_fault(scenario)
                cluster.tick(50)
                self.assertIs(cluster.classify(scenario), MatchState.FAULTED)

    def test_a_deleted_pod_is_replaced_after_a_fixed_number_of_ticks(self) -> None:
        cluster = SimCluster()
        scenario = fixture("deployment-unavailable")
        cluster.inject_fault(scenario)
        for _ in range(REPLACEMENT_TICKS - 1):
            cluster.tick()
            self.assertIs(cluster.classify(scenario), MatchState.FAULTED)
        cluster.tick()
        self.assertIs(cluster.classify(scenario), MatchState.HEALTHY)
        self.assertEqual(cluster.state.tick, REPLACEMENT_TICKS)
        self.assertIsNone(cluster.state.replacement_due_tick)

    def test_scale_to_zero_removes_the_pod(self) -> None:
        cluster = SimCluster()
        cluster.inject_fault(fixture("healthy-replica-loss"))
        self.assertEqual(
            (cluster.state.desired_replicas, cluster.state.pod_present), (0, False)
        )
        self.assertEqual(cluster.observe().available_replicas, 0)

    def test_the_readiness_fault_keeps_the_pod_but_it_is_not_ready(self) -> None:
        cluster = SimCluster()
        cluster.inject_fault(fixture("readiness-regression"))
        state = cluster.state
        self.assertEqual((state.pod_present, state.readiness_port), (True, 9))
        self.assertEqual(state.revision, 2)
        self.assertEqual(
            (cluster.observe().desired_replicas, cluster.observe().ready_replicas), (1, 0)
        )

    def test_a_readiness_path_fault_also_makes_the_pod_unready(self) -> None:
        scenario = fixture("readiness-regression").model_copy(
            update={"fault": ScenarioFault.model_validate({"kind": "readiness", "path": "/bad"})}
        )
        cluster = SimCluster()
        cluster.inject_fault(scenario)
        self.assertEqual(
            (cluster.state.readiness_path, cluster.state.readiness_port), ("/bad", "http")
        )
        self.assertIs(cluster.classify(scenario), MatchState.FAULTED)

    def test_injecting_the_same_fault_twice_changes_nothing_the_second_time(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                cluster = SimCluster()
                scenario = fixture(scenario_id)
                cluster.inject_fault(scenario)
                after_first = cluster.state
                cluster.inject_fault(scenario)
                self.assertEqual(cluster.state, after_first)

    def test_a_pod_selector_outside_the_project_is_refused_without_side_effects(self) -> None:
        fault = ScenarioFault.model_validate({"kind": "delete_pod", "selector": "app=other"})
        scenario = fixture("deployment-unavailable").model_copy(update={"fault": fault})
        cluster = SimCluster()
        before = cluster.state
        with self.assertRaisesRegex(SimError, "refusing pod selector"):
            cluster.inject_fault(scenario)
        self.assertEqual(cluster.state, before)
        self.assertEqual(POD_SELECTOR, "app.kubernetes.io/name=faultweld-sample")

    def test_out_of_scope_scenarios_are_refused_everywhere(self) -> None:
        scenario = fixture("healthy-replica-loss").model_copy(
            update={"namespace": "kube-system", "target_deployment": "other"}
        )
        cluster = SimCluster()
        before = cluster.state
        for action in (cluster.inject_fault, cluster.classify):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(
                SimError, "refusing"
            ):
                action(scenario)
        self.assertEqual(cluster.state, before)

    def test_malformed_faults_are_refused(self) -> None:
        for kind in ("scale", "readiness", "delete_pod"):
            fault = ScenarioFault.model_construct(
                kind=kind, replicas=None, path=None, port=None, selector=None
            )
            scenario = fixture("healthy-replica-loss").model_copy(update={"fault": fault})
            with self.subTest(kind=kind), self.assertRaises(SimError):
                SimCluster().inject_fault(scenario)


class ResetTests(TestCase):
    def test_reset_restores_health_after_every_fault(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                cluster = SimCluster()
                scenario = fixture(scenario_id)
                cluster.inject_fault(scenario)
                cluster.reset()
                self.assertIs(cluster.classify(scenario), MatchState.HEALTHY)
                self.assertEqual(
                    (
                        cluster.state.desired_replicas,
                        cluster.state.pod_present,
                        cluster.state.readiness_path,
                        cluster.state.readiness_port,
                        cluster.state.replacement_due_tick,
                    ),
                    (1, True, "/", "http", None),
                )

    def test_a_second_reset_changes_nothing(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                cluster = SimCluster()
                cluster.inject_fault(fixture(scenario_id))
                cluster.reset()
                after_first = cluster.state
                fingerprint = cluster.fingerprint()
                cluster.reset()
                self.assertEqual(cluster.state, after_first)
                self.assertEqual(cluster.fingerprint(), fingerprint)

    def test_reset_on_a_healthy_cluster_is_a_no_op(self) -> None:
        cluster = SimCluster()
        before = cluster.state
        cluster.reset()
        self.assertEqual(cluster.state, before)

    def test_reset_cancels_a_pending_pod_replacement(self) -> None:
        cluster = SimCluster()
        cluster.inject_fault(fixture("deployment-unavailable"))
        cluster.reset()
        self.assertIsNone(cluster.state.replacement_due_tick)
        before = cluster.state
        cluster.tick(10)
        self.assertEqual(
            dataclasses.replace(cluster.state, tick=0), dataclasses.replace(before, tick=0)
        )


class CounterTests(TestCase):
    def test_resource_version_only_moves_on_real_changes(self) -> None:
        cluster = SimCluster()
        self.assertEqual(cluster.state.resource_version, 1)
        cluster.tick(5)
        self.assertEqual(cluster.state.resource_version, 1)
        cluster.inject_fault(fixture("healthy-replica-loss"))
        self.assertEqual(cluster.state.resource_version, 2)
        cluster.inject_fault(fixture("healthy-replica-loss"))
        self.assertEqual(cluster.state.resource_version, 2)

    def test_the_revision_follows_probe_changes_only(self) -> None:
        cluster = SimCluster()
        cluster.inject_fault(fixture("healthy-replica-loss"))
        cluster.reset()
        self.assertEqual(cluster.state.revision, 1)
        cluster.inject_fault(fixture("readiness-regression"))
        self.assertEqual(cluster.state.revision, 2)
        cluster.reset()
        self.assertEqual(cluster.state.revision, 3)
        cluster.reset()
        self.assertEqual(cluster.state.revision, 3)

    def test_negative_ticks_are_rejected(self) -> None:
        with self.assertRaises(SimError):
            SimCluster().tick(-1)


class FingerprintTests(TestCase):
    def test_equal_states_have_equal_fingerprints_and_time_alone_does_not_change_it(self) -> None:
        first, second = SimCluster(), SimCluster()
        self.assertEqual(first.fingerprint(), second.fingerprint())
        before = first.fingerprint()
        first.tick(7)
        self.assertEqual(first.fingerprint(), before)
        self.assertRegex(before, r"^[0-9a-f]{64}$")

    def test_every_kind_of_change_alters_the_fingerprint(self) -> None:
        changes: dict[str, dict[str, Any]] = {
            "scale": {"desired_replicas": 0},
            "probe path": {"readiness_path": "/other"},
            "probe port": {"readiness_port": 9},
        }
        base = SimCluster().fingerprint()
        for label, change in changes.items():
            with self.subTest(change=label):
                cluster = SimCluster()
                cluster.mutate_state(**change)
                self.assertNotEqual(cluster.fingerprint(), base)

    def test_a_changed_fingerprint_does_not_return_after_the_state_is_restored(self) -> None:
        cluster = SimCluster()
        before = cluster.fingerprint()
        cluster.inject_fault(fixture("healthy-replica-loss"))
        cluster.reset()
        self.assertNotEqual(cluster.fingerprint(), before)

    def test_the_fingerprint_function_matches_the_cluster_method(self) -> None:
        cluster = SimCluster()
        self.assertEqual(fingerprint_of(cluster.state), cluster.fingerprint())


class MutateStateTests(TestCase):
    def test_another_operator_change_is_visible_in_state_and_fingerprint(self) -> None:
        cluster = SimCluster()
        before = cluster.fingerprint()
        cluster.mutate_state(readiness_path="/maintenance")
        self.assertEqual(cluster.state.readiness_path, "/maintenance")
        self.assertNotEqual(cluster.fingerprint(), before)

    def test_scaling_back_up_schedules_a_pod_instead_of_appearing_instantly(self) -> None:
        cluster = SimCluster()
        cluster.mutate_state(desired_replicas=0)
        cluster.mutate_state(desired_replicas=1)
        self.assertEqual(cluster.observe().ready_replicas, 0)
        cluster.tick(REPLACEMENT_TICKS)
        self.assertEqual(cluster.observe().ready_replicas, 1)

    def test_invalid_changes_are_rejected_without_side_effects(self) -> None:
        cluster = SimCluster()
        before = cluster.state
        bad: list[dict[str, object]] = [
            {"desired_replicas": 2},
            {"desired_replicas": -1},
            {"desired_replicas": True},
            {"readiness_path": "no-slash"},
            {"readiness_path": "/" + "x" * 200},
            {"readiness_port": 0},
            {"readiness_port": 70000},
            {"readiness_port": ""},
            {"readiness_port": True},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(SimError):
                cluster.mutate_state(**change)  # type: ignore[arg-type]
        self.assertEqual(cluster.state, before)


class SourceTests(TestCase):
    def test_a_source_can_only_take_snapshots(self) -> None:
        source = SimCluster().source
        self.assertEqual(public(source), {"snapshot"})
        self.assertEqual(public(SimSource(SimCluster())), {"snapshot"})
        self.assertIsInstance(source, SimSource)

    def test_the_source_satisfies_the_protocol_and_reflects_current_state(self) -> None:
        cluster = SimCluster()
        source: EvidenceSource = cluster.source
        first = source.snapshot()
        cluster.inject_fault(fixture("readiness-regression"))
        second = source.snapshot()
        self.assertEqual(first.observation.ready_replicas, 1)
        self.assertEqual(second.observation.ready_replicas, 0)
        self.assertEqual(second.readiness_port, 9)
        self.assertNotEqual(first.state_fingerprint, second.state_fingerprint)
        self.assertEqual(second.state_fingerprint, cluster.fingerprint())

    def test_snapshots_and_state_are_immutable(self) -> None:
        cluster = SimCluster()
        snapshot = cluster.snapshot()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.tick = 5  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cluster.state.desired_replicas = 0  # type: ignore[misc]
        self.assertIsInstance(snapshot, SimSnapshot)


class RepeatabilityTests(TestCase):
    def test_every_fixture_passes_the_repeatability_gate_over_repeated_cycles(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                scenario = fixture(scenario_id)
                cluster = SimCluster()
                cycles = []
                for number in range(1, 6):
                    cluster.reset()
                    baseline = cluster.observe()
                    cluster.inject_fault(scenario)
                    faulted = cluster.observe()
                    cluster.reset()
                    first = cluster.observe()
                    cluster.reset()
                    cycles.append(
                        build_cycle(scenario, number, [baseline, faulted, first, cluster.observe()])
                    )
                verdict = summarize(scenario, cycles)
                self.assertTrue(verdict.passed, verdict.failures)
                self.assertEqual(verdict.cycles_passed, 5)

    def test_the_same_operations_always_produce_the_same_state(self) -> None:
        for scenario_id in ALL_FIXTURES:
            with self.subTest(scenario=scenario_id):
                states = []
                for _ in range(2):
                    cluster = SimCluster()
                    cluster.inject_fault(fixture(scenario_id))
                    cluster.tick(2)
                    cluster.reset()
                    cluster.tick(1)
                    states.append(cluster.state)
                self.assertEqual(states[0], states[1])


class IsolationTests(TestCase):
    def test_the_simulation_cannot_reach_a_real_cluster_or_the_labels(self) -> None:
        for path in sorted(SIM_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                for module in modules:
                    with self.subTest(file=path.name, module=module):
                        self.assertNotIn(
                            module.split(".")[0],
                            {"subprocess", "os", "socket", "http", "urllib", "requests", "httpx"},
                        )
                        self.assertFalse(module.startswith("app.evidence"))
            source = path.read_text(encoding="utf-8")
            for marker in ("expected_outcome", "allowed_actions", "scenarios/expected", "kubectl"):
                with self.subTest(file=path.name, marker=marker):
                    self.assertNotIn(marker, source)
