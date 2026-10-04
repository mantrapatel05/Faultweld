"""Faultweld telemetry checks (operator-only test infrastructure).

``status`` prints what the telemetry pipeline reports right now. ``verify`` proves, against
the live disposable cluster, that telemetry reflects injected faults and that a broken
telemetry source is detected instead of being read as healthy.

Reads go through the Kubernetes API server proxy to Prometheus, so no port-forward is
needed. The only mutations are scenario start/reset (delegated to scripts/scenario.py)
and scaling the telemetry deployment ``faultweld-kube-state-metrics``.
Run with PYTHONPATH pointing at ``backend`` as for scripts/scenario.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.scenarios.health import MatchState
from app.scenarios.models import ScenarioDefinition
from app.scenarios.registry import load_scenarios
from app.telemetry.checks import (
    DeploymentEvidence,
    PollResult,
    SourceObservation,
    SourceState,
    TelemetryParseError,
    TelemetryReading,
    interpret,
    parse_instant_scalar,
    poll_until,
)

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SCENARIO_SCRIPT = ROOT / "scripts" / "scenario.py"
CONTEXT = "kind-faultweld"
NAMESPACE = "faultweld-demo"
SAMPLE_DEPLOYMENT = "faultweld-sample"
KSM_DEPLOYMENT = "faultweld-kube-state-metrics"
KSM_JOB = "kube-state-metrics"
COLLECTOR_JOB = "otel-collector"
PROMETHEUS_SERVICE = "faultweld-prometheus"
PROMETHEUS_PORT = 9090

POLL_INTERVAL_SECONDS = 2.0
DEFAULT_SOURCE_TIMEOUT_SECONDS = 120.0
DEFAULT_FAULT_TIMEOUT_SECONDS = 90.0

PERSISTENT_SCENARIOS = ("healthy-replica-loss", "readiness-regression")
NOT_ASSERTED = {
    "deployment-unavailable": (
        "transient fault: the deleted pod is replaced faster than a 5s scrape interval "
        "reliably observes; use `scripts/scenario.py health` immediately after start"
    )
}

SELECTOR = f"namespace='{NAMESPACE}',deployment='{SAMPLE_DEPLOYMENT}'"
QUERY_COLLECTOR_UP = f"min(up{{job='{COLLECTOR_JOB}'}})"
QUERY_TARGET_UP = f"min(up{{job='{KSM_JOB}'}})"
QUERY_SAMPLE_AGE = (
    f"time() - max(timestamp(kube_deployment_status_replicas_available{{{SELECTOR}}}))"
)
QUERY_DESIRED = f"sum(kube_deployment_spec_replicas{{{SELECTOR}}})"
QUERY_AVAILABLE = f"sum(kube_deployment_status_replicas_available{{{SELECTOR}}})"
QUERY_READY_PODS = (
    f"sum(kube_pod_status_ready{{namespace='{NAMESPACE}',condition='true',"
    f"pod=~'{SAMPLE_DEPLOYMENT}-.*'}})"
)


class TelemetryError(RuntimeError):
    pass


def run(command: Sequence[str], *, capture: bool = False) -> str:
    try:
        result = subprocess.run(list(command), check=False, capture_output=capture, text=True)
    except OSError as error:
        raise TelemetryError(f"could not run {command[0]!r}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() if capture else ""
        suffix = f": {detail}" if detail else ""
        raise TelemetryError(f"command failed: {' '.join(command)}{suffix}")
    return result.stdout.strip() if capture else ""


def require_scope() -> None:
    context = run(["kubectl", "config", "current-context"], capture=True)
    if context != CONTEXT:
        raise TelemetryError(
            f"refusing operation outside context {CONTEXT!r}; found {context!r}"
        )
    namespace: dict[str, Any] = json.loads(
        run(["kubectl", "get", "namespace", NAMESPACE, "-o", "json"], capture=True)
    )
    labels = namespace.get("metadata", {}).get("labels", {})
    if labels.get("faultweld.dev/managed") != "true":
        raise TelemetryError(f"refusing operation on unlabelled namespace {NAMESPACE!r}")


def prom_query(query: str) -> float | None:
    path = (
        f"/api/v1/namespaces/{NAMESPACE}/services/{PROMETHEUS_SERVICE}:{PROMETHEUS_PORT}"
        f"/proxy/api/v1/query?query={quote(query, safe='')}"
    )
    payload = json.loads(run(["kubectl", "get", "--raw", path], capture=True))
    try:
        return parse_instant_scalar(payload)
    except TelemetryParseError as error:
        raise TelemetryError(f"{error} (query: {query})") from error


def to_count(value: float | None) -> int | None:
    return None if value is None else round(value)


def read_source_observation() -> SourceObservation:
    return SourceObservation(
        collector_up=prom_query(QUERY_COLLECTOR_UP),
        target_up=prom_query(QUERY_TARGET_UP),
        sample_age_seconds=prom_query(QUERY_SAMPLE_AGE),
    )


def read_evidence() -> DeploymentEvidence:
    return DeploymentEvidence(
        desired_replicas=to_count(prom_query(QUERY_DESIRED)),
        available_replicas=to_count(prom_query(QUERY_AVAILABLE)),
        ready_pods=to_count(prom_query(QUERY_READY_PODS)),
    )


def read_reading(scenario: ScenarioDefinition) -> TelemetryReading:
    return interpret(scenario, read_source_observation(), read_evidence())


def summarize(reading: TelemetryReading) -> str:
    match = reading.match.value if reading.match is not None else "none"
    return f"source={reading.verdict.state.value}/{reading.verdict.reason}, health={match}"


def scale_ksm(replicas: int) -> None:
    require_scope()
    run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "scale",
            f"deployment/{KSM_DEPLOYMENT}",
            f"--replicas={replicas}",
        ]
    )


def await_ksm_rollout() -> None:
    run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deployment/{KSM_DEPLOYMENT}",
            "--timeout=120s",
        ]
    )


def run_scenario(command: str, scenario_id: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    result = subprocess.run(
        [sys.executable, str(SCENARIO_SCRIPT), command, scenario_id],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise TelemetryError(
            f"scenario {command} {scenario_id} failed: {result.stderr.strip() or result.stdout}"
        )


def wait_for(
    scenario: ScenarioDefinition, expected: MatchState, timeout: float
) -> PollResult[TelemetryReading]:
    return poll_until(
        lambda: read_reading(scenario),
        lambda reading: (
            reading.verdict.state is SourceState.AVAILABLE and reading.match is expected
        ),
        timeout_seconds=timeout,
        interval_seconds=POLL_INTERVAL_SECONDS,
        sleep=time.sleep,
        clock=time.monotonic,
    )


def seconds(result: PollResult[TelemetryReading]) -> float | None:
    return round(result.elapsed_seconds, 1) if result.accepted else None


def phase_failure(phase: str, expected: str, result: PollResult[TelemetryReading]) -> str:
    return (
        f"{phase}: expected telemetry to report {expected} within "
        f"{result.elapsed_seconds:.1f}s but the last reading was {summarize(result.last)}"
    )


def verify_scenario(
    scenario: ScenarioDefinition, *, source_timeout: float, fault_timeout: float
) -> dict[str, Any]:
    failures: list[str] = []
    run_scenario("reset", scenario.scenario_id)
    baseline = wait_for(scenario, MatchState.HEALTHY, source_timeout)
    if not baseline.accepted:
        failures.append(phase_failure("baseline", MatchState.HEALTHY.value, baseline))
    try:
        run_scenario("start", scenario.scenario_id)
        faulted = wait_for(scenario, MatchState.FAULTED, fault_timeout)
        if not faulted.accepted:
            failures.append(phase_failure("fault", MatchState.FAULTED.value, faulted))
    finally:
        run_scenario("reset", scenario.scenario_id)
    run_scenario("reset", scenario.scenario_id)
    reset = wait_for(scenario, MatchState.HEALTHY, source_timeout)
    if not reset.accepted:
        failures.append(phase_failure("reset", MatchState.HEALTHY.value, reset))
    return {
        "scenario_id": scenario.scenario_id,
        "baseline_observed_after_seconds": seconds(baseline),
        "fault_observed_after_seconds": seconds(faulted),
        "reset_observed_after_seconds": seconds(reset),
        "passed": not failures,
        "failures": failures,
    }


def verify_source_outage(scenario: ScenarioDefinition, *, source_timeout: float) -> dict[str, Any]:
    failures: list[str] = []
    detected_as: str | None = None
    detected_after: float | None = None
    scale_ksm(0)
    try:
        outage = poll_until(
            lambda: read_reading(scenario),
            lambda reading: reading.verdict.state is not SourceState.AVAILABLE,
            timeout_seconds=source_timeout,
            interval_seconds=POLL_INTERVAL_SECONDS,
            sleep=time.sleep,
            clock=time.monotonic,
        )
        if outage.accepted:
            detected_after = round(outage.elapsed_seconds, 1)
            detected_as = f"{outage.last.verdict.state.value}/{outage.last.verdict.reason}"
            if outage.last.match is not None:
                failures.append("health was interpreted from an unavailable source")
        else:
            failures.append(
                f"outage: the source still looked available after {outage.elapsed_seconds:.1f}s "
                f"({summarize(outage.last)})"
            )
    finally:
        scale_ksm(1)
    await_ksm_rollout()
    recovered = wait_for(scenario, MatchState.HEALTHY, source_timeout)
    if not recovered.accepted:
        failures.append(phase_failure("recovery", MatchState.HEALTHY.value, recovered))
    return {
        "detected_as": detected_as,
        "detected_after_seconds": detected_after,
        "recovered_after_seconds": seconds(recovered),
        "passed": not failures,
        "failures": failures,
    }


def verify_all(*, source_timeout: float, fault_timeout: float) -> dict[str, Any]:
    require_scope()
    scenarios = load_scenarios()
    missing = [item for item in PERSISTENT_SCENARIOS if item not in scenarios]
    if missing:
        raise TelemetryError(f"unknown scenarios: {', '.join(missing)}")
    ordered = [scenarios[item] for item in PERSISTENT_SCENARIOS]
    results = [
        verify_scenario(scenario, source_timeout=source_timeout, fault_timeout=fault_timeout)
        for scenario in ordered
    ]
    outage = verify_source_outage(ordered[0], source_timeout=source_timeout)
    return {
        "environment_id": CONTEXT,
        "namespace": NAMESPACE,
        "passed": all(item["passed"] for item in results) and outage["passed"],
        "scenarios": results,
        "source_outage": outage,
        "not_asserted": [
            {"scenario_id": scenario_id, "reason": reason}
            for scenario_id, reason in NOT_ASSERTED.items()
        ],
    }


def status(scenario: ScenarioDefinition) -> tuple[dict[str, Any], bool]:
    observation = read_source_observation()
    evidence = read_evidence()
    reading = interpret(scenario, observation, evidence)
    payload = {
        "scenario_id": scenario.scenario_id,
        "source": {"state": reading.verdict.state.value, "reason": reading.verdict.reason},
        "health": reading.match.value if reading.match is not None else None,
        "observation": observation.model_dump(),
        "evidence": evidence.model_dump(),
    }
    return payload, reading.verdict.state is SourceState.AVAILABLE


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the Faultweld telemetry pipeline.")
    parser.add_argument("command", choices=("status", "verify"))
    parser.add_argument("scenario_id", nargs="?")
    parser.add_argument("--source-timeout", type=float, default=DEFAULT_SOURCE_TIMEOUT_SECONDS)
    parser.add_argument("--fault-timeout", type=float, default=DEFAULT_FAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if args.source_timeout < 0 or args.fault_timeout < 0:
        parser.error("timeouts must not be negative")

    try:
        if args.command == "verify":
            if args.scenario_id is not None:
                parser.error("verify covers fixed scenarios; do not pass a scenario ID")
            report = verify_all(
                source_timeout=args.source_timeout, fault_timeout=args.fault_timeout
            )
            print(json.dumps(report, sort_keys=True, indent=2))
            return 0 if report["passed"] else 1

        require_scope()
        scenarios = load_scenarios()
        scenario_id = args.scenario_id or PERSISTENT_SCENARIOS[0]
        if scenario_id not in scenarios:
            parser.error(f"unknown scenario: {scenario_id}")
        payload, available = status(scenarios[scenario_id])
        print(json.dumps(payload, sort_keys=True, indent=2))
        return 0 if available else 1
    except TelemetryError as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
