"""
ml_features.py

Turns completed, plausible-duration Observation rows into ML-ready feature
rows for the evidence-gated duration-prediction experiment (see
ml_evaluation.py).

Permitted feature columns are limited to information available before a
task begins: planned_duration, priority, planned_start, category, tag,
time_bucket (already derived from planned_start by data_prep.py),
day_of_week (already derived from created_at), and historical aggregates
computed only from earlier rows. Actual duration, focus/energy/
interruption feedback, and completion outcome are never used as inputs --
actual_duration_minutes appears on MLFeatureRow only as the prediction
target, and is excluded from NUMERIC_COLUMNS/CATEGORICAL_COLUMNS.

Leakage rule: every historical-aggregate feature on a row is computed from
observations strictly earlier (by created_at, execution_id as a
deterministic tiebreak) than that row -- never from the row itself or any
later row. This module performs the single canonical chronological pass
that ml_evaluation.py's median-predictor comparison arm also relies on
(via sort_eligible_observations), so both arms of the comparison see
history in the exact same order.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from pydantic import BaseModel

from app.productivity.data_prep import Observation

FEATURE_SCHEMA_VERSION = "v1"

NUMERIC_COLUMNS = [
    "planned_duration",
    "priority",
    "planned_start",
    "category_median_duration_so_far",
    "category_sample_count_so_far",
    "category_time_bucket_median_duration_so_far",
    "category_time_bucket_sample_count_so_far",
    "global_median_duration_so_far",
    "global_sample_count_so_far",
]

CATEGORICAL_COLUMNS = ["category", "tag", "time_bucket", "day_of_week"]


class MLFeatureRow(BaseModel):
    """
    One training/evaluation example: pre-task-known inputs plus the actual
    outcome. actual_duration_minutes is the prediction target -- it is
    never included in NUMERIC_COLUMNS/CATEGORICAL_COLUMNS and must never be
    passed to the sklearn pipeline as an input.
    """

    execution_id: str
    created_at: str

    planned_duration: int
    priority: int
    planned_start: int
    category: str
    tag: str
    time_bucket: str
    day_of_week: str

    category_median_duration_so_far: float | None
    category_sample_count_so_far: int
    category_time_bucket_median_duration_so_far: float | None
    category_time_bucket_sample_count_so_far: int
    global_median_duration_so_far: float | None
    global_sample_count_so_far: int

    actual_duration_minutes: float


def is_ml_eligible(observation: Observation) -> bool:
    """The same completed + plausible-duration basis app/productivity/prediction.py uses."""
    return (
        observation.is_completed
        and observation.actual_active_duration_minutes is not None
        and observation.is_duration_plausible
    )


def sort_eligible_observations(observations: list[Observation]) -> list[Observation]:
    """
    The one canonical chronological order used by every ML module: only
    eligible (completed, plausible-duration) observations, sorted by
    (created_at, execution_id) so a timestamp collision still has a
    deterministic tiebreak.
    """
    eligible = [observation for observation in observations if is_ml_eligible(observation)]
    return sorted(eligible, key=lambda observation: (observation.created_at, observation.execution_id))


def build_feature_rows(observations: list[Observation]) -> list[MLFeatureRow]:
    """
    Build one MLFeatureRow per eligible observation, in chronological order.
    rows[i] corresponds to sort_eligible_observations(observations)[i] --
    every eligible observation produces exactly one row, in the same order,
    which ml_evaluation.py relies on to keep the median-predictor comparison
    arm aligned with the ML feature rows.

    Historical-aggregate features for row i are computed from the running
    per-key buckets *before* row i's own actual duration is appended to
    them, so no observation ever leaks into its own or an earlier row's
    features.
    """
    ordered = sort_eligible_observations(observations)

    category_durations: dict[str, list[float]] = defaultdict(list)
    category_time_bucket_durations: dict[tuple[str, str], list[float]] = defaultdict(list)
    global_durations: list[float] = []

    rows: list[MLFeatureRow] = []

    for observation in ordered:
        category_bucket = category_durations[observation.category]
        combo_key = (observation.category, observation.time_bucket.value)
        combo_bucket = category_time_bucket_durations[combo_key]

        rows.append(
            MLFeatureRow(
                execution_id=observation.execution_id,
                created_at=observation.created_at,
                planned_duration=observation.planned_duration,
                priority=observation.priority,
                planned_start=observation.planned_start,
                category=observation.category,
                tag=observation.tag,
                time_bucket=observation.time_bucket.value,
                day_of_week=observation.day_of_week,
                category_median_duration_so_far=(
                    round(statistics.median(category_bucket), 2) if category_bucket else None
                ),
                category_sample_count_so_far=len(category_bucket),
                category_time_bucket_median_duration_so_far=(
                    round(statistics.median(combo_bucket), 2) if combo_bucket else None
                ),
                category_time_bucket_sample_count_so_far=len(combo_bucket),
                global_median_duration_so_far=(
                    round(statistics.median(global_durations), 2) if global_durations else None
                ),
                global_sample_count_so_far=len(global_durations),
                actual_duration_minutes=observation.actual_active_duration_minutes,
            )
        )

        # Only after this row's features are computed does its own actual
        # duration become visible to subsequent rows -- this is the entire
        # leakage guarantee.
        actual = observation.actual_active_duration_minutes
        category_bucket.append(actual)
        combo_bucket.append(actual)
        global_durations.append(actual)

    return rows
