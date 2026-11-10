from __future__ import annotations

import re
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import TestCase

from app.evals.attacks import CORPUS
from app.evals.live import (
    LiveConfig,
    LiveRunMeta,
    LiveRunRaw,
    build_cases,
    load_live_raw,
    make_meta,
    run_live,
    write_live_raw,
)
from app.evals.report import build_live_report, markdown_live_report, write_live_report
from tests.unit.live_support import FakeLiveModel, fake_factory

BANNED = re.compile(r"\b(secure|safe|production-ready|injection-proof|robust|guaranteed)\b", re.I)


def record(
    model: FakeLiveModel | None = None, **overrides: object
) -> tuple[LiveRunMeta, tuple[LiveRunRaw, ...]]:
    values: dict[str, object] = {
        "model_id": "fake-model", "mode": "single-shot", "repeats": 2,
    }
    values.update(overrides)
    cfg = LiveConfig.model_validate(values)
    cases = build_cases(CORPUS)
    outcome = run_live(cases, cfg, fake_factory(model))
    meta = make_meta(config=cfg, outcome=outcome, cases=cases, attacks=CORPUS,
                     run_mode="live", git_sha="abc123")
    return meta, tuple(outcome.runs)


class LiveReportTests(TestCase):
    def test_report_is_byte_identical_across_builds_and_a_disk_round_trip(self) -> None:
        meta, runs = record()
        first = markdown_live_report(build_live_report(meta, runs))
        second = markdown_live_report(build_live_report(meta, tuple(reversed(runs))))
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as directory:
            write_live_raw(Path(directory), meta, list(runs))
            loaded_meta, loaded_runs = load_live_raw(Path(directory))
            self.assertEqual(
                first, markdown_live_report(build_live_report(loaded_meta, loaded_runs))
            )

    def test_every_rate_shows_n_and_an_interval_and_model_and_date(self) -> None:
        meta, runs = record()
        report = build_live_report(meta, runs)
        text = markdown_live_report(report)
        self.assertIn("fake-model", text)
        self.assertIn(report.date_utc, text)
        for row in report.rates:
            if row.n:
                self.assertIsNotNone(row.low)
                self.assertIsNotNone(row.high)
        self.assertIn("Wilson interval", text)
        self.assertIn("a sample, with uncertainty", text)

    def test_empty_scope_prints_n_zero_instead_of_a_rate(self) -> None:
        meta, runs = record()
        report = build_live_report(meta, runs)
        text = markdown_live_report(report)
        # The fake model never complies with an attack, so compliance has runs but blocks have none.
        self.assertIn("injection_block_rate: n=0", text)

    def test_no_banned_wording(self) -> None:
        meta, runs = record()
        self.assertIsNone(BANNED.search(markdown_live_report(build_live_report(meta, runs))))

    def test_cost_cap_stop_is_reported_as_partial_data(self) -> None:
        meta, runs = record(
            FakeLiveModel(cost_usd=0.1),
            max_cost_usd=Decimal("0.25"),
            output_price_per_million_usd=Decimal("25"),
        )
        report = build_live_report(meta, runs)
        self.assertTrue(report.partial)
        self.assertIn("Partial data", markdown_live_report(report))

    def test_failed_runs_are_listed_not_dropped(self) -> None:
        meta, runs = record()
        broken = runs[0].model_copy(
            update={"escalation_reason": "provider_error:timeout", "status": "escalated"}
        )
        report = build_live_report(meta, (broken, *runs[1:]))
        self.assertEqual(len(report.failed_runs), 1)
        self.assertEqual(report.runs_completed, len(runs))

    def test_a_write_on_a_stale_probe_is_reported_as_a_violation(self) -> None:
        meta, runs = record()
        probe = next(run for run in runs if run.stale_probe)
        forged = probe.model_copy(update={"writer_operations": 1})
        report = build_live_report(meta, tuple(forged if run is probe else run for run in runs))
        self.assertGreater(report.invariants["forbidden_mutations"], 0)
        self.assertFalse(report.invariants_clean)
        self.assertIn("system bug", markdown_live_report(report))

    def test_report_writes_json_and_markdown(self) -> None:
        meta, runs = record()
        with tempfile.TemporaryDirectory() as directory:
            json_path, md_path = write_live_report(build_live_report(meta, runs), Path(directory))
            self.assertTrue(json_path.is_file() and md_path.is_file())
