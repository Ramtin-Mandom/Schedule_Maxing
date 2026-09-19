"""
exporters.py

Write an already-built ProductivityReport to JSON or CSV. Both functions are
pure over the report object -- no database access, no formatting decisions
that depend on where the report came from.
"""

from __future__ import annotations

import csv
from pathlib import Path

from app.productivity.reporting import ProductivityReport
from app.productivity.stats import SegmentStats

_SEGMENT_FIELDS = (
    "observation_count",
    "evidence_level",
    "completion_rate",
    "skip_rate",
    "on_schedule_start_rate",
    "median_start_delay_minutes",
    "duration_mae_minutes",
    "median_actual_duration_minutes",
    "median_planned_duration_minutes",
    "median_actual_to_planned_ratio",
    "median_duration_variance_minutes",
    "avg_focus_rating",
    "avg_energy_rating",
    "productive_active_minutes",
)


def export_report_to_json(report: ProductivityReport, path: str | Path) -> None:
    """Write the full report as pretty-printed JSON."""
    Path(path).write_text(report.model_dump_json(indent=2), encoding="utf-8")


def export_report_to_csv(report: ProductivityReport, path: str | Path) -> None:
    """
    Write one row per segment (global + every group in every grouping) to CSV,
    with "grouping" and "key" columns identifying which segment each row is.
    """
    rows: list[dict[str, object]] = []
    rows.append(_segment_row("global", "", report.global_stats))

    groupings = (
        ("category", report.by_category),
        ("tag", report.by_tag),
        ("day_of_week", report.by_day_of_week),
        ("time_bucket", report.by_time_bucket),
        ("category_and_time_bucket", report.by_category_and_time_bucket),
    )
    for grouping_name, segments in groupings:
        for key in sorted(segments):
            rows.append(_segment_row(grouping_name, key, segments[key]))

    with Path(path).open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["grouping", "key", *_SEGMENT_FIELDS])
        writer.writeheader()
        writer.writerows(rows)


def _segment_row(grouping: str, key: str, segment: SegmentStats) -> dict[str, object]:
    row: dict[str, object] = {"grouping": grouping, "key": key}
    for field in _SEGMENT_FIELDS:
        value = getattr(segment, field)
        row[field] = value.value if hasattr(value, "value") else value
    return row
