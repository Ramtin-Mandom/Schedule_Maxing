"""
Tests for app/productivity/ml_prediction.py: runtime ML prediction and its
fallback contract. predict_duration_with_ml must never raise -- it returns
None whenever ML isn't usable (no artifact, inactive, unknown-input
failure), which every caller must treat as "use the median predictor".
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.productivity.buckets import TimeBucket
from app.productivity.ml_evaluation import MLActivationDecision, MLGateConfig, evaluate_predictors
from app.productivity.ml_persistence import save_model_artifact
from app.productivity.ml_prediction import predict_duration_with_ml
from app.productivity.reporting import ProductivityService
from tests.productivity.ml_fixtures import build_interleaved_dataset


def _train_and_save(tmp_path, *, is_active: bool):
    connection = get_connection(":memory:")
    try:
        repository = ExecutionRepository(connection)
        build_interleaved_dataset(repository, count=60)
        observations = ProductivityService(repository).build_observations(period="all_time")
        comparison, pipeline = evaluate_predictors(observations, gate_config=MLGateConfig())
    finally:
        connection.close()

    assert pipeline is not None  # sanity: this fixture must clear the training gate

    decision = MLActivationDecision(
        is_active=is_active,
        reason="test-forced decision",
        comparison=comparison,
        decided_at=datetime.now(timezone.utc).isoformat(),
    )
    save_model_artifact(pipeline, decision, trained_at=datetime.now(timezone.utc).isoformat(), data_dir=tmp_path)
    return observations


def test_returns_none_when_no_model_artifact_exists(tmp_path) -> None:
    result = predict_duration_with_ml(
        [],
        category="study",
        tag="math",
        priority=5,
        time_bucket=TimeBucket.MORNING,
        day_of_week="Monday",
        planned_duration=60,
        planned_start=540,
        data_dir=str(tmp_path),
    )
    assert result is None


def test_returns_none_when_ml_is_not_active(tmp_path) -> None:
    observations = _train_and_save(tmp_path, is_active=False)

    result = predict_duration_with_ml(
        observations,
        category="study",
        tag="math",
        priority=5,
        time_bucket=TimeBucket.MORNING,
        day_of_week="Monday",
        planned_duration=60,
        planned_start=540,
        data_dir=str(tmp_path),
    )
    assert result is None


def test_returns_a_prediction_when_ml_is_active(tmp_path) -> None:
    observations = _train_and_save(tmp_path, is_active=True)

    result = predict_duration_with_ml(
        observations,
        category="study",
        tag="math",
        priority=5,
        time_bucket=TimeBucket.MORNING,
        day_of_week="Monday",
        planned_duration=60,
        planned_start=540,
        data_dir=str(tmp_path),
    )

    assert result is not None
    assert isinstance(result.predicted_duration_minutes, float)
    assert result.sample_count > 0
    assert result.explanation


def test_unknown_category_at_predict_time_does_not_raise(tmp_path) -> None:
    observations = _train_and_save(tmp_path, is_active=True)

    result = predict_duration_with_ml(
        observations,
        category="a-category-never-seen-in-training",
        tag="a-tag-never-seen-in-training",
        priority=5,
        time_bucket=TimeBucket.NIGHT,
        day_of_week="Sunday",
        planned_duration=60,
        planned_start=0,
        data_dir=str(tmp_path),
    )

    # handle_unknown="ignore" means this must not raise and must still
    # produce a numeric result, not silently swallow into None.
    assert result is not None
    assert isinstance(result.predicted_duration_minutes, float)


def test_returns_none_on_unexpected_exception_rather_than_raising(tmp_path, monkeypatch) -> None:
    observations = _train_and_save(tmp_path, is_active=True)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("app.productivity.ml_prediction.load_model_artifact", _boom)

    result = predict_duration_with_ml(
        observations,
        category="study",
        tag="math",
        priority=5,
        time_bucket=TimeBucket.MORNING,
        day_of_week="Monday",
        planned_duration=60,
        planned_start=540,
        data_dir=str(tmp_path),
    )
    assert result is None
