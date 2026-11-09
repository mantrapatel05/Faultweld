from __future__ import annotations

import dataclasses
import hashlib
import json
from unittest import TestCase

from app.scenarios.registry import load_scenarios
from app.sim.cluster import MAX_RECENT_EVENTS, SimCluster, fingerprint_of


def assign_attribute(instance: object, name: str, value: object) -> None:
    setattr(instance, name, value)


class SimSnapshotExtrasTests(TestCase):
    def test_snapshot_extras_are_immutable_and_bounded(self) -> None:
        cluster = SimCluster()
        for index in range(MAX_RECENT_EVENTS + 8):
            cluster.mutate_state(readiness_port=8080 if index % 2 == 0 else "http")

        snapshot = cluster.snapshot()
        self.assertEqual(len(snapshot.recent_events), MAX_RECENT_EVENTS)
        self.assertTrue(snapshot.synthetic_log_lines)
        self.assertLessEqual(len(snapshot.synthetic_log_lines), 20)
        self.assertIsInstance(snapshot.recent_events, tuple)
        self.assertIsInstance(snapshot.synthetic_log_lines, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            assign_attribute(snapshot, "recent_events", ())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            assign_attribute(snapshot.recent_events[0], "kind", "tampered")

    def test_events_and_logs_are_deterministic_for_the_same_operations(self) -> None:
        first = SimCluster()
        second = SimCluster()
        scenario = load_scenarios()["readiness-regression"]
        first.inject_fault(scenario)
        second.inject_fault(scenario)

        a = first.snapshot()
        b = second.snapshot()
        self.assertEqual(a.recent_events, b.recent_events)
        self.assertEqual(a.synthetic_log_lines, b.synthetic_log_lines)
        self.assertEqual(a.state_fingerprint, b.state_fingerprint)

    def test_new_snapshot_fields_do_not_enter_authorization_fingerprint(self) -> None:
        state = SimCluster().state
        expected_material = {
            "desired_replicas": state.desired_replicas,
            "pod_present": state.pod_present,
            "readiness_path": state.readiness_path,
            "readiness_port": state.readiness_port,
            "revision": state.revision,
            "resource_version": state.resource_version,
        }
        encoded = json.dumps(expected_material, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self.assertEqual(fingerprint_of(state), expected)

        cluster = SimCluster()
        before = cluster.snapshot().state_fingerprint
        cluster.inject_fault(load_scenarios()["readiness-regression"])
        after = cluster.snapshot()
        self.assertNotEqual(before, after.state_fingerprint)
        self.assertTrue(after.recent_events)
        self.assertEqual(fingerprint_of(cluster.state), after.state_fingerprint)

    def test_transient_replacement_is_recorded_in_recent_changes(self) -> None:
        cluster = SimCluster()
        cluster.inject_fault(load_scenarios()["deployment-unavailable"])
        cluster.tick(3)
        snapshot = cluster.snapshot()
        self.assertIn("pod_changed", tuple(event.kind for event in snapshot.recent_events))
        self.assertEqual(snapshot.synthetic_log_lines, ("deployment pod is ready",))
