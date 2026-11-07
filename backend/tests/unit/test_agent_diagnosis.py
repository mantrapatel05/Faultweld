from __future__ import annotations

from typing import cast
from unittest import TestCase

from pydantic import JsonValue

from app.agent.diagnosis import build_diagnosis, clamp_confidence
from app.agent.errors import AgentOutputError
from app.agent.ids import SequentialIdFactory
from app.agent.reasons import INVALID_MODEL_OUTPUT, UNKNOWN_EVIDENCE_ID
from app.domain.schemas import Diagnosis

from .agent_support import diagnosis_output, snapshot_item

EVIDENCE_ID = snapshot_item().record.evidence_id
RUN_ID = "run-1"


def hypotheses_of(output: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    hypotheses = output["hypotheses"]
    assert isinstance(hypotheses, list)
    return cast(list[dict[str, JsonValue]], hypotheses)


def build(output: dict[str, JsonValue]) -> Diagnosis:
    return build_diagnosis(
        output,
        run_id=RUN_ID,
        evidence_ids=(EVIDENCE_ID,),
        ids=SequentialIdFactory(),
    )


class ValidDiagnosisTests(TestCase):
    def test_a_valid_output_becomes_a_typed_diagnosis(self) -> None:
        diagnosis = build(diagnosis_output([EVIDENCE_ID]))
        self.assertEqual(diagnosis.schema_version, "0.1")
        self.assertEqual(diagnosis.diagnosis_id, "diagnosis-1")
        self.assertEqual(diagnosis.run_id, RUN_ID)
        self.assertEqual(diagnosis.confidence, 0.9)
        self.assertTrue(diagnosis.is_sufficient)
        self.assertEqual(len(diagnosis.hypotheses), 1)
        hypothesis = diagnosis.hypotheses[0]
        self.assertEqual(hypothesis.hypothesis_id, "hypothesis-1")
        self.assertEqual(hypothesis.supporting_evidence_ids, (EVIDENCE_ID,))
        self.assertEqual(hypothesis.contradicting_evidence_ids, ())

    def test_multiple_hypotheses_get_ordered_server_ids(self) -> None:
        output = diagnosis_output([EVIDENCE_ID])
        hypotheses_of(output).append(
            {
                "statement": "a probe change also contributed",
                "confidence": 0.4,
                "supporting_evidence_ids": [EVIDENCE_ID],
                "contradicting_evidence_ids": [EVIDENCE_ID],
            }
        )
        diagnosis = build(output)
        self.assertEqual(
            [hypothesis.hypothesis_id for hypothesis in diagnosis.hypotheses],
            ["hypothesis-1", "hypothesis-2"],
        )
        self.assertEqual(
            diagnosis.hypotheses[1].contradicting_evidence_ids, (EVIDENCE_ID,)
        )

    def test_missing_evidence_ids_are_preserved(self) -> None:
        output = diagnosis_output([EVIDENCE_ID])
        output["missing_evidence"] = ["missing-logs"]
        diagnosis = build(output)
        self.assertEqual(diagnosis.missing_evidence, ("missing-logs",))

    def test_insufficient_diagnosis_is_valid_and_marked(self) -> None:
        diagnosis = build(diagnosis_output([EVIDENCE_ID], sufficient=False))
        self.assertFalse(diagnosis.is_sufficient)


class ConfidenceTests(TestCase):
    def test_confidence_is_clamped_into_the_unit_interval(self) -> None:
        self.assertEqual(clamp_confidence(1.5), 1.0)
        self.assertEqual(clamp_confidence(-0.5), 0.0)
        self.assertEqual(clamp_confidence(0.25), 0.25)

    def test_both_levels_of_confidence_are_clamped(self) -> None:
        output = diagnosis_output([EVIDENCE_ID], confidence=1.5)
        hypotheses_of(output)[0]["confidence"] = -2.0
        diagnosis = build(output)
        self.assertEqual(diagnosis.confidence, 1.0)
        self.assertEqual(diagnosis.hypotheses[0].confidence, 0.0)


class InvalidDiagnosisTests(TestCase):
    def assert_rejected(self, output: dict[str, JsonValue], reason: str) -> None:
        with self.assertRaises(AgentOutputError) as caught:
            build(output)
        self.assertEqual(caught.exception.reason, reason)

    def test_empty_hypotheses_are_rejected(self) -> None:
        output = diagnosis_output([EVIDENCE_ID])
        output["hypotheses"] = []
        self.assert_rejected(output, INVALID_MODEL_OUTPUT)

    def test_unknown_top_level_fields_are_rejected(self) -> None:
        output = diagnosis_output([EVIDENCE_ID])
        output["diagnosis_id"] = "model-chosen"
        self.assert_rejected(output, INVALID_MODEL_OUTPUT)

    def test_unknown_hypothesis_fields_are_rejected(self) -> None:
        output = diagnosis_output([EVIDENCE_ID])
        hypotheses_of(output)[0]["authority"] = "approved"
        self.assert_rejected(output, INVALID_MODEL_OUTPUT)

    def test_wrong_scalar_types_are_rejected(self) -> None:
        cases: tuple[tuple[str, JsonValue], ...] = (
            ("confidence", "high"),
            ("is_sufficient", "yes"),
            ("hypotheses", "not-a-list"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                output = diagnosis_output([EVIDENCE_ID])
                output[field] = value
                self.assert_rejected(output, INVALID_MODEL_OUTPUT)

    def test_a_wrongly_typed_statement_is_rejected(self) -> None:
        output = diagnosis_output([EVIDENCE_ID])
        hypotheses_of(output)[0]["statement"] = 7
        self.assert_rejected(output, INVALID_MODEL_OUTPUT)

    def test_an_unknown_supporting_evidence_id_is_rejected(self) -> None:
        self.assert_rejected(diagnosis_output(["ev-made-up"]), UNKNOWN_EVIDENCE_ID)

    def test_an_unknown_contradicting_evidence_id_is_rejected(self) -> None:
        output = diagnosis_output([EVIDENCE_ID])
        hypotheses_of(output)[0]["contradicting_evidence_ids"] = ["ev-made-up"]
        self.assert_rejected(output, UNKNOWN_EVIDENCE_ID)

    def test_an_oversized_statement_is_rejected(self) -> None:
        self.assert_rejected(
            diagnosis_output([EVIDENCE_ID], statement="x" * 501),
            INVALID_MODEL_OUTPUT,
        )
