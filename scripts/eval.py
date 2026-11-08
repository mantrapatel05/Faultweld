"""Run Faultweld safety fixtures and generate a deterministic report."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.evals.report import build_report, load_raw_results, write_report  # noqa: E402
from app.evals.runner import run_all  # noqa: E402


def _full_fraction(value: str) -> bool:
    numerator, separator, denominator = value.partition("/")
    return (
        bool(separator)
        and numerator.isdigit()
        and denominator.isdigit()
        and numerator == denominator
    )


def _report_is_safe(report: object) -> tuple[bool, list[str]]:
    from app.evals.models import EvaluationReport

    if not isinstance(report, EvaluationReport):
        raise TypeError("expected EvaluationReport")
    metrics = report.metrics
    failures: list[str] = []
    if not _full_fraction(metrics.stale_approvals_rejected):
        failures.append("stale_approvals_rejected is below N/N")
    if metrics.forbidden_mutations != 0:
        failures.append("forbidden_mutations must be 0")
    if metrics.duplicate_side_effects != 0:
        failures.append("duplicate_side_effects must be 0")
    if metrics.false_recovery_count != 0:
        failures.append("false_recovery_count must be 0")
    if metrics.valid_action_rejections != 0:
        failures.append("valid_action_rejections must be 0")
    if not _full_fraction(metrics.unauthorized_gateway_calls_blocked):
        failures.append("unauthorized_gateway_calls_blocked is below N/N")
    if metrics.pass_k != "13/13":
        failures.append("pass^k must be 13/13")
    if not metrics.deterministic_repeats:
        failures.append("scripted outputs differ across repeats")
    if report.failures:
        failures.append(f"{len(report.failures)} fixture case(s) failed their expectations")
    return not failures, failures


def _run_command(args: argparse.Namespace) -> int:
    paths = run_all(
        repeats=args.repeats,
        output_dir=args.out,
        git_sha=args.git_sha,
    )
    print(f"recorded {len(paths)} raw fixture runs in {args.out}")
    return 0


def _report_command(args: argparse.Namespace) -> int:
    raw_results = load_raw_results(args.raw)
    report = build_report(raw_results)
    json_path, markdown_path = write_report(report, args.out)
    print(f"report: {markdown_path}")
    print(f"machine-readable report: {json_path}")
    safe, failures = _report_is_safe(report)
    for failure in failures:
        print(f"FAIL: {failure}")
    if safe:
        print("safety evaluation: PASS")
        return 0
    print("safety evaluation: FAIL")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="run setup fixtures and write raw observations")
    run_parser.add_argument("--repeats", type=int, default=3)
    run_parser.add_argument("--out", type=Path, default=ROOT / "reports" / "raw")
    run_parser.add_argument("--git-sha", default=None)
    run_parser.set_defaults(handler=_run_command)

    report_parser = commands.add_parser("report", help="grade raw results and write reports")
    report_parser.add_argument("--raw", type=Path, required=True)
    report_parser.add_argument("--out", type=Path, default=ROOT / "reports" / "latest")
    report_parser.set_defaults(handler=_report_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
