"""Read-only, allow-listed, bounded Kubernetes evidence adapter.

The adapter can only issue GET requests for deployments, pods, and services inside the
project namespace. Query inputs are validated before any path is built, returned objects
are re-checked for namespace, and payloads are whitelist projections.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import quote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from app.domain.schemas import Evidence, EvidenceFreshness, EvidenceSourceType
from app.evidence.models import EvidenceContext, EvidenceItem, EvidenceReadError, ReadOutcome
from app.evidence.normalize import (
    normalize_deployment,
    normalize_pod,
    normalize_service,
    object_name,
)
from app.evidence.transport import KubernetesTransport, TransportError

ALLOWED_NAMESPACE = "faultweld-demo"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_NAME_LENGTH = 100
MAX_SELECTOR_REQUIREMENTS = 8
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 128 * 1024

NAME_PATTERN = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")
KEY_PATTERN = re.compile(
    r"([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?"
)
VALUE_PATTERN = re.compile(r"([A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)?")


class ResourceKind(StrEnum):
    DEPLOYMENT = "deployment"
    POD = "pod"
    SERVICE = "service"


KIND_LABEL: dict[ResourceKind, str] = {
    ResourceKind.DEPLOYMENT: "Deployment",
    ResourceKind.POD: "Pod",
    ResourceKind.SERVICE: "Service",
}
RESOURCE_PATHS: dict[ResourceKind, tuple[str, str]] = {
    ResourceKind.DEPLOYMENT: ("apis/apps/v1", "deployments"),
    ResourceKind.POD: ("api/v1", "pods"),
    ResourceKind.SERVICE: ("api/v1", "services"),
}
NORMALIZERS: dict[ResourceKind, Callable[[Mapping[str, object]], dict[str, JsonValue]]] = {
    ResourceKind.DEPLOYMENT: normalize_deployment,
    ResourceKind.POD: normalize_pod,
    ResourceKind.SERVICE: normalize_service,
}


def _valid_key(key: str) -> bool:
    if not KEY_PATTERN.fullmatch(key):
        return False
    prefix, _, name = key.rpartition("/")
    return len(name) <= 63 and len(prefix) <= 253


def _valid_value(value: str) -> bool:
    return len(value) <= 63 and VALUE_PATTERN.fullmatch(value) is not None


def validate_selector(selector: str) -> str:
    """Accept only ``k=v``, ``k==v``, ``k!=v``, ``k`` and ``!k``, comma separated."""
    requirements = selector.split(",")
    if len(requirements) > MAX_SELECTOR_REQUIREMENTS:
        raise ValueError(f"a selector may have at most {MAX_SELECTOR_REQUIREMENTS} requirements")
    for requirement in requirements:
        value: str | None = None
        if "!=" in requirement:
            key, _, value = requirement.partition("!=")
        elif "==" in requirement:
            key, _, value = requirement.partition("==")
        elif "=" in requirement:
            key, _, value = requirement.partition("=")
        elif requirement.startswith("!"):
            key = requirement[1:]
        else:
            key = requirement
        if not _valid_key(key):
            raise ValueError("selector contains an invalid label key")
        if value is not None and not _valid_value(value):
            raise ValueError("selector contains an invalid label value")
    return selector


class KubernetesQuery(BaseModel):
    """A read request. The namespace is deliberately not a field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ResourceKind
    name: str | None = None
    label_selector: str | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is not None and not (
            len(value) <= MAX_NAME_LENGTH and NAME_PATTERN.fullmatch(value)
        ):
            raise ValueError("name must be a lowercase DNS-style resource name")
        return value

    @field_validator("label_selector")
    @classmethod
    def validate_label_selector(cls, value: str | None) -> str | None:
        return None if value is None else validate_selector(value)

    @model_validator(mode="after")
    def validate_exclusive_target(self) -> KubernetesQuery:
        if self.name is not None and self.label_selector is not None:
            raise ValueError("name and label_selector are mutually exclusive")
        return self


@dataclass(frozen=True)
class KubernetesReadResult:
    items: tuple[EvidenceItem, ...]
    outcome: ReadOutcome
    truncated: bool


def request_path(query: KubernetesQuery) -> str:
    group, plural = RESOURCE_PATHS[query.kind]
    base = f"/{group}/namespaces/{ALLOWED_NAMESPACE}/{plural}"
    if query.name is not None:
        return f"{base}/{query.name}"
    parameters = [f"limit={query.limit}"]
    if query.label_selector is not None:
        parameters.append("labelSelector=" + quote(query.label_selector, safe=""))
    return f"{base}?{'&'.join(parameters)}"


def describe_query(query: KubernetesQuery) -> str:
    group, plural = RESOURCE_PATHS[query.kind]
    base = f"GET {group}/{plural} namespace={ALLOWED_NAMESPACE}"
    if query.name is not None:
        return f"{base} name={query.name}"
    selector = f" selector={query.label_selector}" if query.label_selector else ""
    return f"{base}{selector} limit={query.limit}"


