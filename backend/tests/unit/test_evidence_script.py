from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.evidence.transport import AGENT_IDENTITY

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "evidence.py"
MANAGED_NAMESPACE = json.dumps(
    {"metadata": {"name": "faultweld-demo", "labels": {"faultweld.dev/managed": "true"}}}
)


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("faultweld_evidence_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evidence: Any = load_script()


def completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ScopeGuardTests(TestCase):
    def test_wrong_context_is_refused(self) -> None:
        with patch.object(evidence, "run", return_value="kind-other") as run:
            with self.assertRaisesRegex(evidence.EvidenceCheckError, "refusing"):
                evidence.require_scope()
        self.assertEqual(run.call_count, 1)

    def test_unlabelled_namespace_is_refused(self) -> None:
        with patch.object(
            evidence,
            "run",
            side_effect=["kind-faultweld", json.dumps({"metadata": {"labels": {}}})],
        ):
            with self.assertRaisesRegex(evidence.EvidenceCheckError, "unlabelled"):
                evidence.require_scope()

    def test_project_scope_is_accepted(self) -> None:
        with patch.object(evidence, "run", side_effect=["kind-faultweld", MANAGED_NAMESPACE]):
            evidence.require_scope()


class CanIHelperTests(TestCase):
    def test_command_is_pinned_and_impersonates_the_agent(self) -> None:
        with patch.object(evidence.subprocess, "run", return_value=completed(0, "yes\n")) as run:
            self.assertTrue(evidence.can_i("get", "deployments", "faultweld-demo"))
        self.assertEqual(
            run.call_args.args[0],
            [
                "kubectl",
                "--context",
                "kind-faultweld",
                "auth",
                "can-i",
                "get",
                "deployments",
                "-n",
                "faultweld-demo",
                "--as",
                AGENT_IDENTITY,
            ],
        )

    def test_no_is_false_and_other_failures_raise(self) -> None:
        with patch.object(evidence.subprocess, "run", return_value=completed(1, "no\n")):
            self.assertFalse(evidence.can_i("get", "secrets", "faultweld-demo"))
        with patch.object(evidence.subprocess, "run", return_value=completed(2, "", "boom")):
            with self.assertRaises(evidence.EvidenceCheckError):
                evidence.can_i("get", "secrets", "faultweld-demo")


class PayloadHelperTests(TestCase):
    def test_forbidden_keys_are_found_at_any_depth(self) -> None:
        payload = {"a": [{"b": {"env": 1}}], "annotations": {}, "ok": {"data": 2}}
        self.assertEqual(evidence.forbidden_keys(payload), {"env", "annotations", "data"})
        self.assertEqual(evidence.forbidden_keys({"metadata": {"name": "x"}}), set())

    def test_payload_value_walks_nested_mappings(self) -> None:
        payload = {"metadata": {"namespace": "faultweld-demo"}}
        self.assertEqual(
            evidence.payload_value(payload, "metadata", "namespace"), "faultweld-demo"
        )
        self.assertIsNone(evidence.payload_value(payload, "metadata", "missing"))
        self.assertIsNone(evidence.payload_value(payload, "spec", "containers"))

    def test_an_empty_structure_check_fails_rather_than_passes(self) -> None:
        self.assertEqual(
            evidence.check_structure([]), ["no payloads were collected to inspect"]
        )


class VerifyAggregationTests(TestCase):
    def run_verify(self, rbac_failures: list[str]) -> dict[str, Any]:
        with patch.object(evidence, "require_scope"), patch.object(
            evidence, "KubectlRawTransport", MagicMock()
        ), patch.object(evidence, "KubernetesEvidenceAdapter", MagicMock()):
            with patch.object(evidence, "check_deployment", return_value=([], [])), patch.object(
                evidence, "check_pods", return_value=([], [])
            ), patch.object(evidence, "check_service", return_value=([], [])), patch.object(
                evidence, "check_missing", return_value=[]
            ), patch.object(evidence, "check_rbac", return_value=rbac_failures), patch.object(
                evidence, "check_structure", return_value=[]
            ):
                report: dict[str, Any] = evidence.verify()
        return report

    def test_all_checks_passing_passes(self) -> None:
        report = self.run_verify([])
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 6)

    def test_one_failing_check_fails_the_report(self) -> None:
        report = self.run_verify(["agent identity can get secrets"])
        self.assertFalse(report["passed"])
        failed = [check for check in report["checks"] if not check["passed"]]
        self.assertEqual(len(failed), 1)
        self.assertIn("secrets", failed[0]["failures"][0])

    def test_a_read_error_becomes_a_failed_check_not_a_crash(self) -> None:
        def boom() -> list[str]:
            raise evidence.EvidenceReadError("forbidden", "denied")

        result = evidence.run_check("example", boom)
        self.assertFalse(result["passed"])
        self.assertIn("forbidden", result["failures"][0])


class ScriptSafetyTests(TestCase):
    def test_script_never_references_evaluator_labels(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        for marker in ("expected_outcome", "allowed_actions", "scenarios/expected", "labels.json"):
            self.assertNotIn(marker, source)

    def test_script_defaults_to_the_agent_identity(self) -> None:
        self.assertEqual(evidence.AGENT_IDENTITY, AGENT_IDENTITY)
        self.assertEqual(evidence.NAMESPACE, "faultweld-demo")
        self.assertEqual(evidence.CONTEXT, "kind-faultweld")
