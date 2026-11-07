"""Deterministic in-process stand-in for the ``faultweld-sample`` deployment.

The simulation models only what the scenario fixtures can change: the desired replica count,
whether the single pod exists, the readiness probe path and port, and the revision counters.
It uses a logical tick counter instead of wall-clock time, so a fault that heals by itself
(a deleted pod being replaced) heals after a fixed number of ticks on every run.

Health is classified with the same ``observed_state`` function the real-cluster tooling uses,
so the simulation and the live cluster share one definition of healthy and faulted.

Mutation entry points (``inject_fault``, ``reset``, ``mutate_state``, ``tick``) belong to the
scenario controller and to tests. Read-only consumers get a ``SimSource``, which can only take
snapshots.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Protocol

from app.scenarios.health import HealthObservation, MatchState, observed_state
from app.scenarios.models import ScenarioDefinition

NAMESPACE = "faultweld-demo"
DEPLOYMENT = "faultweld-sample"
POD_SELECTOR = "app.kubernetes.io/name=faultweld-sample"
HEALTHY_PROBE_PATH = "/"
HEALTHY_PROBE_PORT = "http"
REPLACEMENT_TICKS = 3
MAX_REPLICAS = 1


class SimError(ValueError):
    """The requested change is outside what the simulation allows."""


@dataclass(frozen=True)
class SimState:
    desired_replicas: int
    pod_present: bool
    readiness_path: str
    readiness_port: str | int
    revision: int
    resource_version: int
    tick: int
    replacement_due_tick: int | None

    @property
    def probe_ok(self) -> bool:
        return self.readiness_path == HEALTHY_PROBE_PATH and (
            self.readiness_port == HEALTHY_PROBE_PORT
        )

    @property
    def ready_replicas(self) -> int:
        if self.desired_replicas > 0 and self.pod_present and self.probe_ok:
            return 1
        return 0


@dataclass(frozen=True)
class SimSnapshot:
    """A read-only view of the deployment at one moment."""

    observation: HealthObservation
    tick: int
    revision: int
    resource_version: int
    readiness_path: str
    readiness_port: str | int
    state_fingerprint: str


class EvidenceSource(Protocol):
    def snapshot(self) -> SimSnapshot: ...


def fingerprint_of(state: SimState) -> str:
    """SHA-256 over the fields that matter for authorization.

    ``tick`` is excluded so that time passing alone does not change the fingerprint; any
    actual change bumps ``resource_version`` and therefore changes it.
    """
    material = {
        "desired_replicas": state.desired_replicas,
        "pod_present": state.pod_present,
        "readiness_path": state.readiness_path,
        "readiness_port": state.readiness_port,
        "revision": state.revision,
        "resource_version": state.resource_version,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_scope(scenario: ScenarioDefinition) -> None:
    if scenario.namespace != NAMESPACE or scenario.target_deployment != DEPLOYMENT:
        raise SimError(
            f"refusing scenario outside {NAMESPACE}/{DEPLOYMENT}: "
            f"{scenario.namespace}/{scenario.target_deployment}"
        )


class SimCluster:
    def __init__(self) -> None:
        self._state = SimCluster.baseline()

    @staticmethod
    def baseline() -> SimState:
        return SimState(
            desired_replicas=1,
            pod_present=True,
            readiness_path=HEALTHY_PROBE_PATH,
            readiness_port=HEALTHY_PROBE_PORT,
            revision=1,
            resource_version=1,
            tick=0,
            replacement_due_tick=None,
        )

    @property
    def state(self) -> SimState:
        return self._state

    @property
    def source(self) -> SimSource:
        return SimSource(self)

    def observe(self) -> HealthObservation:
        ready = self._state.ready_replicas
        return HealthObservation(
            desired_replicas=self._state.desired_replicas,
            available_replicas=ready,
            ready_replicas=ready,
        )

    def classify(self, scenario: ScenarioDefinition) -> MatchState:
        require_scope(scenario)
        return observed_state(scenario, self.observe())

    def fingerprint(self) -> str:
        return fingerprint_of(self._state)

    def snapshot(self) -> SimSnapshot:
        state = self._state
        return SimSnapshot(
            observation=self.observe(),
            tick=state.tick,
            revision=state.revision,
            resource_version=state.resource_version,
            readiness_path=state.readiness_path,
            readiness_port=state.readiness_port,
            state_fingerprint=fingerprint_of(state),
        )

    def _change(self, *, revision_bump: bool = False, **changes: Any) -> None:
        candidate = replace(self._state, **changes)
        if candidate == self._state:
            return
        bump: dict[str, Any] = {"resource_version": candidate.resource_version + 1}
        if revision_bump:
            bump["revision"] = candidate.revision + 1
        self._state = replace(candidate, **bump)

    def tick(self, count: int = 1) -> None:
        if count < 0:
            raise SimError("count must not be negative")
        for _ in range(count):
            due = self._state.replacement_due_tick
            now = self._state.tick + 1
            self._state = replace(self._state, tick=now)
            if due is not None and now >= due and self._state.desired_replicas > 0:
                self._change(pod_present=True, replacement_due_tick=None)

    def inject_fault(self, scenario: ScenarioDefinition) -> None:
        require_scope(scenario)
        fault = scenario.fault
        if fault.kind == "scale":
            if fault.replicas is None:
                raise SimError("scale fault is missing replicas")
            self._scale(fault.replicas)
        elif fault.kind == "readiness":
            if fault.path is None and fault.port is None:
                raise SimError("readiness fault is missing path or port")
            self._set_probe(
                fault.path if fault.path is not None else self._state.readiness_path,
                fault.port if fault.port is not None else self._state.readiness_port,
            )
        else:
            if fault.selector != POD_SELECTOR:
                raise SimError(f"refusing pod selector {fault.selector!r}")
            self._delete_pod()

    def reset(self) -> None:
        """Restore the healthy baseline. Safe to repeat: a second reset changes nothing."""
        self._change(desired_replicas=1, pod_present=True, replacement_due_tick=None)
        self._set_probe(HEALTHY_PROBE_PATH, HEALTHY_PROBE_PORT)

    def mutate_state(
        self,
        *,
        desired_replicas: int | None = None,
        readiness_path: str | None = None,
        readiness_port: str | int | None = None,
    ) -> None:
        """Change the deployment as another operator would, for tests and the demo.

        This is how an approval is made stale during a pause. It is not part of any
        read-only interface.
        """
        if desired_replicas is not None:
            self._scale(desired_replicas)
        if readiness_path is not None or readiness_port is not None:
            self._set_probe(
                readiness_path if readiness_path is not None else self._state.readiness_path,
                readiness_port if readiness_port is not None else self._state.readiness_port,
            )

    def _scale(self, replicas: int) -> None:
        if isinstance(replicas, bool) or not 0 <= replicas <= MAX_REPLICAS:
            raise SimError(f"replicas must be between 0 and {MAX_REPLICAS}")
        state = self._state
        if replicas == 0:
            self._change(desired_replicas=0, pod_present=False, replacement_due_tick=None)
        elif state.desired_replicas == 0:
            self._change(
                desired_replicas=replicas,
                pod_present=False,
                replacement_due_tick=state.tick + REPLACEMENT_TICKS,
            )

    def _set_probe(self, path: str, port: str | int) -> None:
        if not path.startswith("/") or len(path) > 200:
            raise SimError("readiness path must start with / and be at most 200 characters")
        if isinstance(port, bool) or (isinstance(port, int) and not 1 <= port <= 65535):
            raise SimError("readiness port must be 1-65535 or a port name")
        if isinstance(port, str) and not port:
            raise SimError("readiness port name must not be empty")
        self._change(revision_bump=True, readiness_path=path, readiness_port=port)

    def _delete_pod(self) -> None:
        state = self._state
        if state.pod_present and state.desired_replicas > 0:
            self._change(
                pod_present=False, replacement_due_tick=state.tick + REPLACEMENT_TICKS
            )


class SimSource:
    """Read-only access to a cluster. It can take snapshots and nothing else."""

    def __init__(self, cluster: SimCluster) -> None:
        self._cluster = cluster

    def snapshot(self) -> SimSnapshot:
        return self._cluster.snapshot()
