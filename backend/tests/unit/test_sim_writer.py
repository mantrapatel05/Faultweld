from __future__ import annotations

from unittest import TestCase

from app.sim.cluster import SimCluster, SimError
from app.sim.ports import (
    PreconditionFailed,
    RestartOperation,
    RollbackOperation,
    ScaleOperation,
    UnsupportedOperation,
)
from app.sim.writer import SimWriter


class SimWriterTests(TestCase):
    def test_scale_is_compare_and_set_and_logs_only_applied_operations(self) -> None:
        cluster = SimCluster()
        writer = SimWriter(cluster)
        before = cluster.snapshot()
        result = writer.apply(ScaleOperation(0, before.resource_version))
        self.assertEqual(result.before_resource_version, before.resource_version)
        self.assertGreater(result.after_resource_version, before.resource_version)
        self.assertEqual(len(writer.operation_log), 1)

        current = cluster.snapshot()
        with self.assertRaises(PreconditionFailed):
            writer.apply(ScaleOperation(1, before.resource_version))
        self.assertEqual(cluster.snapshot(), current)
        self.assertEqual(len(writer.operation_log), 1)

    def test_restart_replaces_the_pod_after_the_configured_ticks(self) -> None:
        cluster = SimCluster()
        writer = SimWriter(cluster)
        before = cluster.snapshot()
        writer.apply(RestartOperation(before.resource_version))
        self.assertEqual(cluster.snapshot().observation.ready_replicas, 0)
        cluster.tick(3)
        self.assertEqual(cluster.snapshot().observation.ready_replicas, 1)
        self.assertEqual(len(writer.operation_log), 1)

    def test_rollback_is_explicitly_unsupported_and_has_no_side_effect(self) -> None:
        cluster = SimCluster()
        writer = SimWriter(cluster)
        before = cluster.state
        with self.assertRaises(UnsupportedOperation):
            writer.apply(RollbackOperation("1", cluster.snapshot().resource_version))
        self.assertEqual(cluster.state, before)
        self.assertEqual(writer.operation_log, ())

    def test_invalid_restart_fails_without_recording_an_operation(self) -> None:
        cluster = SimCluster()
        cluster.mutate_state(desired_replicas=0)
        writer = SimWriter(cluster)
        with self.assertRaises(SimError):
            writer.apply(RestartOperation(cluster.snapshot().resource_version))
        self.assertEqual(writer.operation_log, ())
