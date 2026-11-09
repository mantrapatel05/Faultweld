"""Manual model-provider workflow test using the synthetic healthy-replica-loss fixture."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from lite import EXIT_ESCALATED, EXIT_OK, _load_local_api_key  # noqa: E402
from lite import main as lite_main  # noqa: E402

from app.agent.llm.client import HttpLlmClient  # noqa: E402
from app.agent.llm.config import LlmConfig  # noqa: E402
from app.agent.llm.errors import LlmClientError  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a manual real-model provider smoke test.")
    parser.add_argument("--backend", choices=("openrouter", "groq"), default="groq")
    parser.add_argument("--model")
    parser.add_argument("--transcript-dir", type=Path, default=ROOT / "transcripts" / "smoke")
    parser.add_argument(
        "--connectivity-only",
        action="store_true",
        help="make one small authentication and model-response check, without running an incident",
    )
    parser.add_argument(
        "--exercise-approval",
        action="store_true",
        help="approve and execute the valid proposal inside the isolated simulator",
    )
    args = parser.parse_args(argv)

    env_name = "GROQ_API_KEY" if args.backend == "groq" else "OPENROUTER_API_KEY"
    default_model = (
        "openai/gpt-oss-120b" if args.backend == "groq" else "recursive/flint-v1.0:free"
    )
    model_id = args.model or default_model
    if not _load_local_api_key(env_name):
        print(f"not run: {env_name} is not set")
        return EXIT_OK

    if args.connectivity_only:
        base_url = (
            "https://api.groq.com/openai/v1"
            if args.backend == "groq"
            else "https://openrouter.ai/api/v1"
        )
        host = "api.groq.com" if args.backend == "groq" else "openrouter.ai"
        config = LlmConfig(
            provider_name=args.backend,
            model_id=model_id,
            max_output_tokens=128,
            max_retries=0,
            base_url=base_url,
            allowed_hosts=(host,),
            input_price_per_million_usd=(
                Decimal("0.15") if args.backend == "groq" else Decimal("0")
            ),
            output_price_per_million_usd=(
                Decimal("0.60") if args.backend == "groq" else Decimal("0")
            ),
        )
        client = HttpLlmClient(config, api_key_env=env_name)
        try:
            response = client.complete(
                prompt="Reply with the single word OK.",
                schema=None,
                model=model_id,
                max_output_tokens=config.max_output_tokens,
                temperature=0.0,
                timeout_seconds=config.timeout_seconds,
            )
            if not response.text.strip():
                print("connectivity: failed (invalid_response)")
                return EXIT_ESCALATED
            print(f"connectivity: passed (HTTP {response.status_code}; {model_id})")
            return EXIT_OK
        except LlmClientError as error:
            suffix = f", HTTP {error.status_code}" if error.status_code is not None else ""
            print(f"connectivity: failed ({error.code}{suffix})")
            return EXIT_ESCALATED

    run_transcript_dir = args.transcript_dir / f"run-{uuid4().hex}"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        db_path = str(Path(temporary) / "smoke.sqlite3")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = lite_main(
                [
                    "run",
                    "healthy-replica-loss",
                    "--db",
                    db_path,
                    "--provider",
                    "llm",
                    "--backend",
                    args.backend,
                    "--model",
                    model_id,
                    "--transcript-dir",
                    str(run_transcript_dir),
                ]
            )
        first_output = output.getvalue()
        print(first_output, end="")
        if result == EXIT_OK and args.exercise_approval:
            proposal_match = re.search(r"^proposal: ([^\s]+)$", first_output, re.MULTILINE)
            incident_match = re.search(r"^incident: ([^\s]+)$", first_output, re.MULTILINE)
            if proposal_match is None or incident_match is None:
                print("full workflow: failed (proposal or incident id missing)")
                return EXIT_ESCALATED
            proposal_id = proposal_match.group(1)
            incident_id = incident_match.group(1)
            steps = (
                ["approve", proposal_id, "--db", db_path],
                ["execute", proposal_id, "--db", db_path],
                ["verify", incident_id, "--db", db_path],
            )
            for step in steps:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    step_result = lite_main(step)
                print(output.getvalue(), end="")
                if step_result != EXIT_OK:
                    print("full workflow: failed at " + step[0])
                    return EXIT_ESCALATED
            print("full workflow: approved, executed once, and independently verified")
            return EXIT_OK
    if result == EXIT_OK:
        print("smoke: typed proposal reached deterministic policy; no action executed")
        return EXIT_OK
    transcript_paths = sorted(run_transcript_dir.glob("call-*.json"))
    if transcript_paths:
        try:
            last_transcript = json.loads(
                transcript_paths[-1].read_text(encoding="utf-8")
            )
            status = last_transcript.get("http_status")
            if isinstance(status, int):
                print(f"provider HTTP status: {status}")
            failure_stage = last_transcript.get("failure_stage")
            if isinstance(failure_stage, str):
                print(f"provider failure stage: {failure_stage}")
            details = last_transcript.get("failure_details")
            if isinstance(details, list) and details:
                print("schema issues: " + ", ".join(str(item) for item in details[:8]))
        except (OSError, json.JSONDecodeError):
            pass
    print(f"smoke: failed with exit code {result}")
    return EXIT_ESCALATED


if __name__ == "__main__":
    raise SystemExit(main())