def canonical_json(payload: Mapping[str, JsonValue]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _malformed(message: str) -> EvidenceReadError:
    return EvidenceReadError("malformed_response", message)


def _extract_objects(
    query: KubernetesQuery, document: Mapping[str, object]
) -> tuple[list[Mapping[str, object]], bool]:
    expected_kind = KIND_LABEL[query.kind]
    if query.name is not None:
        if document.get("kind") != expected_kind:
            raise _malformed(f"expected a {expected_kind} object")
        return [document], False
    if document.get("kind") != f"{expected_kind}List":
        raise _malformed(f"expected a {expected_kind}List object")
    items = document.get("items")
    if not isinstance(items, list):
        raise _malformed("list response has no items array")
    objects: list[Mapping[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise _malformed("list item is not an object")
        objects.append(item)
    metadata = document.get("metadata")
    more = isinstance(metadata, Mapping) and bool(metadata.get("continue"))
    return objects, more


def _check_isolation(query: KubernetesQuery, obj: Mapping[str, object]) -> None:
    metadata = obj.get("metadata")
    meta: Mapping[str, object] = metadata if isinstance(metadata, Mapping) else {}
    if meta.get("namespace") != ALLOWED_NAMESPACE:
        raise EvidenceReadError(
            "cross_namespace_object", f"the object is not in namespace {ALLOWED_NAMESPACE}"
        )
    if query.name is not None and meta.get("name") != query.name:
        raise _malformed("the returned object does not match the requested name")


class KubernetesEvidenceAdapter:
    def __init__(
        self,
        transport: KubernetesTransport,
        *,
        now: Callable[[], datetime] = _utc_now,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_payload_bytes: int = MAX_PAYLOAD_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ) -> None:
        self._transport = transport
        self._now = now
        self._max_response_bytes = max_response_bytes
        self._max_payload_bytes = max_payload_bytes
        self._max_total_bytes = max_total_bytes

    def read(self, context: EvidenceContext, query: KubernetesQuery) -> KubernetesReadResult:
        observed_at = self._observed_at()
        raw = self._fetch(query)
        if raw is None:
            return KubernetesReadResult(items=(), outcome=ReadOutcome.NOT_FOUND, truncated=False)
        document = self._parse(raw)
        objects, more = _extract_objects(query, document)
        objects.sort(key=lambda candidate: object_name(candidate) or "")
        truncated = more
        if len(objects) > query.limit:
            objects = objects[: query.limit]
            truncated = True
        items: list[EvidenceItem] = []
        total = 0
        for obj in objects:
            _check_isolation(query, obj)
            item, size = self._build_item(context, query, obj, observed_at)
            if total + size > self._max_total_bytes:
                truncated = True
                break
            total += size
            items.append(item)
        return KubernetesReadResult(
            items=tuple(items), outcome=ReadOutcome.OK, truncated=truncated
        )

    def _observed_at(self) -> datetime:
        observed = self._now()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("the evidence clock must return a timezone-aware datetime")
        return observed

    def _fetch(self, query: KubernetesQuery) -> str | None:
        try:
            return self._transport.get(request_path(query))
        except TransportError as error:
            if error.code == "not_found":
                return None
            code = "forbidden" if error.code == "forbidden" else "source_unavailable"
            raise EvidenceReadError(code, str(error)) from error

    def _parse(self, raw: str) -> Mapping[str, object]:
        if len(raw.encode("utf-8")) > self._max_response_bytes:
            raise EvidenceReadError(
                "response_too_large", f"response exceeds {self._max_response_bytes} bytes"
            )
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise _malformed("response is not valid JSON") from error
        if not isinstance(document, Mapping):
            raise _malformed("response is not a JSON object")
        return document

    def _build_item(
        self,
        context: EvidenceContext,
        query: KubernetesQuery,
        obj: Mapping[str, object],
        observed_at: datetime,
    ) -> tuple[EvidenceItem, int]:
        name = object_name(obj)
        if name is None:
            raise _malformed("the returned object has no name")
        payload = NORMALIZERS[query.kind](obj)
        canonical = canonical_json(payload)
        encoded = canonical.encode("utf-8")
        if len(encoded) > self._max_payload_bytes:
            raise EvidenceReadError(
                "payload_too_large", f"{name} exceeds {self._max_payload_bytes} payload bytes"
            )
        content_hash = hashlib.sha256(encoded).hexdigest()
        source_resource = f"{KIND_LABEL[query.kind]}/{ALLOWED_NAMESPACE}/{name}"
        identity = f"{context.run_id}|{source_resource}|{content_hash}"
        evidence_id = f"ev-k8s-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"
        try:
            record = Evidence(
                schema_version="0.1",
                evidence_id=evidence_id,
                incident_id=context.incident_id,
                run_id=context.run_id,
                source_type=EvidenceSourceType.KUBERNETES,
                source_resource=source_resource,
                query=describe_query(query),
                observed_at=observed_at,
                freshness=EvidenceFreshness.FRESH,
                content_hash=content_hash,
                payload_reference=f"evidence-store/{evidence_id}",
            )
        except ValidationError as error:
            raise _malformed("the object could not be represented as evidence") from error
        return EvidenceItem(record=record, payload=payload), len(encoded)
