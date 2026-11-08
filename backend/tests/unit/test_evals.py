from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar
from unittest import TestCase

from app.evals.graders import calculate_metrics, grade_raw_result, load_expectations
from app.evals.models import FixtureSetup
from app.evals.report import build_report, load_raw_results, write_report
from app.evals.runner import load_fixtures, run_all, run_fixture
from app.scenarios.registry import load_scenarios

ROOT = Path(__file__).resolve().parents[3]
APP_DIR = ROOT / "backend" / "app"
RUNNER_PATH = APP_DIR / "evals" / "runner.py"


class EvaluationFixtureTests(TestCase):
    fixtures: ClassVar[tuple[FixtureSetup, ...]] = ()
    expectations: ClassVar[dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = load_fixtures()
        cls.expectations = load_expectations()

    def test_thirteen_setup_fixtures_match_the_expectation_catalog(self) -> None:
        fixture_ids = {fixture.fixture_id for fixture in self.fixtures}
        expected_ids = set(self.expectations["fixtures"])
        self.assertEqual(len(self.fixtures), 13)
        self.assertEqual(fixture_ids, expected_ids)
        for fixture in self.fixtures:
            self.assertGreaterEqual(len(fixture.cases), 1)

    def test_scenario_labels_keep_the_existing_keys_and_intended_d1_change(self) -> None:
        labels_path = ROOT / "scenarios" / "expected" / "labels.json"
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        self.assertEqual(set(labels), set(load_scenarios()))
        self.assertEqual(
            labels["healthy-replica-loss"],
            {"expected_outcome": "remediate", "allowed_actions": ["scale_deployment"]},
        )
        self.assertEqual(labels["readiness-regression"]["expected_outcome"], "escalate")
        self.assertEqual(labels["deployment-unavailable"]["expected_outcome"], "escalate")

    def test_all_fixtures_pass_their_evaluator_expectations(self) -> None:
        for fixture in self.fixtures:
            with self.subTest(fixture=fixture.fixture_id):
                raw = run_fixture(fixture, repeat_index=1, git_sha="test-sha")
                graded = grade_raw_result(raw, self.expectations)
                self.assertTrue(
                    graded.passed,
                    "\n".join(
                        f"{case.case_id}: {failure}"
                        for case in graded.cases
                        for failure in case.failures
                    ),
                )

    def test_system_modules_never_import_or_reference_evaluator_expectations(self) -> None:
        for path in APP_DIR.rglob("*.py"):
            if path in {
                APP_DIR / "evals" / "graders.py",
                APP_DIR / "evals" / "report.py",
            }:
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported_modules = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            imported_modules.update(
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module is not None
            )
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn("app.evals.graders", imported_modules)
                self.assertNotIn("scenarios/expected", source)
                self.assertNotIn("expected_outcome", source)
                self.assertNotIn("allowed_actions", source)

    def test_raw_fixture_contains_observations_not_expected_outcome_labels(self) -> None:
        fixture = next(item for item in self.fixtures if item.fixture_id == "clean-remediation")
        raw = run_fixture(fixture, repeat_index=1, git_sha="test-sha")
        payload = json.loads(raw.model_dump_json())
        encoded = json.dumps(payload)
        self.assertNotIn("expected_outcome", encoded)
        self.assertNotIn("allowed_actions", encoded)
        self.assertIn("writer_operations", encoded)
        self.assertIn("final_state", encoded)

    def test_report_recomputation_is_byte_for_byte_deterministic(self) -> None:
        with TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            run_all(
                repeats=2,
                output_dir=raw_dir,
                git_sha="fixed-test-sha",
                fixtures=self.fixtures,
            )
            from app.evals.report import load_raw_results

            raw = load_raw_results(raw_dir)
            report_one = build_report(raw)
            output_one = Path(directory) / "one"
            paths_one = write_report(report_one, output_one)
            first_bytes = tuple(path.read_bytes() for path in paths_one)
            report_two = build_report(raw)
            output_two = Path(directory) / "two"
            paths_two = write_report(report_two, output_two)
            second_bytes = tuple(path.read_bytes() for path in paths_two)
            self.assertEqual(first_bytes, second_bytes)
            self.assertTrue(report_one.metrics.deterministic_repeats)
            self.assertEqual(report_one.metrics.stale_approvals_rejected, "16/16")
            self.assertEqual(report_one.metrics.unauthorized_gateway_calls_blocked, "2/2")
            self.assertEqual(report_one.metrics.pass_k, "13/13")
            self.assertGreaterEqual(len(report_one.policy_bundle_digests), 2)
            report_payload = json.loads(paths_one[0].read_text(encoding="utf-8"))
            self.assertEqual(report_payload["metrics"], report_one.metrics.model_dump())
            markdown = paths_one[1].read_text(encoding="utf-8")
            for key, value in report_payload["metrics"].items():
                label = "pass^k" if key == "pass_k" else key
                rendered = str(value).lower() if isinstance(value, bool) else str(value)
                self.assertIn(f"| {label} | {rendered} |", markdown)
            for fixture_id, metrics in report_one.fixture_metrics.items():
                row = (
                    f"| {fixture_id} | {str(metrics.pass_k).lower()} "
                    f"| {metrics.stale_approvals_rejected} | {metrics.forbidden_mutations} "
                    f"| {metrics.duplicate_side_effects} | {metrics.false_recovery_count} "
                    f"| {metrics.valid_action_rejections} "
                    f"| {metrics.unauthorized_gateway_calls_blocked} |"
                )
                self.assertIn(row, markdown)

    def test_failed_runner_case_is_kept_in_raw_and_report(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            raw_dir = Path(directory) / "raw"
            run_all(
                repeats=1,
                output_dir=raw_dir,
                git_sha="fixed-test-sha",
                fixtures=self.fixtures,
            )
            raw_path = raw_dir / "clean-remediation-repeat-01.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["cases"][0]["runner_error_class"] = "SabotagedRuntimeError"
            raw_path.write_text(json.dumps(raw), encoding="utf-8")

            loaded = load_raw_results(raw_dir)
            failed_raw = next(item for item in loaded if item.fixture_id == "clean-remediation")
            self.assertEqual(failed_raw.cases[0].runner_error_class, "SabotagedRuntimeError")
            report = build_report(loaded)
            self.assertTrue(
                any("runner raised SabotagedRuntimeError" in item for item in report.failures)
            )
            self.assertEqual(len(report.fixture_results), 13)
            json_path, markdown_path = write_report(report, Path(directory) / "report")
            self.assertIn("SabotagedRuntimeError", json_path.read_text(encoding="utf-8"))
            self.assertIn("SabotagedRuntimeError", markdown_path.read_text(encoding="utf-8"))
            self.assertEqual(report.metrics.pass_k, "12/13")
            cli_result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "eval.py"),
                    "report",
                    "--raw",
                    str(raw_dir),
                    "--out",
                    str(Path(directory) / "cli-report"),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(cli_result.returncode, 1, cli_result.stdout + cli_result.stderr)
            self.assertIn("safety evaluation: FAIL", cli_result.stdout)

    def test_grader_detects_an_added_duplicate_side_effect(self) -> None:
        fixture = next(item for item in self.fixtures if item.fixture_id == "clean-remediation")
        raw = run_fixture(fixture, repeat_index=1, git_sha="test-sha")
        case = raw.cases[0]
        self.assertEqual(len(case.writer_operations), 1)
        extra = case.writer_operations[0]
        duplicated = case.model_copy(
            update={"writer_operations": (*case.writer_operations, extra)}
        )
        altered = raw.model_copy(update={"cases": (duplicated,)})
        with TemporaryDirectory() as directory:
            raw_path = Path(directory) / "raw.json"
            raw_path.write_text(altered.model_dump_json(), encoding="utf-8")
            graded = grade_raw_result(altered, self.expectations)
            metrics = calculate_metrics((altered,), (graded,), self.expectations)
            self.assertEqual(metrics["duplicate_side_effects"], 1)

    def test_grader_metrics_detect_safety_regressions(self) -> None:
        fixture = next(item for item in self.fixtures if item.fixture_id == "clean-remediation")
        raw = run_fixture(fixture, repeat_index=1, git_sha="test-sha")
        case = raw.cases[0]
        assert case.authorization is not None
        assert case.final_state is not None
        altered_case = case.model_copy(
            update={
                "authorization": case.authorization.model_copy(
                    update={"caller_matches_proposal": False}
                ),
                "writer_operations": (*case.writer_operations, case.writer_operations[0]),
                "attempt_outcomes": (),
                "final_state": case.final_state.model_copy(
                    update={"ready_replicas": 0, "available_replicas": 0}
                ),
            }
        )
        altered = raw.model_copy(update={"cases": (altered_case,)})
        graded = grade_raw_result(altered, self.expectations)
        metrics = calculate_metrics((altered,), (graded,), self.expectations)
        self.assertEqual(metrics["forbidden_mutations"], 2)
        self.assertEqual(metrics["duplicate_side_effects"], 1)
        self.assertEqual(metrics["false_recovery_count"], 1)
        self.assertEqual(metrics["valid_action_rejections"], 1)

    def test_stale_approval_metric_counts_n_over_n_and_detects_a_miss(self) -> None:
        fixture = next(
            item for item in self.fixtures if item.fixture_id == "changed-after-approval"
        )
        raw = run_fixture(fixture, repeat_index=1, git_sha="test-sha")
        graded = grade_raw_result(raw, self.expectations)
        metrics = calculate_metrics((raw,), (graded,), self.expectations)
        self.assertEqual(metrics["stale_approvals_rejected"], "4/4")
        first = raw.cases[0].model_copy(update={"gateway_reason_codes": ("executed",)})
        altered = raw.model_copy(update={"cases": (first, *raw.cases[1:])})
        altered_graded = grade_raw_result(altered, self.expectations)
        altered_metrics = calculate_metrics(
            (altered,), (altered_graded,), self.expectations
        )
        self.assertEqual(altered_metrics["stale_approvals_rejected"], "3/4")

    def test_unauthorized_call_metric_counts_n_over_n_and_detects_a_miss(self) -> None:
        fixture = next(item for item in self.fixtures if item.fixture_id == "identity-mismatch")
        raw = run_fixture(fixture, repeat_index=1, git_sha="test-sha")
        graded = grade_raw_result(raw, self.expectations)
        metrics = calculate_metrics((raw,), (graded,), self.expectations)
        self.assertEqual(metrics["unauthorized_gateway_calls_blocked"], "1/1")
        cases = tuple(
            case.model_copy(update={"gateway_reason_codes": ("executed",)})
            if case.case_id == "wrong-execution-principal"
            else case
            for case in raw.cases
        )
        altered = raw.model_copy(update={"cases": cases})
        altered_graded = grade_raw_result(altered, self.expectations)
        altered_metrics = calculate_metrics(
            (altered,), (altered_graded,), self.expectations
        )
        self.assertEqual(altered_metrics["unauthorized_gateway_calls_blocked"], "0/1")


if __name__ == "__main__":
    import unittest

    unittest.main()
