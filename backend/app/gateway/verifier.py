"""Read-only post-action verification; writer results are never consulted."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.agent.ids import IdFactory, uuid_id
from app.domain.schemas import (
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)
from app.scenarios.models import ScenarioDefinition
from app.sim.cluster import EvidenceSource, SimSnapshot


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class VerifierConfig:
    consecutive_healthy: int = 2
    window_ticks: int = 6
    expected_readiness_path: str = "/"
    expected_readiness_port: str = "http"

    def __post_init__(self) -> None:
        if self.consecutive_healthy < 1 or self.window_ticks < 1:
            raise ValueError("verification bounds must be positive")


def verify(
    scenario: ScenarioDefinition,
    source: EvidenceSource,
    advance: Callable[[int], None],
    config: VerifierConfig,
    *,
    incident_id: str,
    attempt_id: str,
    clock: Callable[[], datetime] = _utc_now,
    ids: IdFactory = uuid_id,
) -> VerificationResult:
    """Require consecutive healthy snapshots within a fixed number of logical ticks."""
    previous_tick: int | None = None
    healthy_streak = 0
    last_snapshot: SimSnapshot | None = None
    for index in range(config.window_ticks + 1):
        try:
            snapshot = source.snapshot()
        except Exception:
            return _unknown(
                scenario, incident_id, attempt_id, config, clock, ids, "state_unavailable"
            )
        if previous_tick is not None and snapshot.tick < previous_tick:
            return _unknown(
                scenario, incident_id, attempt_id, config, clock, ids, "tick_went_backwards"
            )
        observation = snapshot.observation
        if (
            observation.ready_replicas > observation.available_replicas
            or observation.available_replicas > observation.desired_replicas
            or observation.ready_replicas > observation.desired_replicas
        ):
            return _unknown(
                scenario, incident_id, attempt_id, config, clock, ids, "contradictory_state"
            )
        previous_tick = snapshot.tick
        last_snapshot = snapshot
        checks = _checks(scenario, snapshot, config, clock())
        if all(check.passed is True for check in checks[:3]):
            healthy_streak += 1
        else:
            healthy_streak = 0
        if healthy_streak >= config.consecutive_healthy:
            recovered_checks = _checks(scenario, snapshot, config, clock())
            return VerificationResult(
                schema_version="0.1",
                verification_id=ids("verification"),
                incident_id=incident_id,
                attempt_id=attempt_id,
                status=VerificationStatus.RECOVERED,
                checks=recovered_checks,
                observed_at=clock(),
                reason_code=None,
            )
        if index < config.window_ticks:
            try:
                advance(1)
            except Exception:
                return _unknown(
                    scenario, incident_id, attempt_id, config, clock, ids, "advance_unavailable"
                )

    assert last_snapshot is not None
    final_checks = list(_checks(scenario, last_snapshot, config, clock()))
    final_checks.append(
        VerificationCheck(
            schema_version="0.1",
            check_id="consecutive-healthy",
            source="simulator",
            observed_at=clock(),
            passed=False,
            observed_value=str(healthy_streak),
            expected_value=str(config.consecutive_healthy),
        )
    )
    return VerificationResult(
        schema_version="0.1",
        verification_id=ids("verification"),
        incident_id=incident_id,
        attempt_id=attempt_id,
        status=VerificationStatus.NOT_RECOVERED,
        checks=tuple(final_checks),
        observed_at=clock(),
        reason_code="postconditions_not_met",
    )


def _checks(
    scenario: ScenarioDefinition,
    snapshot: SimSnapshot,
    config: VerifierConfig,
    now: datetime,
) -> tuple[VerificationCheck, ...]:
    observation = snapshot.observation
    checks = (
        (
            "deployment-available",
            observation.available_replicas >= scenario.health.healthy_replicas,
            str(observation.available_replicas),
            str(scenario.health.healthy_replicas),
        ),
        (
            "pods-ready",
            observation.ready_replicas >= scenario.health.healthy_replicas,
            str(observation.ready_replicas),
            str(scenario.health.healthy_replicas),
        ),
        (
            "probe-config-correct",
            snapshot.readiness_path == config.expected_readiness_path
            and str(snapshot.readiness_port) == config.expected_readiness_port,
            f"{snapshot.readiness_path}:{snapshot.readiness_port}",
            f"{config.expected_readiness_path}:{config.expected_readiness_port}",
        ),
    )
    return tuple(
        VerificationCheck(
            schema_version="0.1",
            check_id=check_id,
            source="simulator",
            observed_at=now,
            passed=passed,
            observed_value=observed,
            expected_value=expected,
        )
        for check_id, passed, observed, expected in checks
    )


def _unknown(
    scenario: ScenarioDefinition,
    incident_id: str,
    attempt_id: str,
    config: VerifierConfig,
    clock: Callable[[], datetime],
    ids: IdFactory,
    reason_code: str,
) -> VerificationResult:
    now = clock()
    checks = tuple(
        VerificationCheck(
            schema_version="0.1",
            check_id=check_id,
            source="simulator",
            observed_at=now,
            passed=None,
            observed_value=None,
            expected_value=expected,
        )
        for check_id, expected in (
            ("deployment-available", str(scenario.health.healthy_replicas)),
            ("pods-ready", str(scenario.health.healthy_replicas)),
            (
                "probe-config-correct",
                f"{config.expected_readiness_path}:{config.expected_readiness_port}",
            ),
            ("consecutive-healthy", str(config.consecutive_healthy)),
        )
    )
    return VerificationResult(
        schema_version="0.1",
        verification_id=ids("verification"),
        incident_id=incident_id,
        attempt_id=attempt_id,
        status=VerificationStatus.UNKNOWN,
        checks=checks,
        observed_at=now,
        reason_code=reason_code,
    )


__all__ = ["VerifierConfig", "verify"]
