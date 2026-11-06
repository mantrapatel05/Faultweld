from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue

from app.domain.schemas import Evidence, NonEmptyId


class ReadOutcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"


class EvidenceContext(BaseModel):
    """Server-derived identifiers that a read is attributed to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: NonEmptyId
    run_id: NonEmptyId


class EvidenceReadError(RuntimeError):
    """A read that must not be treated as evidence. ``code`` is a stable reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class EvidenceItem:
    """An evidence record plus its bounded, redacted payload.

    The evidence store arrives in a later version; until then the payload travels with
    the record and ``record.payload_reference`` names where it will live.
    """

    record: Evidence
    payload: dict[str, JsonValue]
