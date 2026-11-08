"""The only simulation adapter that exposes the write port."""

from __future__ import annotations

from threading import Lock

from app.sim.cluster import SimCluster
from app.sim.ports import (
    RestartOperation,
    RollbackOperation,
    ScaleOperation,
    UnsupportedOperation,
    WriteOperation,
    WriteResult,
)


class SimWriter:
    def __init__(self, cluster: SimCluster) -> None:
        self._cluster = cluster
        self._lock = Lock()
        self._operations: list[WriteResult] = []

    @property
    def operation_log(self) -> tuple[WriteResult, ...]:
        with self._lock:
            return tuple(self._operations)

    def apply(self, operation: WriteOperation) -> WriteResult:
        with self._lock:
            before = self._cluster.snapshot()
            if isinstance(operation, RestartOperation):
                self._cluster.apply_restart(operation.expected_resource_version)
            elif isinstance(operation, ScaleOperation):
                self._cluster.apply_scale(
                    operation.replicas, operation.expected_resource_version
                )
            elif isinstance(operation, RollbackOperation):
                raise UnsupportedOperation("rollback is not supported in Faultweld Lite")
            else:
                raise UnsupportedOperation("unknown write operation")
            after = self._cluster.snapshot()
            result = WriteResult(
                operation=operation,
                before_resource_version=before.resource_version,
                after_resource_version=after.resource_version,
                state_fingerprint=after.state_fingerprint,
            )
            self._operations.append(result)
            return result


__all__ = ["SimWriter"]
