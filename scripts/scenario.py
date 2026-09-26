from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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
        raise ScenarioSafetyError(f"command failed: {' '.join(command)}{f': {detail}' if detail else ''}")
    return result.stdout.strip() if capture else ""


def project_context() -> None:
    run(["python", str(CLUSTER), "reset"])


def apply_fault(scenario: ScenarioDefinition) -> None:
    project_context()
    fault = scenario.fault
    if fault.kind == "scale":
        run(["kubectl", "-n", NAMESPACE, "scale", f"deployment/{DEPLOYMENT}", f"--replicas={fault.replicas}"])
    elif fault.kind == "readiness":
        patch = json.dumps(
            [
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/readinessProbe/httpGet/path",
                    "value": fault.path,
                }
            ]
        )
        run(["kubectl", "-n", NAMESPACE, "patch", f"deployment/{DEPLOYMENT}", "--type=json", "-p", patch])
    elif fault.kind == "delete_pod":
        run(["kubectl", "-n", NAMESPACE, "delete", "pod", "-l", fault.selector])


def reset_scenario(_: ScenarioDefinition) -> None:
    run(["python", str(CLUSTER), "reset"])


def health(_: ScenarioDefinition) -> dict[str, Any]:
    payload = run(
        ["kubectl", "-n", NAMESPACE, "get", f"deployment/{DEPLOYMENT}", "-o", "json"],
        capture=True,
    )
    deployment: dict[str, Any] = json.loads(payload)
    status = deployment.get("status", {})
    return {
        "desired_replicas": deployment.get("spec", {}).get("replicas", 0),
        "available_replicas": status.get("availableReplicas", 0),
        "ready_replicas": status.get("readyReplicas", 0),
        "observed_generation": status.get("observedGeneration"),
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