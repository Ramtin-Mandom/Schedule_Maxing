"""Tests for app/productivity/ml_features.py: feature-row construction, the
permitted-feature-column contract, and eligibility filtering."""

from __future__ import annotations

from app.execution.models import ExecutionStatus
from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation
from app.productivity.ml_features import (
    CATEGORICAL_COLUMNS,
    NUMERIC_COLUMNS,
    build_feature_rows,
    is_ml_eligible,
    sort_eligible_observations,
)


def _observation(
    *,
    execution_id: str,
    created_at: str,
    category: str = "study",
    tag: str = "math",
    time_bucket: TimeBucket = TimeBucket.MORNING,
    day_of_week: str = "Monday",
    actual_minutes: float | None = 60.0,
    planned_duration: int = 60,
    priority: int = 5,
    planned_start: int = 540,
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    plausible: bool = True,
) -> Observation:
    is_completed = status == ExecutionStatus.COMPLETED
    return Observation(
        execution_id=execution_id,
        task_name="task",
        category=category,
        tag=tag,
        priority=priority,
        status=status,
        planned_date=1,
        planned_start=planned_start,
        planned_end=planned_start + planned_duration,
        planned_duration=planned_duration,
        created_at=created_at,
        time_bucket=time_bucket,
        day_of_week=day_of_week,
        actual_active_duration_minutes=actual_minutes,
        duration_variance_minutes=None,
        start_delay_minutes=0.0,
        focus_rating=None,
        energy_rating=None,
        interruption_count=None,
        is_completed=is_completed,
        is_skipped=status == ExecutionStatus.SKIPPED,
        is_terminal=is_completed or status == ExecutionStatus.SKIPPED,
        is_duration_plausible=plausible,
    )


def test_permitted_feature_columns_exclude_post_task_information() -> None:
    forbidden = {
        "actual_active_duration_minutes",
        "actual_duration_minutes",
        "duration_variance_minutes",
        "focus_rating",
        "energy_rating",
        "interruption_count",
        "status",
        "is_completed",
        "is_skipped",
        "is_terminal",
    }
    used_columns = set(NUMERIC_COLUMNS) | set(CATEGORICAL_COLUMNS)
    assert used_columns.isdisjoint(forbidden)


def test_first_occurrence_of_a_category_has_no_prior_aggregate() -> None:
    obs = _observation(execution_id="a", created_at="2024-01-01T09:00:00+00:00")
    rows = build_feature_rows([obs])

    assert len(rows) == 1
    row = rows[0]
    assert row.category_median_duration_so_far is None
    assert row.category_sample_count_so_far == 0
    assert row.category_time_bucket_median_duration_so_far is None
    assert row.category_time_bucket_sample_count_so_far == 0
    assert row.global_median_duration_so_far is None
    assert row.global_sample_count_so_far == 0


def test_second_occurrence_reflects_only_the_first() -> None:
    obs_a = _observation(execution_id="a", created_at="2024-01-01T09:00:00+00:00", actual_minutes=50.0)
    obs_b = _observation(execution_id="b", created_at="2024-01-02T09:00:00+00:00", actual_minutes=70.0)

    rows = build_feature_rows([obs_a, obs_b])
    row_b = next(row for row in rows if row.execution_id == "b")

    assert row_b.category_median_duration_so_far == 50.0
    assert row_b.category_sample_count_so_far == 1
    assert row_b.global_median_duration_so_far == 50.0
    assert row_b.global_sample_count_so_far == 1
    # Row b's own actual duration (70) must not appear in its own aggregate.
    assert row_b.category_median_duration_so_far != 70.0


def test_categorical_values_pass_through_unencoded() -> None:
    obs = _observation(
        execution_id="a",
        created_at="2024-01-01T09:00:00+00:00",
        category="exercise",
        tag="cardio",
        time_bucket=TimeBucket.EVENING,
        day_of_week="Friday",
    )
    row = build_feature_rows([obs])[0]

    assert row.category == "exercise"
    assert row.tag == "cardio"
    assert row.time_bucket == "evening"
    assert row.day_of_week == "Friday"


def test_target_is_the_actual_duration_not_a_feature() -> None:
    obs = _observation(execution_id="a", created_at="2024-01-01T09:00:00+00:00", actual_minutes=63.0)
    row = build_feature_rows([obs])[0]

    assert row.actual_duration_minutes == 63.0
    assert "actual_duration_minutes" not in NUMERIC_COLUMNS
    assert "actual_duration_minutes" not in CATEGORICAL_COLUMNS


def test_is_ml_eligible_excludes_incomplete_skipped_and_implausible() -> None:
    scheduled = _observation(
        execution_id="a", created_at="t", status=ExecutionStatus.SCHEDULED, actual_minutes=None
    )
    skipped = _observation(execution_id="b", created_at="t", status=ExecutionStatus.SKIPPED, actual_minutes=None)
    implausible = _observation(execution_id="c", created_at="t", actual_minutes=2000.0, plausible=False)
    eligible = _observation(execution_id="d", created_at="t", actual_minutes=60.0)

    assert is_ml_eligible(scheduled) is False
    assert is_ml_eligible(skipped) is False
    assert is_ml_eligible(implausible) is False
    assert is_ml_eligible(eligible) is True


def test_build_feature_rows_only_includes_eligible_observations() -> None:
    scheduled = _observation(
        execution_id="a",
        created_at="2024-01-01T09:00:00+00:00",
        status=ExecutionStatus.SCHEDULED,
        actual_minutes=None,
    )
    implausible = _observation(
        execution_id="b", created_at="2024-01-01T10:00:00+00:00", actual_minutes=2000.0, plausible=False
    )
    eligible = _observation(execution_id="c", created_at="2024-01-01T11:00:00+00:00", actual_minutes=55.0)

    rows = build_feature_rows([scheduled, implausible, eligible])

    assert len(rows) == 1
    assert rows[0].execution_id == "c"


def test_sort_eligible_observations_orders_by_created_at_then_execution_id() -> None:
    obs_late = _observation(execution_id="z", created_at="2024-01-02T09:00:00+00:00")
    obs_early_b = _observation(execution_id="b", created_at="2024-01-01T09:00:00+00:00")
    obs_early_a = _observation(execution_id="a", created_at="2024-01-01T09:00:00+00:00")

    ordered = sort_eligible_observations([obs_late, obs_early_b, obs_early_a])

    assert [observation.execution_id for observation in ordered] == ["a", "b", "z"]


def test_build_feature_rows_row_order_matches_sort_eligible_observations() -> None:
    obs_1 = _observation(execution_id="1", created_at="2024-01-01T09:00:00+00:00")
    obs_2 = _observation(execution_id="2", created_at="2024-01-02T09:00:00+00:00")
    obs_3 = _observation(execution_id="3", created_at="2024-01-03T09:00:00+00:00")

    observations = [obs_3, obs_1, obs_2]
    ordered = sort_eligible_observations(observations)
    rows = build_feature_rows(observations)

    assert [row.execution_id for row in rows] == [observation.execution_id for observation in ordered]
