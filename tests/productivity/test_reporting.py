"""End-to-end tests for app/productivity/reporting.py and exporters.py against
the synthetic fixture dataset."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.execution.repository import ExecutionRepository
from app.productivity.buckets import TimeBucket
from app.productivity.exporters import export_report_to_csv, export_report_to_json
from app.productivity.filters import ObservationFilters
from app.productivity.reporting import ProductivityService
from app.productivity.stats import ProductivityThresholds

THRESHOLDS = ProductivityThresholds(low=3, moderate=6, high=10)


def test_generate_report_all_time_covers_full_history(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    report = service.generate_report(period="all_time")

    assert report.period == "all_time"
    assert report.observation_count == report.global_stats.observation_count
    assert report.observation_count > 0
    assert set(report.by_category) == {"study", "exercise", "errand"}


def test_generate_report_includes_insights(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    report = service.generate_report(period="all_time")

    assert len(report.insights) > 0
    # The thin "errand" category should surface as an insufficient-data insight.
    assert any(insight.metric == "insufficient_data" and insight.category == "errand" for insight in report.insights)


def test_generate_report_last_7_days_can_be_empty(populated_repository) -> None:
    repository, clock = populated_repository
    service = ProductivityService(repository, thresholds=THRESHOLDS, clock=clock)

    # The fixture's data is all generated starting "now" (clock start) and moving forward,
    # so asking for the 7 days *before* the very first record yields nothing.
    from datetime import datetime, timezone

    service_before_data = ProductivityService(
        repository, thresholds=THRESHOLDS, clock=lambda: datetime(2020, 1, 1, tzinfo=timezone.utc)
    )
    report = service_before_data.generate_report(period="last_7_days")

    assert report.observation_count == 0
    assert report.global_stats.observation_count == 0
    assert report.global_stats.completion_rate is None
    assert report.insights == []


def test_predict_duration_via_service(populated_repository) -> None:
    from app.productivity.buckets import TimeBucket

    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    prediction = service.predict_duration(
        category="study", time_bucket=TimeBucket.MORNING, original_estimate_minutes=60
    )

    assert prediction.sample_count > 0
    assert prediction.predicted_duration_minutes > 0


def test_export_report_to_json_round_trips(populated_repository, tmp_path: Path) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)
    report = service.generate_report()

    output_path = tmp_path / "report.json"
    export_report_to_json(report, output_path)

    loaded = json.loads(output_path.read_text(encoding="utf-8"))
    assert loaded["observation_count"] == report.observation_count
    assert loaded["period"] == "all_time"
    assert "study" in loaded["by_category"]


def test_export_report_to_csv_round_trips(populated_repository, tmp_path: Path) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)
    report = service.generate_report()

    output_path = tmp_path / "report.csv"
    export_report_to_csv(report, output_path)

    with output_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    assert rows[0]["grouping"] == "global"
    category_rows = [row for row in rows if row["grouping"] == "category"]
    assert {row["key"] for row in category_rows} == {"study", "exercise", "errand"}
    # observation_count round-trips as a numeric string.
    study_row = next(row for row in category_rows if row["key"] == "study")
    assert int(study_row["observation_count"]) > 0


# ----------------------------------------------------------------------
# filters parameter (days + dimension filters) on generate_report/predict_duration
# ----------------------------------------------------------------------


def test_generate_report_filters_by_category(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    report = service.generate_report(filters=ObservationFilters(category="exercise"))

    assert report.observation_count == report.global_stats.observation_count
    assert set(report.by_category) == {"exercise"}


def test_generate_report_filters_by_days_overrides_period(populated_repository) -> None:
    repository, clock = populated_repository
    service = ProductivityService(repository, thresholds=THRESHOLDS, clock=clock)

    # The fixture writes data starting at the clock's start and moving forward;
    # asking for a 1-day window from that same starting instant should return far
    # fewer observations than "all_time".
    all_time_report = service.generate_report(period="all_time")
    narrow_report = service.generate_report(filters=ObservationFilters(days=0))

    assert narrow_report.observation_count <= all_time_report.observation_count


def test_generate_report_omitted_filters_matches_period_only_call(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    without_filters_arg = service.generate_report(period="all_time")
    with_none_filters = service.generate_report(period="all_time", filters=None)

    assert without_filters_arg.observation_count == with_none_filters.observation_count


def test_predict_duration_respects_category_filter(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    prediction = service.predict_duration(
        category="exercise",
        time_bucket=TimeBucket.EVENING,
        original_estimate_minutes=30,
        filters=ObservationFilters(category="exercise"),
    )

    assert prediction.sample_count > 0


# ----------------------------------------------------------------------
# build_dashboard
# ----------------------------------------------------------------------


def test_build_dashboard_on_populated_history(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    dashboard = service.build_dashboard()

    assert dashboard.observation_count > 0
    assert set(dashboard.by_category) == {"study", "exercise", "errand"}
    assert set(dashboard.best_supported_time_bucket_by_category) == {"study", "exercise", "errand"}
    # Every best-supported bucket key must be one that actually appears in by_category_and_time_bucket.
    for category, time_bucket in dashboard.best_supported_time_bucket_by_category.items():
        assert f"{category}/{time_bucket}" in dashboard.by_category_and_time_bucket
    assert dashboard.recent_trend.baseline_observation_count == dashboard.observation_count
    assert len(dashboard.insights) > 0


def test_build_dashboard_respects_filters(populated_repository) -> None:
    repository: ExecutionRepository = populated_repository[0]
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    dashboard = service.build_dashboard(filters=ObservationFilters(category="errand"))

    assert dashboard.window_days is None
    assert set(dashboard.by_category) == {"errand"}
    assert dashboard.global_stats.observation_count == dashboard.observation_count


def test_build_dashboard_on_empty_history(repository: ExecutionRepository) -> None:
    service = ProductivityService(repository, thresholds=THRESHOLDS)

    dashboard = service.build_dashboard()

    assert dashboard.observation_count == 0
    assert dashboard.by_category == {}
    assert dashboard.best_supported_time_bucket_by_category == {}
    assert dashboard.global_stats.completion_rate is None
    assert dashboard.recent_trend.recent_observation_count == 0
    assert dashboard.insights == []
