from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from pydantic import ValidationError

from app.scenarios.models import ScenarioDefinition
from app.scenarios.registry import load_scenarios

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "scenarios" / "fixtures"
EXPECTED_LABELS = ROOT / "scenarios" / "expected" / "labels.json"


class ScenarioContractTests(TestCase):
    def test_all_initial_fixtures_are_versioned_and_distinct(self) -> None:
        scenarios = load_scenarios(FIXTURES)
        self.assertEqual(
            set(scenarios),
            {"deployment-unavailable", "healthy-replica-loss", "readiness-regression"},
        )
        self.assertTrue(all(item.schema_version == "0.2" for item in scenarios.values()))
        self.assertTrue(all(item.namespace == "faultweld-demo" for item in scenarios.values()))

    def test_unknown_fault_kind_is_rejected(self) -> None:
        payload = json.loads((FIXTURES / "healthy-replica-loss.json").read_text(encoding="utf-8"))
        payload["fault"] = {"kind": "delete_namespace", "selector": "*"}
        with self.assertRaises(ValidationError):
            ScenarioDefinition.model_validate(payload)

    def test_fixture_cannot_escape_project_namespace_or_deployment(self) -> None:
        payload = json.loads((FIXTURES / "healthy-replica-loss.json").read_text(encoding="utf-8"))
        payload["namespace"] = "kube-system"
        with self.assertRaises(ValidationError):
            ScenarioDefinition.model_validate(payload)

        payload["namespace"] = "faultweld-demo"
        payload["target_deployment"] = "other-workload"
        with self.assertRaises(ValidationError):
            ScenarioDefinition.model_validate(payload)

    def test_default_fixtures_dir_loads_initial_fixtures(self) -> None:
        scenarios = load_scenarios()
        self.assertEqual(
            set(scenarios),
            {"deployment-unavailable", "healthy-replica-loss", "readiness-regression"},
        )

    def test_loading_is_deterministic(self) -> None:
        first = load_scenarios(FIXTURES)
        second = load_scenarios(FIXTURES)
        self.assertEqual(list(first), sorted(first))
        self.assertEqual(list(first), list(second))
        self.assertEqual(
            [item.model_dump() for item in first.values()],
            [item.model_dump() for item in second.values()],
        )

    def test_malformed_fixtures_are_rejected(self) -> None:
        base = json.loads(
            (FIXTURES / "healthy-replica-loss.json").read_text(encoding="utf-8")
        )
        bad_payloads = [
            {**base, "schema_version": "0.3"},
            {**base, "scenario_id": "Healthy_Replica_Loss"},
            {**base, "version": "0"},
            {**base, "fault": {"kind": "scale"}},
            {**base, "fault": {"kind": "readiness"}},
            {**base, "fault": {"kind": "delete_pod"}},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    ScenarioDefinition.model_validate(payload)
        missing_title = {key: value for key, value in base.items() if key != "title"}
        with self.assertRaises(ValidationError):
            ScenarioDefinition.model_validate(missing_title)
        with self.assertRaises(ValidationError):
            ScenarioDefinition.model_validate({**base, "unexpected": "field"})

    def test_malformed_fixture_file_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "broken.json").write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_scenarios(directory)

    def test_evaluator_labels_are_not_loaded_by_registry(self) -> None:
        scenarios = load_scenarios(FIXTURES)
        for scenario in scenarios.values():
            self.assertNotIn("expected_outcome", scenario.model_dump())
            self.assertNotIn("allowed_actions", scenario.model_dump())
        labels = json.loads(EXPECTED_LABELS.read_text(encoding="utf-8"))
        self.assertEqual(set(labels), set(scenarios))
        for fixture_path in FIXTURES.glob("*.json"):
            payload = json.loads(fixture_path.read_text(encoding="utf-8"))
            self.assertNotIn("expected_outcome", payload)
            self.assertNotIn("allowed_actions", payload)

    def test_duplicate_scenario_ids_are_rejected(self) -> None:
        first = json.loads((FIXTURES / "healthy-replica-loss.json").read_text(encoding="utf-8"))
        second = {**first, "fault": {"kind": "scale", "replicas": 0}}
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "first.json").write_text(json.dumps(first), encoding="utf-8")
            (directory / "second.json").write_text(json.dumps(second), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate scenario ID"):
                load_scenarios(directory)
