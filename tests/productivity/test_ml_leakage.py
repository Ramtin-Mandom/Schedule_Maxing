"""
Leakage-prevention tests for app/productivity/ml_features.py.

The core claim: a row's historical-aggregate features depend only on
observations strictly earlier than it. These tests prove that by
constructing a case where inserting an extreme, later-dated observation
changes a *later* row's aggregate (correct -- that's real history) but
never an *earlier* row's aggregate (the actual leakage guarantee).
"""

from __future__ import annotations

import statistics

from app.execution.models import ExecutionStatus
from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation
from app.productivity.ml_features import build_feature_rows


def _completed_observation(*, execution_id: str, created_at: str, actual_minutes: float) -> Observation:
    return Observation(
        execution_id=execution_id,
        task_name="task",
        category="study",
        tag="math",
        priority=5,
        status=ExecutionStatus.COMPLETED,
        planned_date=1,
        planned_start=540,
        planned_end=600,
        planned_duration=60,
        created_at=created_at,
        time_bucket=TimeBucket.MORNING,
        day_of_week="Monday",
        actual_active_duration_minutes=actual_minutes,
        duration_variance_minutes=None,
        start_delay_minutes=0.0,
        focus_rating=None,
        energy_rating=None,
        interruption_count=None,
        is_completed=True,
        is_skipped=False,
        is_terminal=True,
        is_duration_plausible=True,
    )


def test_earlier_row_is_unaffected_by_an_inserted_later_extreme_row() -> None:
    obs_a = _completed_observation(execution_id="a", created_at="2024-01-01T09:00:00+00:00", actual_minutes=50.0)
    obs_extreme = _completed_observation(
        execution_id="extreme", created_at="2024-01-02T09:00:00+00:00", actual_minutes=1400.0
    )
    obs_c = _completed_observation(execution_id="c", created_at="2024-01-03T09:00:00+00:00", actual_minutes=70.0)

    rows_without_extreme = build_feature_rows([obs_a, obs_c])
    rows_with_extreme = build_feature_rows([obs_a, obs_extreme, obs_c])

    row_a_without = next(row for row in rows_without_extreme if row.execution_id == "a")
    row_a_with = next(row for row in rows_with_extreme if row.execution_id == "a")

    # obs_a has nothing before it in either dataset -- its features must be identical.
    assert row_a_without == row_a_with


def test_later_row_correctly_reflects_a_genuinely_earlier_extreme_row() -> None:
    obs_a = _completed_observation(execution_id="a", created_at="2024-01-01T09:00:00+00:00", actual_minutes=50.0)
    obs_extreme = _completed_observation(
        execution_id="extreme", created_at="2024-01-02T09:00:00+00:00", actual_minutes=1400.0
    )
    obs_c = _completed_observation(execution_id="c", created_at="2024-01-03T09:00:00+00:00", actual_minutes=70.0)

    rows_without_extreme = build_feature_rows([obs_a, obs_c])
    rows_with_extreme = build_feature_rows([obs_a, obs_extreme, obs_c])

    row_c_without = next(row for row in rows_without_extreme if row.execution_id == "c")
    row_c_with = next(row for row in rows_with_extreme if row.execution_id == "c")

    # obs_c comes after obs_extreme chronologically, so its aggregate legitimately changes.
    assert row_c_without.category_median_duration_so_far == 50.0
    assert row_c_with.category_median_duration_so_far == statistics.median([50.0, 1400.0])
    assert row_c_with.category_sample_count_so_far == 2


def test_extreme_rows_own_features_do_not_include_itself() -> None:
    obs_a = _completed_observation(execution_id="a", created_at="2024-01-01T09:00:00+00:00", actual_minutes=50.0)
    obs_extreme = _completed_observation(
        execution_id="extreme", created_at="2024-01-02T09:00:00+00:00", actual_minutes=1400.0
    )

    rows = build_feature_rows([obs_a, obs_extreme])
    row_extreme = next(row for row in rows if row.execution_id == "extreme")

    assert row_extreme.category_median_duration_so_far == 50.0
    assert row_extreme.category_sample_count_so_far == 1
