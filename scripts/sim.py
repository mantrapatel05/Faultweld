"""Faultweld simulated cluster runner (no Docker, no Kubernetes).

``run`` walks one fixture through baseline, fault, reset, and a repeated reset, printing the
health observed at every step. ``watch`` injects a fault and prints health tick by tick, so a
transient fault such as a deleted pod is visible. ``verify`` repeats the cycle for every
fixture and grades it with the same repeatability gate used for the live cluster.
Each command starts a fresh simulation: state does not persist between invocations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.scenarios.health import HealthObservation  # noqa: E402
from app.scenarios.models import SCENARIO_SCHEMA_VERSION, ScenarioDefinition  # noqa: E402
from app.scenarios.registry import load_scenarios  # noqa: E402
from app.scenarios.repeatability import (  # noqa: E402
    RepeatabilityReport,
    build_cycle,
    build_report,
    summarize,
)
from app.sim.cluster import REPLACEMENT_TICKS, SimCluster, SimError  # noqa: E402

DEFAULT_REPEATS = 3
MAX_WATCH_TICKS = 20


def step(cluster: SimCluster, scenario: ScenarioDefinition, label: str) -> dict[str, Any]:
    observation = cluster.observe()
    return {
        "step": label,
        "tick": cluster.state.tick,
        "state": cluster.classify(scenario).value,
        "desired_replicas": observation.desired_replicas,
        "available_replicas": observation.available_replicas,
        "ready_replicas": observation.ready_replicas,
        "resource_version": cluster.state.resource_version,
    }


def run_cycle(cluster: SimCluster, scenario: ScenarioDefinition) -> list[HealthObservation]:
    """Baseline, immediate fault observation, reset, repeated reset. No artificial delay."""
    cluster.reset()
    baseline = cluster.observe()
    cluster.inject_fault(scenario)
    faulted = cluster.observe()
    cluster.reset()
    first_reset = cluster.observe()
    cluster.reset()
    return [baseline, faulted, first_reset, cluster.observe()]


def verify_all(repeats: int) -> RepeatabilityReport:
    scenarios = load_scenarios()
    ordered = [scenarios[scenario_id] for scenario_id in sorted(scenarios)]
    verdicts = []
    for scenario in ordered:
        cluster = SimCluster()
        cycles = [
            build_cycle(scenario, number, run_cycle(cluster, scenario))
            for number in range(1, repeats + 1)
        ]
        verdicts.append(summarize(scenario, cycles))
    return build_report(ordered, verdicts, repeats, SCENARIO_SCHEMA_VERSION)


def report_payload(report: RepeatabilityReport) -> dict[str, Any]:
    return {
        "mode": "sim",
        "schema_version": report.schema_version,
        "environment_id": report.environment_id,
        "namespace": report.namespace,
        "target_deployment": report.target_deployment,
        "repeats": report.repeats,
        "passed": report.passed,
        "scenarios": [
            {
                "scenario_id": verdict.scenario_id,
                "fixture_version": verdict.fixture_version,
                "repeats": verdict.repeats,
                "cycles_passed": verdict.cycles_passed,
                "passed": verdict.passed,
                "failures": list(verdict.failures),
            }
            for verdict in report.verdicts
        ],
    }


def run_payload(scenario: ScenarioDefinition) -> list[dict[str, Any]]:
    cluster = SimCluster()
    steps = [step(cluster, scenario, "baseline")]
    cluster.inject_fault(scenario)
    steps.append(step(cluster, scenario, "after_fault"))
    cluster.reset()
    steps.append(step(cluster, scenario, "after_reset"))
    cluster.reset()
    steps.append(step(cluster, scenario, "after_second_reset"))
    return steps


def watch_payload(scenario: ScenarioDefinition, ticks: int) -> list[dict[str, Any]]:
    cluster = SimCluster()
    cluster.inject_fault(scenario)
    rows = [step(cluster, scenario, "after_fault")]
    for _ in range(ticks):
        cluster.tick()
        rows.append(step(cluster, scenario, "tick"))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Faultweld fixtures on the simulation.")
    parser.add_argument("command", choices=("run", "watch", "verify"))
    parser.add_argument("scenario_id", nargs="?")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--ticks", type=int, default=REPLACEMENT_TICKS + 1)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if not 0 <= args.ticks <= MAX_WATCH_TICKS:
        parser.error(f"--ticks must be between 0 and {MAX_WATCH_TICKS}")

    if args.command == "verify":
        if args.scenario_id is not None:
            parser.error("verify covers every fixture; do not pass a scenario ID")
        report = verify_all(args.repeats)
        print(json.dumps(report_payload(report), sort_keys=True, indent=2))
        return 0 if report.passed else 1

    if args.scenario_id is None:
        parser.error(f"{args.command} requires a scenario ID")
    scenarios = load_scenarios()
    if args.scenario_id not in scenarios:
        parser.error(f"unknown scenario: {args.scenario_id}")
    scenario = scenarios[args.scenario_id]
    try:
        rows = (
            run_payload(scenario)
            if args.command == "run"
            else watch_payload(scenario, args.ticks)
        )
    except SimError as error:
        parser.error(str(error))
    print(json.dumps(rows, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
