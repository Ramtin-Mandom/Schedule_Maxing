"""
ml_prediction.py

Runtime-facing ML prediction: consult the persisted activation decision
and, only if ML is active, build a single feature row for the task being
estimated (using aggregates from the caller's *entire* current history --
at real prediction time there is no "future" to leak from, unlike the
chronological train/test evaluation in ml_evaluation.py) and run it through
the trained pipeline.

Safety contract: predict_duration_with_ml never raises. Any failure at all
-- missing/stale/corrupt artifact, ML inactive, an exception from the
pipeline itself, an observation list it can't process -- returns None,
which every caller must treat as "fall back to the median predictor."
ML is never the only prediction path.
"""

from __future__ import annotations

import statistics

import pandas as pd
from pydantic import BaseModel

from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation
from app.productivity.ml_features import CATEGORICAL_COLUMNS, NUMERIC_COLUMNS, sort_eligible_observations
from app.productivity.ml_persistence import load_model_artifact


class MLDurationPrediction(BaseModel):
    """
    The ML predictor's result. Deliberately a separate type from
    app.productivity.prediction.DurationPrediction (which is shaped around
    the median predictor's fallback-level hierarchy) rather than forcing ML
    into that hierarchy's semantics.
    """

    predicted_duration_minutes: float
    sample_count: int
    explanation: str


def predict_duration_with_ml(
    observations: list[Observation],
    *,
    category: str,
    tag: str,
    priority: int,
    time_bucket: TimeBucket,
    day_of_week: str,
    planned_duration: int,
    planned_start: int,
    data_dir: str | None = None,
) -> MLDurationPrediction | None:
    """Predict a task's actual duration with the persisted ML model, or None if ML isn't usable right now."""
    try:
        artifact = load_model_artifact(data_dir)
        if artifact is None:
            return None

        pipeline, metadata = artifact
        if not metadata.activation_decision.is_active:
            return None

        history = sort_eligible_observations(observations)
        aggregates = _current_aggregates(history, category=category, time_bucket=time_bucket)

        feature_row = {
            "planned_duration": planned_duration,
            "priority": priority,
            "planned_start": planned_start,
            "category": category,
            "tag": tag,
            "time_bucket": time_bucket.value,
            "day_of_week": day_of_week,
            **aggregates,
        }
        frame = pd.DataFrame([feature_row])[NUMERIC_COLUMNS + CATEGORICAL_COLUMNS]

        predicted_minutes = round(float(pipeline.predict(frame)[0]), 2)

        return MLDurationPrediction(
            predicted_duration_minutes=predicted_minutes,
            sample_count=len(history),
            explanation=(
                f"Predicted by the evidence-gated ML model, trained on {len(history)} completed task(s). "
                f"Activated because: {metadata.activation_decision.reason}"
            ),
        )
    except Exception:
        return None


def _current_aggregates(
    history: list[Observation],
    *,
    category: str,
    time_bucket: TimeBucket,
) -> dict[str, float | int | None]:
    category_values = [
        observation.actual_active_duration_minutes for observation in history if observation.category == category
    ]
    combo_values = [
        observation.actual_active_duration_minutes
        for observation in history
        if observation.category == category and observation.time_bucket == time_bucket
    ]
    global_values = [observation.actual_active_duration_minutes for observation in history]

    return {
        "category_median_duration_so_far": round(statistics.median(category_values), 2) if category_values else None,
        "category_sample_count_so_far": len(category_values),
        "category_time_bucket_median_duration_so_far": (
            round(statistics.median(combo_values), 2) if combo_values else None
        ),
        "category_time_bucket_sample_count_so_far": len(combo_values),
        "global_median_duration_so_far": round(statistics.median(global_values), 2) if global_values else None,
        "global_sample_count_so_far": len(global_values),
    }
