from __future__ import annotations

from unittest import TestCase

from app.evals.ablation import Arm, mode_arms, model_arms, run_ablation
from app.evals.attacks import CORPUS
from app.evals.live import LiveConfig, build_cases, make_meta
from app.evals.report import build_live_report, compare_arms
from tests.unit.live_support import fake_factory

BASE = LiveConfig(model_id="fake-model", mode="agentic", repeats=1)


class AblationTests(TestCase):
    def test_mode_arms_differ_only_in_mode(self) -> None:
        single, agentic = mode_arms(BASE, fake_factory())
        self.assertEqual(single.config.mode, "single-shot")
        self.assertEqual(agentic.config.mode, "agentic")
        self.assertEqual(
            single.config.model_copy(update={"mode": "agentic"}), agentic.config
        )

    def test_model_arms_are_agentic_and_labelled_by_model(self) -> None:
        arms = model_arms(BASE, [("small", fake_factory()), ("large", fake_factory())])
        self.assertEqual([arm.label for arm in arms], ["small", "large"])
        self.assertTrue(all(arm.config.mode == "agentic" for arm in arms))
        self.assertEqual([arm.config.model_id for arm in arms], ["small", "large"])

    def test_arms_run_the_same_cases_and_repeats(self) -> None:
        cases = build_cases(CORPUS)
        results = run_ablation(mode_arms(BASE, fake_factory()), cases)
        self.assertEqual({len(r.outcome.runs) for r in results}, {len(cases)})
        for result in results:
            self.assertEqual(
                [(run.case_id, run.repeat_index) for run in result.outcome.runs],
                [(case.case_id, 1) for case in cases],
            )

    def test_duplicate_labels_and_unequal_repeats_are_refused(self) -> None:
        arm = Arm("a", BASE, fake_factory())
        with self.assertRaises(ValueError):
            run_ablation([arm, arm], build_cases(CORPUS))
        other = Arm("b", BASE.model_copy(update={"repeats": 2}), fake_factory())
        with self.assertRaises(ValueError):
            run_ablation([arm, other], build_cases(CORPUS))

    def test_comparison_states_unequal_run_counts(self) -> None:
        cases = build_cases(CORPUS)
        reports = {}
        for result in run_ablation(mode_arms(BASE, fake_factory()), cases):
            cfg = BASE.model_copy(update={"mode": result.mode})
            meta = make_meta(config=cfg, outcome=result.outcome, cases=cases, attacks=CORPUS,
                             run_mode="live", git_sha="t")
            reports[result.label] = build_live_report(meta, tuple(result.outcome.runs))
        self.assertNotIn("Unequal run counts", compare_arms(reports))
        short = reports["agentic"].model_copy(update={"runs_completed": 3})
        self.assertIn("Unequal run counts", compare_arms({**reports, "agentic": short}))
