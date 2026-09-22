"""Tests for app/productivity/ml_model.py: pipeline construction and deterministic training."""

from __future__ import annotations

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.productivity.ml_features import build_feature_rows
from app.productivity.ml_model import predict_rows, train_ml_pipeline
from app.productivity.ml_split import chronological_split
from app.productivity.reporting import ProductivityService
from tests.productivity.ml_fixtures import build_interleaved_dataset


def _rows():
    connection = get_connection(":memory:")
    try:
        repository = ExecutionRepository(connection)
        build_interleaved_dataset(repository, count=30)
        observations = ProductivityService(repository).build_observations(period="all_time")
        return build_feature_rows(observations)
    finally:
        connection.close()


def test_training_the_same_rows_twice_with_the_same_seed_is_deterministic() -> None:
    rows = _rows()
    split = chronological_split(rows, test_fraction=0.2)

    pipeline_a = train_ml_pipeline(split.train_rows, random_state=0)
    pipeline_b = train_ml_pipeline(split.train_rows, random_state=0)

    predictions_a = predict_rows(pipeline_a, split.test_rows)
    predictions_b = predict_rows(pipeline_b, split.test_rows)

    assert predictions_a == predictions_b


def test_predict_rows_on_empty_list_returns_empty_list() -> None:
    rows = _rows()
    split = chronological_split(rows, test_fraction=0.2)
    pipeline = train_ml_pipeline(split.train_rows, random_state=0)

    assert predict_rows(pipeline, []) == []


def test_unknown_category_at_predict_time_does_not_raise() -> None:
    rows = _rows()
    split = chronological_split(rows, test_fraction=0.2)
    pipeline = train_ml_pipeline(split.train_rows, random_state=0)

    unseen = split.test_rows[0].model_copy(update={"category": "brand-new-category", "tag": "brand-new-tag"})

    predictions = predict_rows(pipeline, [unseen])

    assert len(predictions) == 1
    assert isinstance(predictions[0], float)
