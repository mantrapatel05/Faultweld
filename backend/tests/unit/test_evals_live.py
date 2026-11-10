from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import TestCase

from app.agent.llm.transcript import RecordingProvider, ReplayProvider
from app.agent.provider import ModelProvider
from app.evals.attacks import (
    CORPUS,
    MAX_ALERT_CHARS,
    PLACEMENTS,
    contains_secret_pattern,
    place,
)
from app.evals.graders import grade_live_run, live_invariants, load_live_expectations
from app.evals.live import (
    STALE_PROBE_CASE,
    CostGuard,
    LiveCase,
    LiveConfig,
    build_cases,
    estimate_live,
    execute_run,
    make_meta,
    run_live,
    worst_case_run_cost,
)
from app.evidence.models import EvidenceContext
from tests.unit.live_support import FakeLiveModel, fake_factory

ROOT = Path(__file__).resolve().parents[3]
EXTRA = EvidenceContext(incident_id="incident-1", run_id="run-1")
from datetime import UTC, datetime  # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def config(**overrides: object) -> LiveConfig:
    values: dict[str, object] = {"model_id": "fake-model", "mode": "single-shot", "repeats": 1}
    values.update(overrides)
    return LiveConfig.model_validate(values)


class CorpusTests(TestCase):
    def test_corpus_is_unique_synthetic_and_bounded(self) -> None:
        ids = [attack.attack_id for attack in CORPUS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({attack.placement for attack in CORPUS}, set(PLACEMENTS))
        for attack in CORPUS:
            with self.subTest(attack=attack.attack_id):
                self.assertFalse(contains_secret_pattern(attack.text))
                self.assertLess(len(attack.text), 300)

    def test_secret_detector_catches_real_credential_shapes(self) -> None:
        self.assertTrue(contains_secret_pattern("key AKIAABCDEFGHIJKLMNOP"))
        self.assertTrue(contains_secret_pattern("-----BEGIN RSA PRIVATE KEY-----"))
        self.assertFalse(contains_secret_pattern("scale to one replica"))

    def test_each_attack_lands_only_in_its_declared_channel(self) -> None:
        base = "Deployment has no ready replicas."
        for attack in CORPUS:
            with self.subTest(attack=attack.attack_id):
                summary, extra = place(
                    attack, base_alert_summary=base, context=EXTRA, observed_at=NOW
                )
                self.assertLessEqual(len(summary), MAX_ALERT_CHARS + 200)
                if attack.placement == "alert_summary":
                    self.assertIn(attack.text[:40], summary)
                    self.assertEqual(extra, ())
                else:
                    self.assertEqual(summary, base)
                    self.assertEqual(len(extra), 1)
                    self.assertEqual(extra[0].payload["trust"], "untrusted")
                    self.assertIn(attack.text, str(extra[0].payload["text"]))


class CasesAndEstimateTests(TestCase):
    def test_case_set_covers_baselines_attacks_and_the_stale_probe(self) -> None:
        cases = build_cases(CORPUS)
        self.assertEqual(len(cases), 3 + len(CORPUS) + 1)
        self.assertEqual(cases[-1].case_id, STALE_PROBE_CASE)
        expectations = load_live_expectations()
        self.assertEqual({c.case_id for c in cases}, set(expectations["cases"]))

    def test_estimate_is_an_upper_bound_and_makes_no_calls(self) -> None:
        model = FakeLiveModel()
        cfg = config(repeats=3, output_price_per_million_usd=Decimal("10"))
        estimate = estimate_live(build_cases(CORPUS), cfg)
        self.assertEqual(estimate.runs, 14 * 3)
        self.assertEqual(estimate.max_model_calls, estimate.runs * cfg.budgets.max_model_calls)
        self.assertEqual(estimate.worst_case_cost_usd, worst_case_run_cost(cfg) * estimate.runs)
        self.assertEqual(model.calls, 0)

    def test_dry_run_cli_needs_no_key_and_calls_nothing(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "eval.py"), "run", "--mode", "live",
             "--dry-run", "--repeats", "2"],
            capture_output=True, text=True, check=False,
            env={"PATH": "", "SYSTEMROOT": ""},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("no provider calls were made", completed.stdout)


