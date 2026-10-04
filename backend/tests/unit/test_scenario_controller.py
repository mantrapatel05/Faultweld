from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from app.scenarios.health import HealthObservation, classify_health, observe_deployment
from app.scenarios.models import ScenarioDefinition, ScenarioFault
from app.scenarios.registry import load_scenarios

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "scenarios" / "fixtures"
CONTROLLER_PATH = ROOT / "scripts" / "scenario.py"


def load_controller() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "faultweld_scenario_controller", CONTROLLER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller: Any = load_controller()


def fixture(scenario_id: str) -> ScenarioDefinition:
    return load_scenarios(FIXTURES)[scenario_id]


def out_of_scope(base: ScenarioDefinition) -> ScenarioDefinition:
    return base.model_copy(update={"namespace": "kube-system", "target_deployment": "other"})


class ScenarioControllerTests(TestCase):
    def test_trigger_scale_fault_resets_baseline_then_scales(self) -> None:
        scenario = fixture("healthy-replica-loss")
        with patch.object(controller, "run") as run:
            controller.apply_fault(scenario)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["python", str(controller.CLUSTER), "reset"],
                [
                    "kubectl",
                    "-n",
                    "faultweld-demo",
                    "scale",
                    "deployment/faultweld-sample",
                    "--replicas=0",
                ],
            ],
        )

    def test_trigger_readiness_fault_patches_probe_port(self) -> None:
        scenario = fixture("readiness-regression")
        with patch.object(controller, "run") as run:
            controller.apply_fault(scenario)
        patch_call = run.call_args_list[-1].args[0]
        self.assertEqual(
            patch_call[:4], ["kubectl", "-n", "faultweld-demo", "patch"]
        )
        self.assertEqual(patch_call[4], "deployment/faultweld-sample")
        payload = json.loads(patch_call[-1])
        by_path = {op["path"]: op["value"] for op in payload}
        self.assertEqual(
            by_path["/spec/template/spec/containers/0/readinessProbe/httpGet/port"], 9
        )

    def test_trigger_readiness_fault_patches_probe_path_when_set(self) -> None:
        scenario = fixture("readiness-regression")
        fault = ScenarioFault.model_validate({"kind": "readiness", "path": "/unhealthy"})
        patched = scenario.model_copy(update={"fault": fault})
        with patch.object(controller, "run") as run:
            controller.apply_fault(patched)
        payload = json.loads(run.call_args_list[-1].args[0][-1])
        by_path = {op["path"]: op["value"] for op in payload}
        self.assertEqual(
            by_path["/spec/template/spec/containers/0/readinessProbe/httpGet/path"],
            "/unhealthy",
        )

    def test_trigger_delete_pod_fault_uses_fixture_selector(self) -> None:
        scenario = fixture("deployment-unavailable")
        with patch.object(controller, "run") as run:
            controller.apply_fault(scenario)
        self.assertEqual(
            run.call_args_list[-1].args[0],
            [
                "kubectl",
                "-n",
                "faultweld-demo",
                "delete",
                "pod",
                "-l",
                "app.kubernetes.io/name=faultweld-sample",
                "--wait=false",
            ],
        )

    def test_reset_path_for_each_fixture_restores_baseline(self) -> None:
        for scenario_id in (
            "deployment-unavailable",
            "healthy-replica-loss",
            "readiness-regression",
        ):
            with self.subTest(scenario=scenario_id), patch.object(controller, "run") as run:
                controller.reset_scenario(fixture(scenario_id))
            self.assertEqual(
                [call.args[0] for call in run.call_args_list],
                [["python", str(controller.CLUSTER), "reset"]],
            )

    def test_reset_is_idempotent(self) -> None:
        scenario = fixture("healthy-replica-loss")
        with patch.object(controller, "run") as run:
            controller.reset_scenario(scenario)
            first = [call.args[0] for call in run.call_args_list]
            controller.reset_scenario(scenario)
            second = [call.args[0] for call in run.call_args_list[len(first) :]]
        self.assertEqual(first, second)
        self.assertEqual(first, [["python", str(controller.CLUSTER), "reset"]])

    def test_unknown_scenario_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(controller.ScenarioSafetyError, "unknown scenario"):
            controller.scenario_by_id("does-not-exist")

    def test_out_of_scope_scenario_is_rejected_without_kubectl_calls(self) -> None:
        scenario = out_of_scope(fixture("healthy-replica-loss"))
        with patch.object(controller, "run") as run:
            for command in (
                controller.apply_fault,
                controller.reset_scenario,
                controller.health,
            ):
                with self.subTest(command=command.__name__):
                    with self.assertRaisesRegex(controller.ScenarioSafetyError, "refusing"):
                        command(scenario)
            run.assert_not_called()

    def test_fault_missing_argument_is_rejected(self) -> None:
        fault = ScenarioFault.model_construct(
            kind="scale", replicas=None, path=None, port=None, selector=None
        )
        scenario = fixture("healthy-replica-loss").model_copy(update={"fault": fault})
        with patch.object(controller, "run") as run:
            with self.assertRaisesRegex(controller.ScenarioSafetyError, "missing replicas"):
                controller.apply_fault(scenario)
            run.assert_not_called()

    def test_readiness_fault_missing_path_and_port_is_rejected(self) -> None:
        fault = ScenarioFault.model_construct(
            kind="readiness", replicas=None, path=None, port=None, selector=None
        )
        scenario = fixture("readiness-regression").model_copy(update={"fault": fault})
        with patch.object(controller, "run") as run:
            with self.assertRaisesRegex(
                controller.ScenarioSafetyError, "missing path or port"
            ):
                controller.apply_fault(scenario)
            run.assert_not_called()

    def test_health_probe_distinguishes_healthy_and_faulted(self) -> None:
        for scenario_id in (
            "deployment-unavailable",
            "healthy-replica-loss",
            "readiness-regression",
        ):
            scenario = fixture(scenario_id)
            healthy = HealthObservation(
                desired_replicas=1, available_replicas=1, ready_replicas=1
            )
            faulted = HealthObservation(
                desired_replicas=0, available_replicas=0, ready_replicas=0
            )
            self.assertEqual(classify_health(scenario, healthy), "healthy")
            self.assertEqual(classify_health(scenario, faulted), "faulted")

    def test_health_command_reports_state_from_cluster_payload(self) -> None:
        scenario = fixture("healthy-replica-loss")
        payload = {
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "readyReplicas": 1, "observedGeneration": 3},
        }
        with patch.object(controller, "run", return_value=json.dumps(payload)) as run:
            report = controller.health(scenario)
        run.assert_called_once()
        self.assertEqual(
            report,
            {
                "scenario_id": "healthy-replica-loss",
                "desired_replicas": 1,
                "available_replicas": 1,
                "ready_replicas": 1,
                "observed_generation": 3,
                "state": "healthy",
            },
        )

    def test_health_command_marks_missing_replicas_as_faulted(self) -> None:
        scenario = fixture("healthy-replica-loss")
        payload = {"spec": {"replicas": 0}, "status": {}}
        with patch.object(controller, "run", return_value=json.dumps(payload)):
            report = controller.health(scenario)
        self.assertEqual(report["state"], "faulted")
        self.assertEqual(report["ready_replicas"], 0)

    def test_observe_deployment_rejects_malformed_payload(self) -> None:
        with self.assertRaises(ValueError):
            observe_deployment({"spec": [], "status": {}})
        with self.assertRaises(ValueError):
            observe_deployment({"spec": {}, "status": None})

    def test_controller_and_probe_outputs_exclude_evaluator_labels(self) -> None:
        source = CONTROLLER_PATH.read_text(encoding="utf-8")
        for marker in ("expected_outcome", "allowed_actions", "scenarios/expected"):
            self.assertNotIn(marker, source)
        scenario = fixture("readiness-regression")
        observation = HealthObservation(
            desired_replicas=1, available_replicas=1, ready_replicas=1
        )
        self.assertNotIn("expected_outcome", observation.model_dump())
        with patch.object(
            controller,
            "run",
            return_value=json.dumps({"spec": {"replicas": 1}, "status": {}}),
        ):
            report = controller.health(scenario)
        for key in ("expected_outcome", "allowed_actions"):
            self.assertNotIn(key, report)
