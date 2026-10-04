"""Faultweld scenario controller (operator-only test infrastructure).

Applies and resets known scenario fixtures with the scenario-controller
identity. This entrypoint is infrastructure for local testing and must never
be exposed as a model tool or to the read-only agent identity.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.scenarios.health import (
    HealthObservation,
    classify_health,
    observe_deployment,
)
from app.scenarios.models import SCENARIO_SCHEMA_VERSION, ScenarioDefinition
from app.scenarios.registry import load_scenarios
from app.scenarios.repeatability import (
    RepeatabilityReport,
    ScenarioVerdict,
    build_cycle,
    build_report,
    summarize,
)

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = ROOT / "scripts" / "cluster.py"
NAMESPACE = "faultweld-demo"
DEPLOYMENT = "faultweld-sample"
DEFAULT_REPEATS = 3


class ScenarioSafetyError(RuntimeError):
    pass


def run(command: Sequence[str], *, capture: bool = False) -> str:
    result = subprocess.run(list(command), check=False, capture_output=capture, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() if capture else ""
        suffix = f": {detail}" if detail else ""
        raise ScenarioSafetyError(f"command failed: {' '.join(command)}{suffix}")
    return result.stdout.strip() if capture else ""


def project_context() -> None:
    run(["python", str(CLUSTER), "reset"])


def require_scenario_scope(scenario: ScenarioDefinition) -> None:
    if scenario.namespace != NAMESPACE or scenario.target_deployment != DEPLOYMENT:
        raise ScenarioSafetyError(
            f"refusing scenario outside {NAMESPACE}/{DEPLOYMENT}: "
            f"{scenario.namespace}/{scenario.target_deployment}"
        )


def fault_command(scenario: ScenarioDefinition) -> list[str]:
    fault = scenario.fault
    if fault.kind == "scale":
        if fault.replicas is None:
            raise ScenarioSafetyError("scale fault is missing replicas")
        return [
            "kubectl",
            "-n",
            NAMESPACE,
            "scale",
            f"deployment/{DEPLOYMENT}",
            f"--replicas={fault.replicas}",
        ]
    if fault.kind == "readiness":
        if fault.path is None and fault.port is None:
            raise ScenarioSafetyError("readiness fault is missing path or port")
        ops: list[dict[str, Any]] = []
        if fault.path is not None:
            ops.append(
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/readinessProbe/httpGet/path",
                    "value": fault.path,
                }
            )
        if fault.port is not None:
            ops.append(
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/readinessProbe/httpGet/port",
                    "value": fault.port,
                }
            )
        patch = json.dumps(ops)
        return [
            "kubectl",
            "-n",
            NAMESPACE,
            "patch",
            f"deployment/{DEPLOYMENT}",
            "--type=json",
            "-p",
            patch,
        ]
    if fault.selector is None:
        raise ScenarioSafetyError("delete_pod fault is missing selector")
    return [
        "kubectl",
        "-n",
        NAMESPACE,
        "delete",
        "pod",
        "-l",
        fault.selector,
        "--wait=false",
    ]


def apply_fault(scenario: ScenarioDefinition) -> None:
    require_scenario_scope(scenario)
    command = fault_command(scenario)
    project_context()
    run(command)


def reset_scenario(scenario: ScenarioDefinition) -> None:
    require_scenario_scope(scenario)
    project_context()


def health(scenario: ScenarioDefinition) -> dict[str, Any]:
    require_scenario_scope(scenario)
    payload = run(
        ["kubectl", "-n", NAMESPACE, "get", f"deployment/{DEPLOYMENT}", "-o", "json"],
        capture=True,
    )
    deployment: dict[str, Any] = json.loads(payload)
    observation = observe_deployment(deployment)
    status = deployment.get("status", {})
    observed_generation = status.get("observedGeneration") if isinstance(status, dict) else None
    return {
        "scenario_id": scenario.scenario_id,
        "desired_replicas": observation.desired_replicas,
        "available_replicas": observation.available_replicas,
        "ready_replicas": observation.ready_replicas,
        "observed_generation": observed_generation,
        "state": classify_health(scenario, observation),
    }


def observe_current_state() -> HealthObservation:
    payload = run(
        ["kubectl", "-n", NAMESPACE, "get", f"deployment/{DEPLOYMENT}", "-o", "json"],
        capture=True,
    )
    deployment: dict[str, Any] = json.loads(payload)
    return observe_deployment(deployment)


def run_cycle(scenario: ScenarioDefinition) -> list[HealthObservation]:
    """Inject, observe, reset, and observe again, recording each observation.

    Observations are taken with no artificial delay so that an asynchronous
    fault such as pod deletion is observed on the transition it caused. The
    repeated reset proves cleanup is idempotent.
    """
    require_scenario_scope(scenario)
    reset_scenario(scenario)
    baseline = observe_current_state()
    apply_fault(scenario)
    faulted = observe_current_state()
    reset_scenario(scenario)
    reset_scenario(scenario)
    return [baseline, faulted, observe_current_state(), observe_current_state()]


def verify_scenario(scenario: ScenarioDefinition, repeats: int) -> ScenarioVerdict:
    cycles = [
        build_cycle(scenario, cycle, run_cycle(scenario)) for cycle in range(1, repeats + 1)
    ]
    return summarize(scenario, cycles)


def verify_all(repeats: int = DEFAULT_REPEATS) -> RepeatabilityReport:
    scenarios = load_scenarios()
    ordered = [scenarios[scenario_id] for scenario_id in sorted(scenarios)]
    verdicts = [verify_scenario(scenario, repeats) for scenario in ordered]
    return build_report(ordered, verdicts, repeats, SCENARIO_SCHEMA_VERSION)


def report_payload(report: RepeatabilityReport) -> dict[str, Any]:
    return {
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


def scenario_by_id(scenario_id: str) -> ScenarioDefinition:
    scenarios = load_scenarios()
    try:
        return scenarios[scenario_id]
    except KeyError as error:
        raise ScenarioSafetyError(f"unknown scenario: {scenario_id}") from error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a known Faultweld scenario fixture, or verify fixture repeatability."
    )
    parser.add_argument("command", choices=("start", "reset", "health", "verify"))
    parser.add_argument("scenario_id", nargs="?")
    parser.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help="inject/observe/reset cycles per fixture for the verify command",
    )
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    if args.command == "verify":
        if args.scenario_id is not None:
            parser.error("verify covers every fixture; do not pass a scenario ID")
        try:
            report = verify_all(args.repeats)
        except ScenarioSafetyError as error:
            parser.error(str(error))
        print(json.dumps(report_payload(report), sort_keys=True, indent=2))
        return 0 if report.passed else 1

    if args.scenario_id is None:
        parser.error(f"{args.command} requires a scenario ID")
    try:
        scenario = scenario_by_id(args.scenario_id)
    except ScenarioSafetyError as error:
        parser.error(str(error))
    commands: dict[str, Callable[[ScenarioDefinition], Any]] = {
        "start": apply_fault,
        "reset": reset_scenario,
        "health": health,
    }
    result = commands[args.command](scenario)
    if args.command == "health":
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
