"""Trusted composition helper for the simulator-backed gateway."""

from collections.abc import Callable
from datetime import datetime

from app.agent.ids import IdFactory
from app.gateway import Gateway
from app.policy.config import PolicyConfig
from app.sim.cluster import SimCluster
from app.sim.writer import SimWriter
from app.store import Store


def build_simulator_gateway(
    store: Store,
    cluster: SimCluster,
    policy_config: PolicyConfig,
    *,
    clock: Callable[[], datetime],
    ids: IdFactory,
) -> tuple[Gateway, SimWriter]:
    """Keep the write adapter inside the gateway composition boundary."""
    writer = SimWriter(cluster)
    gateway = Gateway(
        store,
        cluster.source,
        writer,
        policy_config,
        clock=clock,
        ids=ids,
    )
    return gateway, writer


__all__ = ["build_simulator_gateway"]
