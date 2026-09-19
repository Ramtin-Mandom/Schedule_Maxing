"""
report_cli.py

Thin CLI entry point for the productivity report. All the actual work is
done by ProductivityService (app/productivity/reporting.py); this module
only parses arguments and formats output.

Usage:
    python -m app.productivity.report_cli
    python -m app.productivity.report_cli --period week
    python -m app.productivity.report_cli --format json --output report.json
    python -m app.productivity.report_cli --format csv --output report.csv
    python -m app.productivity.report_cli --db-path path/to/executions.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.productivity.exporters import export_report_to_csv, export_report_to_json
from app.productivity.reporting import ProductivityReport, ProductivityService

_PERIOD_LABELS = {"all_time": "All-time", "last_7_days": "Last 7 days"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print or export a Schedule Maxing productivity report.")
    parser.add_argument(
        "--period",
        choices=["all", "week"],
        default="all",
        help="'all' for all-time history, 'week' for the last 7 days (default: all).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "csv"],
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="File to write the report to. Required for --format csv; optional for json/text (defaults to stdout).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to the execution database (default: the configured local data directory).",
    )
    return parser


def render_report_text(report: ProductivityReport) -> str:
    """Deterministic, human-readable rendering of a ProductivityReport."""
    lines = [
        f"Schedule Maxing Productivity Report ({_PERIOD_LABELS.get(report.period, report.period)})",
        f"Generated at: {report.generated_at}",
        f"Observations: {report.observation_count}",
        "",
        "Global:",
        f"  {_render_segment(report.global_stats)}",
    ]

    for title, segments in (
        ("By category", report.by_category),
        ("By tag", report.by_tag),
        ("By day of week", report.by_day_of_week),
        ("By time bucket", report.by_time_bucket),
        ("By category + time bucket", report.by_category_and_time_bucket),
    ):
        lines.append("")
        lines.append(f"{title}:")
        if not segments:
            lines.append("  (no data)")
            continue
        for key in sorted(segments):
            lines.append(f"  {key}: {_render_segment(segments[key])}")

    lines.append("")
    lines.append("Insights:")
    if not report.insights:
        lines.append("  (none yet -- not enough history)")
    else:
        for insight in report.insights:
            lines.append(f"  - {insight.text}")

    return "\n".join(lines)


def _render_segment(segment) -> str:
    def fmt(value: object, suffix: str = "") -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:g}{suffix}"
        return f"{value}{suffix}"

    return (
        f"n={segment.observation_count} evidence={segment.evidence_level.value} "
        f"completion={fmt(segment.completion_rate)} skip={fmt(segment.skip_rate)} "
        f"on_schedule={fmt(segment.on_schedule_start_rate)} "
        f"median_start_delay={fmt(segment.median_start_delay_minutes, 'm')} "
        f"duration_mae={fmt(segment.duration_mae_minutes, 'm')} "
        f"median_actual_duration={fmt(segment.median_actual_duration_minutes, 'm')} "
        f"median_planned_duration={fmt(segment.median_planned_duration_minutes, 'm')} "
        f"median_ratio={fmt(segment.median_actual_to_planned_ratio)} "
        f"median_variance={fmt(segment.median_duration_variance_minutes, 'm')} "
        f"avg_focus={fmt(segment.avg_focus_rating)} avg_energy={fmt(segment.avg_energy_rating)} "
        f"productive_minutes={fmt(segment.productive_active_minutes, 'm')}"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    period = "last_7_days" if args.period == "week" else "all_time"

    if args.format == "csv" and args.output is None:
        print("--format csv requires --output PATH", file=sys.stderr)
        return 2

    connection = get_connection(args.db_path)
    try:
        service = ProductivityService(ExecutionRepository(connection))
        report = service.generate_report(period=period)
    finally:
        connection.close()

    if args.format == "text":
        text = render_report_text(report)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text)
    elif args.format == "json":
        if args.output:
            export_report_to_json(report, args.output)
        else:
            print(report.model_dump_json(indent=2))
    else:
        export_report_to_csv(report, args.output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
