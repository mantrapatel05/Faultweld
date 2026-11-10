from __future__ import annotations

from pathlib import Path
from unittest import TestCase

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "live-evals.yml"
CI = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


class LiveWorkflowTests(TestCase):
    def test_live_workflow_is_manual_only(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        for trigger in ("push:", "pull_request", "schedule:", "pull_request_target"):
            self.assertNotIn(trigger, text)

    def test_live_workflow_dry_runs_first_and_enforces_the_cap(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(text.index("--dry-run"), text.index("--require-estimate-within-cap"))
        self.assertIn("--max-cost-usd", text)
        self.assertIn("contents: read", text)

    def test_ci_never_sees_a_provider_key_or_live_mode(self) -> None:
        text = CI.read_text(encoding="utf-8")
        for forbidden in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "--mode live", "secrets."):
            self.assertNotIn(forbidden, text)
