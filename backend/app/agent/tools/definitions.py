"""Pure projections from read-only simulation snapshots to tool payloads."""

from __future__ import annotations

from typing import cast

from pydantic import JsonValue

from app.agent.tools.registry import LogsArguments, NoArguments, RecentChangesArguments
from app.evidence.redaction import redact_text
from app.sim.cluster import SimSnapshot


def deployment_status(
    snapshot: SimSnapshot, _arguments: NoArguments
) -> dict[str, JsonValue]:
    return {
        "desired_replicas": snapshot.observation.desired_replicas,
        "available_replicas": snapshot.observation.available_replicas,
        "ready_replicas": snapshot.observation.ready_replicas,
        "revision": snapshot.revision,
        "resource_version": snapshot.resource_version,
        "tick": snapshot.tick,
    }


def readiness_probe(
    snapshot: SimSnapshot, _arguments: NoArguments
) -> dict[str, JsonValue]:
    return {
        "path": snapshot.readiness_path,
        "port": snapshot.readiness_port,
        "matches_healthy_values": (
            snapshot.readiness_path == "/" and snapshot.readiness_port == "http"
        ),
    }


def recent_changes(
    snapshot: SimSnapshot, arguments: RecentChangesArguments
) -> dict[str, JsonValue]:
    events = snapshot.recent_events[-arguments.limit :]
    return {
        "events": [
            {
                "tick": event.tick,
                "kind": event.kind,
                "resource_version": event.resource_version,
            }
            for event in events
        ]
    }


def logs(snapshot: SimSnapshot, arguments: LogsArguments) -> dict[str, JsonValue]:
    lines = [
        redact_text(line)[:500]
        for line in snapshot.synthetic_log_lines[: arguments.lines]
    ]
    return {"trust": "untrusted_log_text", "lines": cast(JsonValue, lines)}
