"""Pure interpretation of telemetry observations for the Faultweld sample service.

This module performs no cluster or network calls. It decides whether the telemetry
*source* can be trusted and, only if it can, what health the telemetry reports.

The central rule: an unavailable, absent, or stale source never yields a health
reading. "No data" is unknown, not healthy and not faulted.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.scenarios.health import HealthObservation, MatchState, observed_state
from app.scenarios.models import ScenarioDefinition

DEFAULT_MAX_SAMPLE_AGE_SECONDS = 30.0
NonNegativeCount = Annotated[int, Field(ge=0)]


class TelemetryParseError(ValueError):
    pass


class SourceState(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class SourceObservation(BaseModel):
    """What Prometheus says about the pipeline. ``None`` means the series is absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collector_up: float | None
    target_up: float | None
    sample_age_seconds: float | None


class SourceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: SourceState
    reason: str


class DeploymentEvidence(BaseModel):
    """Replica evidence for the sample deployment. ``None`` means the series is absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    desired_replicas: NonNegativeCount | None
    available_replicas: NonNegativeCount | None
    ready_pods: NonNegativeCount | None


@dataclass(frozen=True)
class TelemetryReading:
    verdict: SourceVerdict
    match: MatchState | None


@dataclass(frozen=True)
class PollResult[T]:
    accepted: bool
    elapsed_seconds: float
    last: T


def classify_source(
    observation: SourceObservation,
    max_age_seconds: float = DEFAULT_MAX_SAMPLE_AGE_SECONDS,
) -> SourceVerdict:
    if observation.collector_up is None or observation.collector_up < 1:
        return SourceVerdict(state=SourceState.UNAVAILABLE, reason="collector_unreachable")
    if observation.target_up is None:
        return SourceVerdict(state=SourceState.UNAVAILABLE, reason="target_absent")
    if observation.target_up < 1:
        return SourceVerdict(state=SourceState.UNAVAILABLE, reason="target_down")
    if observation.sample_age_seconds is None:
        return SourceVerdict(state=SourceState.UNAVAILABLE, reason="metric_absent")
    if observation.sample_age_seconds > max_age_seconds:
        return SourceVerdict(state=SourceState.STALE, reason="stale_sample")
    return SourceVerdict(state=SourceState.AVAILABLE, reason="ok")


def to_health_observation(evidence: DeploymentEvidence) -> HealthObservation | None:
    """Convert evidence to the same shape the scenario controller uses.

    Pod-ready series disappear when no pod exists, so a missing ``ready_pods`` series is
    read as zero. That is only valid once the source itself is known to be available,
    which ``interpret`` enforces before calling this.
    """
    if evidence.desired_replicas is None or evidence.available_replicas is None:
        return None
    return HealthObservation(
        desired_replicas=evidence.desired_replicas,
        available_replicas=evidence.available_replicas,
        ready_replicas=evidence.ready_pods or 0,
    )


def interpret(
    scenario: ScenarioDefinition,
    observation: SourceObservation,
    evidence: DeploymentEvidence,
    max_age_seconds: float = DEFAULT_MAX_SAMPLE_AGE_SECONDS,
) -> TelemetryReading:
    verdict = classify_source(observation, max_age_seconds)
    if verdict.state is not SourceState.AVAILABLE:
        return TelemetryReading(verdict=verdict, match=None)
    health = to_health_observation(evidence)
    if health is None:
        return TelemetryReading(verdict=verdict, match=None)
    return TelemetryReading(verdict=verdict, match=observed_state(scenario, health))


def parse_instant_scalar(payload: object) -> float | None:
    """Parse a Prometheus instant-query response holding at most one sample."""
    if not isinstance(payload, Mapping):
        raise TelemetryParseError("prometheus response is not a JSON object")
    if payload.get("status") != "success":
        raise TelemetryParseError(f"prometheus query failed: {payload.get('errorType')!r}")
    data = payload.get("data")
    if not isinstance(data, Mapping) or data.get("resultType") != "vector":
        raise TelemetryParseError("prometheus response is not an instant vector")
    result = data.get("result")
    if not isinstance(result, list):
        raise TelemetryParseError("prometheus result is not a list")
    if not result:
        return None
    if len(result) > 1:
        raise TelemetryParseError("query returned several series; aggregate it first")
    sample = result[0]
    value = sample.get("value") if isinstance(sample, Mapping) else None
    if not isinstance(value, list) or len(value) != 2:
        raise TelemetryParseError("prometheus sample has no [timestamp, value] pair")
    try:
        number = float(value[1])
    except (TypeError, ValueError) as error:
        raise TelemetryParseError("prometheus sample value is not numeric") from error
    if not math.isfinite(number):
        raise TelemetryParseError("prometheus sample value is not finite")
    return number


def poll_until[T](
    read: Callable[[], T],
    accept: Callable[[T], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> PollResult[T]:
    """Read at least once, immediately, then wait for scrape latency up to a deadline.

    This waits for a pipeline with a known scrape interval to catch up. It is not a
    retry of a failed assertion: the elapsed time is returned so lag is measured, and
    a timeout is a failure.
    """
    start = clock()
    while True:
        value = read()
        elapsed = clock() - start
        if accept(value):
            return PollResult(accepted=True, elapsed_seconds=elapsed, last=value)
        if elapsed >= timeout_seconds:
            return PollResult(accepted=False, elapsed_seconds=elapsed, last=value)
        sleep(interval_seconds)
