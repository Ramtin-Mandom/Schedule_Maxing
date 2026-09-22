"""
Tests for app/productivity/ml_evaluation.py: the orchestrator and activation
gate. Gate-decision logic (decide_activation) is tested against directly
constructed PredictorComparison objects for full control over the numbers on
both sides of the gate; evaluate_predictors is tested end-to-end against
real fixtures to prove the pipeline actually runs, without asserting in
advance which predictor wins (that is a real, empirical outcome, not
something to script).
"""

from __future__ import annotations

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.productivity.ml_evaluation import (
    MLGateConfig,
    PredictorComparison,
    PredictorMetrics,
    decide_activation,
    evaluate_predictors,
)
from app.productivity.reporting import ProductivityService
from tests.productivity.fixtures import build_synthetic_dataset
from tests.productivity.ml_fixtures import build_interleaved_dataset


def _metrics(mae: float, *, sample_count: int = 10) -> PredictorMetrics:
    return PredictorMetrics(
        mae_minutes=mae,
        median_absolute_error_minutes=mae,
        within_15_min_rate=1.0,
        within_30_min_rate=1.0,
        sample_count=sample_count,
    )


def _comparison(*, ml_mae: float | None, median_mae: float = 10.0, insufficient: str | None = None) -> PredictorComparison:
    return PredictorComparison(
        generated_at="2024-01-01T00:00:00+00:00",
        train_sample_count=40,
        test_sample_count=10,
        cutoff_created_at="2024-01-10T00:00:00+00:00",
        cutoff_index=40,
        original_estimate=_metrics(15.0),
        median_predictor=_metrics(median_mae),
        ml_predictor=None if ml_mae is None else _metrics(ml_mae),
        insufficient_history_reason=insufficient,
    )


# -----------------------------
# decide_activation gate logic
# -----------------------------


def test_decide_activation_enables_ml_when_it_beats_the_median() -> None:
    comparison = _comparison(ml_mae=5.0, median_mae=10.0)
    decision = decide_activation(comparison)

    assert decision.is_active is True
    assert "5" in decision.reason
    assert "10" in decision.reason


def test_decide_activation_rejects_ml_when_it_does_not_beat_the_median() -> None:
    comparison = _comparison(ml_mae=12.0, median_mae=10.0)
    decision = decide_activation(comparison)

    assert decision.is_active is False


def test_decide_activation_rejects_ml_on_a_tie() -> None:
    comparison = _comparison(ml_mae=10.0, median_mae=10.0)
    decision = decide_activation(comparison)

    assert decision.is_active is False  # strictly lower MAE is required, not a tie


def test_decide_activation_rejects_when_ml_was_not_evaluated() -> None:
    comparison = _comparison(ml_mae=None, insufficient="not enough history")
    decision = decide_activation(comparison)

    assert decision.is_active is False
    assert decision.reason == "not enough history"


def test_decide_activation_result_carries_the_comparison_it_decided_from() -> None:
    comparison = _comparison(ml_mae=5.0, median_mae=10.0)
    decision = decide_activation(comparison)

    assert decision.comparison is comparison


# -----------------------------
# evaluate_predictors end to end
# -----------------------------


def test_insufficient_history_on_the_stock_fixture() -> None:
    """The repo's real, unmodified stock fixture (19 completed+plausible rows)
    does not meet the default, conservative MLGateConfig thresholds -- this
    is the correct, honest outcome on real (if small) history, not a bug."""
    connection = get_connection(":memory:")
    try:
        repository = ExecutionRepository(connection)
        build_synthetic_dataset(repository)
        observations = ProductivityService(repository).build_observations(period="all_time")

        comparison, pipeline = evaluate_predictors(observations)
    finally:
        connection.close()

    assert comparison.ml_predictor is None
    assert comparison.insufficient_history_reason is not None
    assert pipeline is None

    decision = decide_activation(comparison)
    assert decision.is_active is False


def test_evaluate_predictors_trains_and_evaluates_ml_when_gate_thresholds_are_met() -> None:
    """With enough interleaved history, the ML arm actually trains and
    produces real metrics -- whether or not it happens to beat the median
    predictor is a separate, empirical question decided_activation answers;
    this test only proves the pipeline runs end to end without error."""
    connection = get_connection(":memory:")
    try:
        repository = ExecutionRepository(connection)
        build_interleaved_dataset(repository, count=60)
        observations = ProductivityService(repository).build_observations(period="all_time")

        comparison, pipeline = evaluate_predictors(observations, gate_config=MLGateConfig())
    finally:
        connection.close()

    assert comparison.ml_predictor is not None
    assert comparison.insufficient_history_reason is None
    assert pipeline is not None
    assert comparison.train_sample_count + comparison.test_sample_count == 60
    assert comparison.ml_predictor.mae_minutes >= 0.0
    assert comparison.median_predictor is not None
    assert comparison.original_estimate is not None

    # The activation decision must not raise regardless of which side wins.
    decision = decide_activation(comparison)
    assert decision.is_active in (True, False)
    assert decision.reason


def test_evaluate_predictors_empty_history_reports_insufficient_without_crashing() -> None:
    comparison, pipeline = evaluate_predictors([])

    assert comparison.ml_predictor is None
    assert comparison.median_predictor is None
    assert comparison.original_estimate is None
    assert comparison.insufficient_history_reason is not None
    assert pipeline is None
