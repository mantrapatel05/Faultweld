from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from pydantic import JsonValue

from app.agent.llm.transcript import RecordingProvider, ReplayProvider
from app.agent.provider import (
    ModelEvidence,
    ModelProviderError,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    scripted_response,
)
from tests.unit.agent_support import diagnosis_output, snapshot_item


def request(*, text: str = "ordinary note") -> ModelRequest:
    item = snapshot_item()
    payload = dict(item.payload)
    payload["operator_note"] = text
    return ModelRequest(
        purpose=ModelPurpose.DIAGNOSE,
        incident_id="incident-1",
        run_id="run-1",
        evidence=(ModelEvidence(record=item.record, payload=payload),),
    )


class OneResponseProvider:
    def complete(self, _request: ModelRequest) -> ModelResponse:
        return scripted_response(
            diagnosis_output([request().evidence[0].record.evidence_id]),
            input_tokens=12,
            output_tokens=8,
        )


class TranscriptTests(TestCase):
    def test_record_then_replay_matches_response_and_redacts_evidence(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            recorded = RecordingProvider(
                OneResponseProvider(),
                Path(directory),
                provider_id="fake",
                model_id="fake-v1",
            )
            original = recorded.complete(request(text="token=canary-secret"))
            transcript_text = (Path(directory) / "call-0000.json").read_text(encoding="utf-8")
            self.assertNotIn("canary-secret", transcript_text)
            self.assertIn("[redacted]", transcript_text)

            replayed = ReplayProvider(Path(directory)).complete(request(text="token=canary-secret"))
            self.assertEqual(original.model_dump(mode="json"), replayed.model_dump(mode="json"))

    def test_replay_rejects_changed_request_and_corrupt_transcript(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            provider = RecordingProvider(
                OneResponseProvider(), root, provider_id="fake", model_id="fake-v1"
            )
            provider.complete(request())
            replay = ReplayProvider(root)
            with self.assertRaises(ModelProviderError) as mismatch:
                replay.complete(request(text="changed evidence"))
            self.assertEqual(mismatch.exception.code, "invalid_response")

            (root / "call-0000.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(ModelProviderError) as corrupt:
                ReplayProvider(root).complete(request())
            self.assertEqual(corrupt.exception.code, "invalid_response")

    def test_replay_binds_model_and_parameters(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            parameters: dict[str, JsonValue] = {
                "temperature": 0.0,
                "max_output_tokens": 512,
            }
            recorded = RecordingProvider(
                OneResponseProvider(),
                root,
                provider_id="fake",
                model_id="fake-v1",
                parameters=parameters,
            )
            recorded.complete(request())
            with self.assertRaises(ModelProviderError):
                ReplayProvider(
                    root,
                    model_id="fake-v1",
                    parameters={"temperature": 0.8, "max_output_tokens": 512},
                ).complete(request())

    def test_replay_rejects_changed_response_with_stale_digest(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            RecordingProvider(
                OneResponseProvider(), root, provider_id="fake", model_id="fake-v1"
            ).complete(request())
            transcript_path = root / "call-0000.json"
            data = json.loads(transcript_path.read_text(encoding="utf-8"))
            data["response"]["is_sufficient"] = False
            transcript_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ModelProviderError) as raised:
                ReplayProvider(root).complete(request())
            self.assertEqual(raised.exception.code, "invalid_response")
