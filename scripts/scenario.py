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

from app.scenarios.health import classify_health, observe_deployment
from app.scenarios.models import ScenarioDefinition
from app.scenarios.registry import load_scenarios

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = ROOT / "scripts" / "cluster.py"
NAMESPACE = "faultweld-demo"
DEPLOYMENT = "faultweld-sample"


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
        if fault.path is None:
            raise ScenarioSafetyError("readiness fault is missing path")
        patch = json.dumps(
            [
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/readinessProbe/httpGet/path",
                    "value": fault.path,
                }
            ]
        )
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


def scenario_by_id(scenario_id: str) -> ScenarioDefinition:
    scenarios = load_scenarios()
    try:
        return scenarios[scenario_id]
    except KeyError as error:
        raise ScenarioSafetyError(f"unknown scenario: {scenario_id}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a known Faultweld scenario fixture.")
    parser.add_argument("command", choices=("start", "reset", "health"))
    parser.add_argument("scenario_id")
    args = parser.parse_args()
    scenario = scenario_by_id(args.scenario_id)
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
