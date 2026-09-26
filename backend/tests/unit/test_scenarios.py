from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from pydantic import ValidationError

from app.scenarios.models import ScenarioDefinition
from app.scenarios.registry import load_scenarios

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "scenarios" / "fixtures"


class ScenarioContractTests(TestCase):
    def test_all_initial_fixtures_are_versioned_and_distinct(self) -> None:
        scenarios = load_scenarios(FIXTURES)
        self.assertEqual(set(scenarios), {"deployment-unavailable", "healthy-replica-loss", "readiness-regression"})
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

    def test_evaluator_labels_are_not_loaded_by_registry(self) -> None:
        scenarios = load_scenarios(FIXTURES)
        for scenario in scenarios.values():
            self.assertNotIn("expected_outcome", scenario.model_dump())

    @patch("app.scenarios.registry.FIXTURES_DIR")
    def test_duplicate_scenario_ids_are_rejected(self, fixtures_dir: object) -> None:
        del fixtures_dir
        first = json.loads((FIXTURES / "healthy-replica-loss.json").read_text(encoding="utf-8"))
        second = {**first, "fault": {"kind": "scale", "replicas": 0}}
        with self.assertRaises(ValueError):
            definitions = [ScenarioDefinition.model_validate(first), ScenarioDefinition.model_validate(second)]
            self.assertEqual(len({item.scenario_id for item in definitions}), len(definitions))