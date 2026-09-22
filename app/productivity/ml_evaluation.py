"""
ml_evaluation.py

Orchestrates the honest three-way comparison the ML duration-prediction
experiment is built around: the user's original estimate, the existing
hierarchical median predictor (app/productivity/prediction.py, untouched),
and a scikit-learn regression model (app/productivity/ml_model.py) -- all
scored on the identical, chronologically held-out test rows.

Fairness note: the median predictor is not simply re-run against the whole
dataset while the ML model only sees its train split. For each test row,
_median_predictions_for_test_rows calls prediction.predict_duration using
only observations strictly earlier (in the same canonical chronological
order ml_features.py uses) than that row, so neither arm gets to see
"future" data the other doesn't.

Activation policy: ML may become the active predictor only when the
configured minimum sample thresholds (MLGateConfig) are met AND its
held-out MAE is lower than the median predictor's held-out MAE on the same
test rows. decide_activation records the concrete numbers behind whichever
decision it makes -- enabling or rejecting ML is never asserted without
citing the real evaluation result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel
from sklearn.pipeline import Pipeline

from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation
from app.productivity.ml_features import MLFeatureRow, build_feature_rows, sort_eligible_observations
from app.productivity.ml_metrics import mean_absolute_error, median_absolute_error, within_tolerance_rate
from app.productivity.ml_model import predict_rows, train_ml_pipeline
from app.productivity.ml_split import chronological_split
from app.productivity.prediction import predict_duration
from app.productivity.stats import ProductivityThresholds


@dataclass(frozen=True)
class MLGateConfig:
    """
    Configurable, deliberately conservative minimum-history requirements for
    training and activating the ML predictor. These are independent of
    ProductivityThresholds (which governs the median predictor's own
    fallback-level thresholds, an unrelated concern).

    The stock test fixture (19 completed, plausible observations) does not
    meet these defaults -- that is the correct, honest "insufficient
    history" outcome on a small dataset, not something to tune away.
    """

    min_total_samples: int = 40
    min_train_samples: int = 25
    min_test_samples: int = 10
    test_fraction: float = 0.2


class PredictorMetrics(BaseModel):
    mae_minutes: float
    median_absolute_error_minutes: float
    within_15_min_rate: float
    within_30_min_rate: float
    sample_count: int


class PredictorComparison(BaseModel):
    """
    A snapshot of all three predictors' held-out performance on the same
    chronological test split. original_estimate/median_predictor/
    ml_predictor are all optional: they are None only when there wasn't
    even enough history to form a non-empty train/test split at all (e.g.
    zero completed executions) -- a distinct, stricter case from ML simply
    not clearing MLGateConfig's thresholds.
    """

    generated_at: str
    train_sample_count: int
    test_sample_count: int
    cutoff_created_at: str
    cutoff_index: int

    original_estimate: PredictorMetrics | None
    median_predictor: PredictorMetrics | None
    ml_predictor: PredictorMetrics | None

    insufficient_history_reason: str | None


class MLActivationDecision(BaseModel):
    is_active: bool
    reason: str
    comparison: PredictorComparison | None
    decided_at: str


def _metrics_for(predicted: list[float], actual: list[float]) -> PredictorMetrics:
    return PredictorMetrics(
        mae_minutes=mean_absolute_error(predicted, actual),
        median_absolute_error_minutes=median_absolute_error(predicted, actual),
        within_15_min_rate=within_tolerance_rate(predicted, actual, 15.0),
        within_30_min_rate=within_tolerance_rate(predicted, actual, 30.0),
        sample_count=len(actual),
    )


def _median_predictions_for_test_rows(
    eligible: list[Observation],
    cutoff_index: int,
    test_rows: list[MLFeatureRow],
    thresholds: ProductivityThresholds,
) -> list[float]:
    """
    For each test row (in chronological order), predict with the median
    predictor using only the observations strictly before it -- i.e. the
    train split plus whichever test rows have already "passed" -- never the
    full dataset. `eligible` must be sort_eligible_observations(...) of the
    same observations build_feature_rows was built from, so index i here
    lines up with feature row i.
    """
    predictions: list[float] = []
    for offset, row in enumerate(test_rows):
        history = eligible[: cutoff_index + offset]
        prediction = predict_duration(
            history,
            category=row.category,
            time_bucket=TimeBucket(row.time_bucket),
            original_estimate_minutes=float(row.planned_duration),
            thresholds=thresholds,
        )
        predictions.append(prediction.predicted_duration_minutes)
    return predictions


def evaluate_predictors(
    observations: list[Observation],
    *,
    thresholds: ProductivityThresholds = ProductivityThresholds(),
    gate_config: MLGateConfig = MLGateConfig(),
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    model_random_state: int = 0,
) -> tuple[PredictorComparison, Pipeline | None]:
    """
    Build features from `observations`, split chronologically, and evaluate
    all three predictors on the identical held-out test rows.

    Returns the comparison plus the trained ML pipeline (None whenever
    there wasn't enough history to even form a split, or MLGateConfig's
    sample-count thresholds were not met -- the pipeline is only returned
    when it was actually trained, regardless of whether it goes on to win
    the activation gate).
    """
    generated_at = clock().isoformat()
    eligible = sort_eligible_observations(observations)
    rows = build_feature_rows(observations)  # rows[i] corresponds to eligible[i]

    split = chronological_split(rows, test_fraction=gate_config.test_fraction)

    if not split.train_rows or not split.test_rows:
        reason = (
            f"Only {len(rows)} completed task(s) with a plausible actual duration are available; "
            "need at least one training row and one held-out test row to evaluate any predictor."
        )
        return (
            PredictorComparison(
                generated_at=generated_at,
                train_sample_count=len(split.train_rows),
                test_sample_count=len(split.test_rows),
                cutoff_created_at=split.cutoff_created_at,
                cutoff_index=split.cutoff_index,
                original_estimate=None,
                median_predictor=None,
                ml_predictor=None,
                insufficient_history_reason=reason,
            ),
            None,
        )

    actual = [row.actual_duration_minutes for row in split.test_rows]

    original_predicted = [float(row.planned_duration) for row in split.test_rows]
    original_metrics = _metrics_for(original_predicted, actual)

    median_predicted = _median_predictions_for_test_rows(eligible, split.cutoff_index, split.test_rows, thresholds)
    median_metrics = _metrics_for(median_predicted, actual)

    sample_counts_ok = (
        len(rows) >= gate_config.min_total_samples
        and len(split.train_rows) >= gate_config.min_train_samples
        and len(split.test_rows) >= gate_config.min_test_samples
    )

    ml_metrics = None
    pipeline = None
    insufficient_reason = None

    if not sample_counts_ok:
        insufficient_reason = (
            f"Only {len(rows)} total / {len(split.train_rows)} train / {len(split.test_rows)} test "
            f"completed+plausible observations are available; need at least {gate_config.min_total_samples} "
            f"total, {gate_config.min_train_samples} train, and {gate_config.min_test_samples} test to "
            "train and evaluate the ML model."
        )
    else:
        pipeline = train_ml_pipeline(split.train_rows, random_state=model_random_state)
        ml_predicted = predict_rows(pipeline, split.test_rows)
        ml_metrics = _metrics_for(ml_predicted, actual)

    comparison = PredictorComparison(
        generated_at=generated_at,
        train_sample_count=len(split.train_rows),
        test_sample_count=len(split.test_rows),
        cutoff_created_at=split.cutoff_created_at,
        cutoff_index=split.cutoff_index,
        original_estimate=original_metrics,
        median_predictor=median_metrics,
        ml_predictor=ml_metrics,
        insufficient_history_reason=insufficient_reason,
    )
    return comparison, pipeline


def decide_activation(
    comparison: PredictorComparison,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> MLActivationDecision:
    """
    ML is activated only when it was actually evaluated (comparison.ml_predictor
    is not None, i.e. MLGateConfig's sample thresholds were met) AND its
    held-out MAE beats the median predictor's held-out MAE on the same test
    rows. The reason always cites the concrete numbers behind the decision.
    """
    decided_at = clock().isoformat()

    if comparison.ml_predictor is None:
        reason = comparison.insufficient_history_reason or "ML predictor could not be evaluated."
        return MLActivationDecision(is_active=False, reason=reason, comparison=comparison, decided_at=decided_at)

    if comparison.median_predictor is None:
        return MLActivationDecision(
            is_active=False,
            reason="Median predictor could not be evaluated on the same test set, so ML cannot be compared fairly.",
            comparison=comparison,
            decided_at=decided_at,
        )

    ml_mae = comparison.ml_predictor.mae_minutes
    median_mae = comparison.median_predictor.mae_minutes

    if ml_mae < median_mae:
        reason = (
            f"ML held-out MAE ({ml_mae:g} min) is lower than the median predictor's held-out MAE "
            f"({median_mae:g} min) on {comparison.test_sample_count} held-out test rows: ML enabled."
        )
        return MLActivationDecision(is_active=True, reason=reason, comparison=comparison, decided_at=decided_at)

    reason = (
        f"ML held-out MAE ({ml_mae:g} min) did not beat the median predictor's held-out MAE "
        f"({median_mae:g} min) on {comparison.test_sample_count} held-out test rows: ML stays disabled."
    )
    return MLActivationDecision(is_active=False, reason=reason, comparison=comparison, decided_at=decided_at)


def run_comparison(
    observations: list[Observation],
    *,
    thresholds: ProductivityThresholds = ProductivityThresholds(),
    gate_config: MLGateConfig = MLGateConfig(),
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    model_random_state: int = 0,
) -> tuple[MLActivationDecision, Pipeline | None]:
    """
    Convenience wrapper combining evaluate_predictors + decide_activation.
    The returned pipeline is None unless the activation decision was actually
    ENABLED -- a rejected model is never handed back as if it were usable.
    """
    comparison, pipeline = evaluate_predictors(
        observations,
        thresholds=thresholds,
        gate_config=gate_config,
        clock=clock,
        model_random_state=model_random_state,
    )
    decision = decide_activation(comparison, clock=clock)
    if not decision.is_active:
        pipeline = None
    return decision, pipeline
