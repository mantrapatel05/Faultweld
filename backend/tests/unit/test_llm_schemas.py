from __future__ import annotations

from unittest import TestCase

from pydantic import JsonValue

from app.agent.llm.schemas import normalize_model_output, schema_for


class ProviderSchemaTests(TestCase):
    def test_diagnosis_schema_projects_nested_properties(self) -> None:
        schema = schema_for("diagnose")
        properties = schema["properties"]
        assert isinstance(properties, dict)
        self.assertIn("hypotheses", properties)
        hypotheses = properties["hypotheses"]
        assert isinstance(hypotheses, dict)
        items = hypotheses["items"]
        assert isinstance(items, dict)
        hypothesis_properties = items["properties"]
        assert isinstance(hypothesis_properties, dict)
        self.assertEqual(
            set(hypothesis_properties),
            {
                "statement",
                "confidence",
                "supporting_evidence_ids",
                "contradicting_evidence_ids",
            },
        )

    def test_proposal_schema_is_closed_and_carries_all_top_level_fields(self) -> None:
        schema = schema_for("propose")
        properties = schema["properties"]
        assert isinstance(properties, dict)
        self.assertEqual(
            set(properties), {"action_type", "parameters", "rationale", "evidence_ids"}
        )
        self.assertEqual(schema["additionalProperties"], False)

    def test_diagnosis_integer_confidence_normalizes_without_filling_fields(self) -> None:
        output: dict[str, JsonValue] = {
            "hypotheses": [
                {
                    "statement": "replica is absent",
                    "confidence": 1,
                    "supporting_evidence_ids": ["ev-1"],
                    "contradicting_evidence_ids": [],
                }
            ],
            "missing_evidence": [],
            "confidence": 1,
            "is_sufficient": True,
        }
        normalized = normalize_model_output("diagnose", output)
        self.assertEqual(normalized["confidence"], 1.0)
        hypotheses = normalized["hypotheses"]
        assert isinstance(hypotheses, list)
        hypothesis = hypotheses[0]
        assert isinstance(hypothesis, dict)
        self.assertEqual(hypothesis["confidence"], 1.0)
