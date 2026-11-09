"""Read-only tool executor that accepts only the snapshot-only evidence source."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue

from app.agent.tools.definitions import deployment_status, logs, readiness_probe, recent_changes
from app.agent.tools.registry import (
    ToolArgumentsError,
    ToolRegistry,
)
from app.domain.schemas import Evidence, EvidenceFreshness, EvidenceSourceType
from app.evidence.canonical import canonical_json
from app.evidence.models import EvidenceContext, EvidenceItem
from app.evidence.redaction import redact_text
from app.sim.cluster import EvidenceSource

PER_RESULT_MAX_BYTES = 8_000
TOTAL_EVIDENCE_MAX_BYTES = 24_000


class ToolResultError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ToolExecutor:
    """Holds only an EvidenceSource, never a cluster controller or writer."""

    def __init__(
        self,
        source: EvidenceSource,
        context: EvidenceContext,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        per_result_max_bytes: int = PER_RESULT_MAX_BYTES,
        total_max_bytes: int = TOTAL_EVIDENCE_MAX_BYTES,
        initial_evidence: Sequence[EvidenceItem] = (),
    ) -> None:
        if per_result_max_bytes < 1 or total_max_bytes < per_result_max_bytes:
            raise ValueError("invalid evidence byte limits")
        self._source = source
        self._context = context
        self._now = now
        self._per_result_max_bytes = per_result_max_bytes
        self._total_max_bytes = total_max_bytes
        self._registry = ToolRegistry()
        self._functions: dict[str, Callable[..., dict[str, JsonValue]]] = {
            "get_deployment_status": deployment_status,
            "get_readiness_probe": readiness_probe,
            "get_recent_changes": recent_changes,
            "get_logs": logs,
        }
        self._used_bytes = sum(self._payload_size(item.payload) for item in initial_evidence)
        if self._used_bytes > self._total_max_bytes:
            raise ToolResultError("evidence_byte_limit")

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def execute(self, name: str, raw_arguments: object) -> EvidenceItem:
        try:
            arguments = self._registry.parse_arguments(name, raw_arguments)
        except ToolArgumentsError as error:
            raise ToolResultError(error.reason) from error

        snapshot = self._source.snapshot()
        payload = self._functions[name](snapshot, arguments)
        payload = self._redact_payload(payload)
        encoded = canonical_json(payload).encode("utf-8")
        size = len(encoded)
        if size > self._per_result_max_bytes or self._used_bytes + size > self._total_max_bytes:
            raise ToolResultError("evidence_byte_limit")

        content_hash = hashlib.sha256(encoded).hexdigest()
        argument_json = canonical_json(cast(dict[str, JsonValue], raw_arguments))
        identity = "|".join((self._context.run_id, name, argument_json, content_hash))
        evidence_id = "ev-tool-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        observed_at = self._now()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("tool evidence timestamp must be timezone-aware")

        source_type = {
            "get_logs": EvidenceSourceType.LOGS,
            "get_recent_changes": EvidenceSourceType.CHANGE_HISTORY,
        }.get(name, EvidenceSourceType.KUBERNETES)
        record = Evidence(
            schema_version="0.1",
            evidence_id=evidence_id,
            incident_id=self._context.incident_id,
            run_id=self._context.run_id,
            source_type=source_type,
            source_resource=f"sim-tool/{name}",
            query=name,
            observed_at=observed_at,
            freshness=EvidenceFreshness.FRESH,
            content_hash=content_hash,
            payload_reference=f"evidence-store/{evidence_id}",
        )
        self._used_bytes += size
        return EvidenceItem(record=record, payload=payload)

    @staticmethod
    def _payload_size(payload: dict[str, JsonValue]) -> int:
        return len(canonical_json(payload).encode("utf-8"))

    @classmethod
    def _redact_payload(cls, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        def clean(value: JsonValue) -> JsonValue:
            if isinstance(value, str):
                return redact_text(value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value

        return cast(dict[str, JsonValue], clean(payload))