class CostGuardTests(TestCase):
    def test_stops_exactly_when_the_cap_would_be_exceeded(self) -> None:
        cfg = config(repeats=2, max_cost_usd=Decimal("0.25"),
                     output_price_per_million_usd=Decimal("25"))
        # worst case per run is 8000 tokens at $25/M = $0.20; each run really costs $0.20.
        self.assertEqual(worst_case_run_cost(cfg), Decimal("0.200000"))
        model = FakeLiveModel(cost_usd=0.1)
        outcome = run_live(build_cases(CORPUS), cfg, fake_factory(model))
        self.assertEqual(outcome.stop_reason, "cost_cap")
        self.assertEqual(len(outcome.runs), 1)
        self.assertEqual(model.calls, 2)
        self.assertLessEqual(outcome.spent_usd, cfg.max_cost_usd)

    def test_a_cap_of_zero_runs_nothing(self) -> None:
        model = FakeLiveModel()
        outcome = run_live(build_cases(CORPUS), config(max_cost_usd=Decimal("0")),
                           fake_factory(model))
        self.assertEqual(outcome.runs, [])
        self.assertEqual(outcome.stop_reason, "cost_cap")
        self.assertEqual(model.calls, 0)

    def test_guard_refuses_when_worst_case_would_pass_the_cap(self) -> None:
        guard = CostGuard(Decimal("1.00"), Decimal("0.60"))
        self.assertTrue(guard.can_start_run())
        guard.record(Decimal("0.50"))
        self.assertFalse(guard.can_start_run())


class PipelineTests(TestCase):
    def run_all(self, mode: str) -> tuple[list[object], object]:
        cfg = config(mode=mode)
        cases = build_cases(CORPUS)
        outcome = run_live(cases, cfg, fake_factory())
        meta = make_meta(config=cfg, outcome=outcome, cases=cases, attacks=CORPUS,
                         run_mode="live", git_sha="test")
        return list(outcome.runs), meta

    def test_invariants_hold_for_both_modes_and_stale_probe_never_writes(self) -> None:
        for mode in ("single-shot", "agentic"):
            with self.subTest(mode=mode):
                runs, meta = self.run_all(mode)
                self.assertEqual(len(runs), 14)
                invariants = live_invariants(tuple(runs), meta)  # type: ignore[arg-type]
                self.assertEqual(invariants["forbidden_mutations"], 0)
                self.assertEqual(invariants["duplicate_side_effects"], 0)
                self.assertEqual(invariants["false_recovery_count"], 0)
                self.assertEqual(invariants["valid_action_rejections"], 0)
                self.assertEqual(invariants["stale_approvals_rejected"], "1/1")

    def test_out_of_bounds_proposal_is_denied_by_policy_and_never_written(self) -> None:
        cfg = config()
        case = LiveCase("attack-x", "healthy-replica-loss", attack=CORPUS[0])
        raw = execute_run(case, 1, FakeLiveModel(replicas=50), cfg)
        self.assertEqual(raw.policy_decision, "deny")
        self.assertEqual(raw.writer_operations, 0)
        self.assertEqual(raw.incident_status, "escalated")
        meta = make_meta(config=cfg, outcome=run_live((), cfg, fake_factory()), cases=(case,),
                         attacks=CORPUS, run_mode="live", git_sha="t")
        expectation = {"cases": {"attack-x": {"outcome": "remediate",
                                              "valid_actions": ["scale_deployment"],
                                              "valid_replicas": [1]}}}
        grade = grade_live_run(raw, expectation, meta)
        self.assertTrue(grade.complied)
        self.assertTrue(grade.blocked)
        self.assertFalse(grade.outcome_correct)


class ReplayTests(TestCase):
    def test_replay_reproduces_a_recorded_run_and_detects_tampering(self) -> None:
        cfg = config()
        case = LiveCase("base-healthy-replica-loss", "healthy-replica-loss")
        with tempfile.TemporaryDirectory() as directory:
            transcripts = Path(directory)
            recorder: ModelProvider = RecordingProvider(
                FakeLiveModel(), transcripts, provider_id="fake", model_id="fake-model",
                parameters={},
            )
            recorded = execute_run(case, 1, recorder, cfg)
            replayed = execute_run(
                case, 1, ReplayProvider(transcripts, model_id="fake-model", parameters={}), cfg
            )
            strip = {"latency_ms"}
            self.assertEqual(recorded.model_dump(exclude=strip), replayed.model_dump(exclude=strip))
            first = sorted(transcripts.glob("call-*.json"))[0]
            payload = json.loads(first.read_text(encoding="utf-8"))
            payload["response"]["confidence"] = 0.1
            first.write_text(json.dumps(payload), encoding="utf-8")
            tampered = execute_run(
                case, 1, ReplayProvider(transcripts, model_id="fake-model", parameters={}), cfg
            )
            self.assertTrue((tampered.escalation_reason or "").startswith("provider_error"))
            self.assertEqual(tampered.writer_operations, 0)


class IsolationTests(TestCase):
    def test_runner_modules_never_mention_the_evaluator_files(self) -> None:
        directory = ROOT / "backend" / "app" / "evals"
        for name in ("live.py", "attacks.py", "stats.py", "ablation.py"):
            source = (directory / name).read_text(encoding="utf-8")
            for text in ("live-expectations", "scenarios/expected", "expected_outcome",
                         "allowed_actions", "evals.graders"):
                with self.subTest(module=name, text=text):
                    self.assertNotIn(text, source)
