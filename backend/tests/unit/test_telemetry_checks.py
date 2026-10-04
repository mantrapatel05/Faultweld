from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from app.scenarios.health import MatchState
from app.scenarios.models import ScenarioDefinition
from app.scenarios.registry import load_scenarios
from app.telemetry.checks import (
    DeploymentEvidence,
    SourceObservation,
    SourceState,
    TelemetryParseError,
    classify_source,
    interpret,
    parse_instant_scalar,
    poll_until,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "scenarios" / "fixtures"


def scenario() -> ScenarioDefinition:
    return load_scenarios(FIXTURES)["healthy-replica-loss"]


def observation(
    collector_up: float | None = 1.0,
    target_up: float | None = 1.0,
    age: float | None = 4.0,
) -> SourceObservation:
    return SourceObservation(collector_up=collector_up, target_up=target_up, sample_age_seconds=age)


def evidence(desired: int | None, available: int | None, ready: int | None) -> DeploymentEvidence:
    return DeploymentEvidence(
        desired_replicas=desired, available_replicas=available, ready_pods=ready
    )


def vector(*values: str) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1.0, value]} for value in values],
        },
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps += 1
        self.now += delay


class SourceClassificationTests(TestCase):
    def test_fresh_source_is_available(self) -> None:
        verdict = classify_source(observation())
        self.assertEqual((verdict.state, verdict.reason), (SourceState.AVAILABLE, "ok"))

    def test_age_equal_to_the_limit_is_still_available(self) -> None:
        self.assertEqual(
            classify_source(observation(age=30.0), 30.0).state, SourceState.AVAILABLE
        )

    def test_age_beyond_the_limit_is_stale(self) -> None:
        verdict = classify_source(observation(age=30.1), 30.0)
        self.assertEqual((verdict.state, verdict.reason), (SourceState.STALE, "stale_sample"))

    def test_every_missing_or_down_signal_is_unavailable(self) -> None:
        cases = {
            "collector_unreachable": observation(collector_up=None),
            "target_absent": observation(target_up=None),
            "metric_absent": observation(age=None),
        }
        for reason, case in cases.items():
            with self.subTest(reason=reason):
                verdict = classify_source(case)
                self.assertEqual((verdict.state, verdict.reason), (SourceState.UNAVAILABLE, reason))
        self.assertEqual(
            classify_source(observation(collector_up=0.0)).reason, "collector_unreachable"
        )
        self.assertEqual(classify_source(observation(target_up=0.0)).reason, "target_down")


class InterpretationTests(TestCase):
    def test_available_source_maps_replica_evidence_to_the_fixture_tolerances(self) -> None:
        cases = {
            MatchState.HEALTHY: evidence(1, 1, 1),
            MatchState.FAULTED: evidence(0, 0, None),
            MatchState.INDETERMINATE: evidence(1, 1, 0),
        }
        for expected, case in cases.items():
            with self.subTest(expected=expected):
                reading = interpret(scenario(), observation(), case)
                self.assertEqual(reading.verdict.state, SourceState.AVAILABLE)
                self.assertIs(reading.match, expected)

    def test_unavailable_or_stale_source_never_yields_a_health_reading(self) -> None:
        healthy = evidence(1, 1, 1)
        for case in (
            observation(target_up=0.0),
            observation(target_up=None),
            observation(collector_up=None),
            observation(age=None),
            observation(age=999.0),
        ):
            with self.subTest(case=case):
                self.assertIsNone(interpret(scenario(), case, healthy).match)

    def test_missing_replica_series_is_unknown_not_faulted(self) -> None:
        self.assertIsNone(interpret(scenario(), observation(), evidence(None, None, None)).match)
        self.assertIsNone(interpret(scenario(), observation(), evidence(1, None, 1)).match)


class PrometheusParsingTests(TestCase):
    def test_single_sample_is_parsed(self) -> None:
        self.assertEqual(parse_instant_scalar(vector("2")), 2.0)

    def test_empty_result_means_absent_series(self) -> None:
        self.assertIsNone(parse_instant_scalar(vector()))

    def test_malformed_responses_raise(self) -> None:
        bad: list[object] = [
            "not an object",
            {"status": "error", "errorType": "bad_data"},
            {"status": "success", "data": {"resultType": "matrix", "result": []}},
            {"status": "success", "data": {"resultType": "vector", "result": "x"}},
            vector("1", "2"),
            vector("NaN"),
            vector("+Inf"),
            vector("abc"),
            {"status": "success", "data": {"resultType": "vector", "result": [{"value": [1]}]}},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(TelemetryParseError):
                parse_instant_scalar(payload)


class PollingTests(TestCase):
    def test_reads_immediately_without_sleeping_when_already_accepted(self) -> None:
        fake = FakeClock()
        result = poll_until(
            lambda: 1,
            lambda value: value == 1,
            timeout_seconds=10,
            interval_seconds=2,
            sleep=fake.sleep,
            clock=fake.clock,
        )
        self.assertTrue(result.accepted)
        self.assertEqual((result.elapsed_seconds, fake.sleeps), (0.0, 0))

    def test_waits_for_scrape_latency_and_reports_elapsed_time(self) -> None:
        fake = FakeClock()
        values = iter([0, 0, 0, 1])
        result = poll_until(
            lambda: next(values),
            lambda value: value == 1,
            timeout_seconds=30,
            interval_seconds=2,
            sleep=fake.sleep,
            clock=fake.clock,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.elapsed_seconds, 6.0)

    def test_timeout_is_a_failure_that_keeps_the_last_value(self) -> None:
        fake = FakeClock()
        result = poll_until(
            lambda: 0,
            lambda value: value == 1,
            timeout_seconds=5,
            interval_seconds=2,
            sleep=fake.sleep,
            clock=fake.clock,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.last, 0)
        self.assertGreaterEqual(result.elapsed_seconds, 5.0)

    def test_zero_timeout_still_reads_once(self) -> None:
        fake = FakeClock()
        reads: list[int] = []

        def read() -> int:
            reads.append(1)
            return 0

        result = poll_until(
            read,
            lambda value: value == 1,
            timeout_seconds=0,
            interval_seconds=2,
            sleep=fake.sleep,
            clock=fake.clock,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(len(reads), 1)
