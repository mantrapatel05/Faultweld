"""Read models needed by the HTTP API.

The core Store intentionally stays focused on write-side lifecycle operations. This small
adapter provides ordered list queries without exposing SQLite access to FastAPI route code.
It lives beside Store because it is coupled to the existing SQLite schema.
"""

from __future__ import annotations

from app.domain.schemas import ActionAttempt, Incident
from app.store.models import StoredProposal
from app.store.store import Store


class StoreQueries:
    def __init__(self, store: Store) -> None:
        self._store = store

    def list_incidents(self) -> tuple[Incident, ...]:
        rows = self._store._conn().execute(
            "SELECT json FROM incidents ORDER BY updated_at DESC, incident_id DESC"
        ).fetchall()
        return tuple(Incident.model_validate_json(row["json"]) for row in rows)

    def list_proposals_for_incident(self, incident_id: str) -> tuple[StoredProposal, ...]:
        rows = self._store._conn().execute(
            """SELECT proposal_id, incident_id, digest, json, state, version
               FROM proposals WHERE incident_id = ? ORDER BY rowid DESC""",
            (incident_id,),
        ).fetchall()
        return tuple(self._store._stored_proposal(row) for row in rows)

    def list_attempts_for_incident(self, incident_id: str) -> tuple[ActionAttempt, ...]:
        rows = self._store._conn().execute(
            """SELECT attempts.json FROM attempts
               JOIN proposals ON proposals.proposal_id = attempts.proposal_id
               WHERE proposals.incident_id = ? ORDER BY attempts.rowid""",
            (incident_id,),
        ).fetchall()
        return tuple(ActionAttempt.model_validate_json(row["json"]) for row in rows)


__all__ = ["StoreQueries"]
