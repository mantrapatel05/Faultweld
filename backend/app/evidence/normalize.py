"""Whitelist projections of Kubernetes objects into bounded, redacted evidence payloads.

Only fields named here are copied. Everything else is dropped, including annotations,
managed fields, container environment and arguments, volumes, and anything else that can
hold configuration or credentials. A new field must be added deliberately.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from app.evidence.redaction import redact_text

MAX_STRING_LENGTH = 200
MAX_KEY_LENGTH = 100
MAX_LABELS = 32
MAX_ITEMS_PER_LIST = 16


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _sequence(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)[:MAX_ITEMS_PER_LIST]
    return []


def text(value: object, limit: int = MAX_STRING_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = redact_text(value)
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _compact(items: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: value for key, value in items.items() if value is not None}


def _labels(value: object) -> dict[str, JsonValue]:
    source = _mapping(value)
    result: dict[str, JsonValue] = {}
    for name in sorted(candidate for candidate in source if isinstance(candidate, str))[
        :MAX_LABELS
    ]:
        clean_name = text(name, MAX_KEY_LENGTH)
        clean_value = text(source[name])
        if clean_name is not None and clean_value is not None:
            result[clean_name] = clean_value
    return result


def _conditions(value: object) -> list[JsonValue]:
    result: list[JsonValue] = []
    for item in _sequence(value):
        condition = _mapping(item)
        result.append(
            _compact(
                {
                    "type": text(condition.get("type")),
                    "status": text(condition.get("status")),
                    "reason": text(condition.get("reason")),
                    "message": text(condition.get("message")),
                    "lastTransitionTime": text(condition.get("lastTransitionTime")),
                }
            )
        )
    return result


def _metadata(obj: Mapping[str, object]) -> dict[str, JsonValue]:
    meta = _mapping(obj.get("metadata"))
    owners: list[JsonValue] = []
    for owner in _sequence(meta.get("ownerReferences")):
        reference = _mapping(owner)
        owners.append(
            _compact(
                {
                    "kind": text(reference.get("kind")),
                    "name": text(reference.get("name")),
                    "controller": _bool(reference.get("controller")),
                }
            )
        )
    return _compact(
        {
            "name": text(meta.get("name")),
            "namespace": text(meta.get("namespace")),
            "uid": text(meta.get("uid")),
            "resourceVersion": text(meta.get("resourceVersion")),
            "generation": _int(meta.get("generation")),
            "creationTimestamp": text(meta.get("creationTimestamp")),
            "labels": _labels(meta.get("labels")),
            "ownerReferences": owners,
        }
    )


def object_name(obj: Mapping[str, object]) -> str | None:
    return text(_mapping(obj.get("metadata")).get("name"))


def _probe_path(value: object) -> str | None:
    return text(_mapping(_mapping(value).get("httpGet")).get("path"))


def _counter(status: Mapping[str, object], key: str) -> int:
    """Kubernetes omits zero-valued status counters; absent means zero."""
    value = _int(status.get(key))
    return 0 if value is None else value


def normalize_deployment(obj: Mapping[str, object]) -> dict[str, JsonValue]:
    spec = _mapping(obj.get("spec"))
    status = _mapping(obj.get("status"))
    pod_spec = _mapping(_mapping(spec.get("template")).get("spec"))
    containers: list[JsonValue] = []
    for item in _sequence(pod_spec.get("containers")):
        container = _mapping(item)
        containers.append(
            _compact(
                {
                    "name": text(container.get("name")),
                    "image": text(container.get("image")),
                    "readinessProbePath": _probe_path(container.get("readinessProbe")),
                    "livenessProbePath": _probe_path(container.get("livenessProbe")),
                }
            )
        )
    return {
        "metadata": _metadata(obj),
        "spec": _compact(
            {
                "replicas": _int(spec.get("replicas")),
                "strategy": text(_mapping(spec.get("strategy")).get("type")),
                "selector": _labels(_mapping(spec.get("selector")).get("matchLabels")),
                "containers": containers,
            }
        ),
        "status": _compact(
            {
                "observedGeneration": _int(status.get("observedGeneration")),
                "replicas": _counter(status, "replicas"),
                "updatedReplicas": _counter(status, "updatedReplicas"),
                "readyReplicas": _counter(status, "readyReplicas"),
                "availableReplicas": _counter(status, "availableReplicas"),
                "unavailableReplicas": _counter(status, "unavailableReplicas"),
                "conditions": _conditions(status.get("conditions")),
            }
        ),
    }


def _container_state(value: object) -> dict[str, JsonValue] | None:
    state = _mapping(value)
    for phase in ("running", "waiting", "terminated"):
        if phase in state:
            inner = _mapping(state[phase])
            return _compact(
                {
                    "phase": phase,
                    "reason": text(inner.get("reason")),
                    "exitCode": _int(inner.get("exitCode")),
                }
            )
    return None


def normalize_pod(obj: Mapping[str, object]) -> dict[str, JsonValue]:
    spec = _mapping(obj.get("spec"))
    status = _mapping(obj.get("status"))
    containers: list[JsonValue] = []
    for item in _sequence(spec.get("containers")):
        container = _mapping(item)
        containers.append(
            _compact({"name": text(container.get("name")), "image": text(container.get("image"))})
        )
    container_statuses: list[JsonValue] = []
    for item in _sequence(status.get("containerStatuses")):
        container_status = _mapping(item)
        container_statuses.append(
            _compact(
                {
                    "name": text(container_status.get("name")),
                    "image": text(container_status.get("image")),
                    "ready": _bool(container_status.get("ready")),
                    "restartCount": _int(container_status.get("restartCount")),
                    "state": _container_state(container_status.get("state")),
                    "lastState": _container_state(container_status.get("lastState")),
                }
            )
        )
    return {
        "metadata": _metadata(obj),
        "spec": _compact({"nodeName": text(spec.get("nodeName")), "containers": containers}),
        "status": _compact(
            {
                "phase": text(status.get("phase")),
                "reason": text(status.get("reason")),
                "startTime": text(status.get("startTime")),
                "conditions": _conditions(status.get("conditions")),
                "containerStatuses": container_statuses,
            }
        ),
    }


def normalize_service(obj: Mapping[str, object]) -> dict[str, JsonValue]:
    spec = _mapping(obj.get("spec"))
    ports: list[JsonValue] = []
    for item in _sequence(spec.get("ports")):
        port = _mapping(item)
        target_number = _int(port.get("targetPort"))
        target: JsonValue = (
            target_number if target_number is not None else text(port.get("targetPort"))
        )
        ports.append(
            _compact(
                {
                    "name": text(port.get("name")),
                    "port": _int(port.get("port")),
                    "targetPort": target,
                    "protocol": text(port.get("protocol")),
                }
            )
        )
    return {
        "metadata": _metadata(obj),
        "spec": _compact(
            {
                "type": text(spec.get("type")),
                "selector": _labels(spec.get("selector")),
                "ports": ports,
            }
        ),
    }
