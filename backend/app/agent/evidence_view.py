"""Evidence records built from the read-only simulated-cluster snapshot.

The snapshot payload is a whitelist projection: exactly what the fixtures can change plus
the two revision counters. Everything is hashed with the canonical JSON encoding, and the
evidence ID is derived from the run, the source resource and the content hash so the same
observation always yields the same ID.

Callers may add further evidence items (alerts, stale guidance, injection text). Those are
untrusted input: they are built with the same helper and carry an explicit trust marker in
their payload. Nothing here trusts that marker; it exists so a reviewer can see the source.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime

from pydantic import JsonValue

from app.agent.identity import DEPLOYMENT_KIND, DEPLOYMENT_NAME, NAMESPACE
from app.domain.schemas import Evidence, EvidenceFreshness, EvidenceSourceType
from app.evidence.canonical import canonical_json
from app.evidence.models import EvidenceContext, EvidenceItem
from app.sim.cluster import SimSnapshot

SOURCE_RESOURCE = f"{DEPLOYMENT_KIND}/{NAMESPACE}/{DEPLOYMENT_NAME}"
SNAPSHOT_QUERY = f"sim.snapshot {SOURCE_RESOURCE}"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence timestamps must be timezone-aware")
    return value


def evidence_item(
    *,
    context: EvidenceContext,
    source_resource: str,
    query: str,
    payload: Mapping[str, JsonValue],
    observed_at: datetime,
    source_type: EvidenceSourceType = EvidenceSourceType.KUBERNETES,
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH,
    evidence_id_prefix: str = "ev",
) -> EvidenceItem:
    """Build one evidence record and its payload, with a content-derived ID."""
    if not evidence_id_prefix:
        raise ValueError("evidence_id_prefix must not be empty")
    body = dict(payload)
    content_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    identity = f"{context.run_id}|{source_resource}|{content_hash}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    evidence_id = f"{evidence_id_prefix}-{digest}"
    record = Evidence(
        schema_version="0.1",
        evidence_id=evidence_id,
        incident_id=context.incident_id,
        run_id=context.run_id,
        source_type=source_type,
        source_resource=source_resource,
        query=query,
        observed_at=_require_aware(observed_at),
        freshness=freshness,
        content_hash=content_hash,
        payload_reference=f"evidence-store/{evidence_id}",
    )
    return EvidenceItem(record=record, payload=body)


def snapshot_payload(snapshot: SimSnapshot) -> dict[str, JsonValue]:
    """The bounded, deployment-level projection of one snapshot."""
    return {
        "desired_replicas": snapshot.observation.desired_replicas,
        "available_replicas": snapshot.observation.available_replicas,
        "ready_replicas": snapshot.observation.ready_replicas,
        "readiness_path": snapshot.readiness_path,
        "readiness_port": snapshot.readiness_port,
        "revision": snapshot.revision,
        "resource_version": snapshot.resource_version,
        "tick": snapshot.tick,
    }


def snapshot_evidence(
    snapshot: SimSnapshot,
    *,
    context: EvidenceContext,
    observed_at: datetime,
) -> EvidenceItem:
    """Turn a simulation snapshot into fresh Kubernetes evidence."""
    return evidence_item(
        context=context,
        source_resource=SOURCE_RESOURCE,
        query=SNAPSHOT_QUERY,
        payload=snapshot_payload(snapshot),
        observed_at=observed_at,
        source_type=EvidenceSourceType.KUBERNETES,
        freshness=EvidenceFreshness.FRESH,
        evidence_id_prefix="ev-sim",
    )
