"""
Tests for app/productivity/ml_persistence.py: runtime fallback safety.
load_model_artifact must never raise -- a missing file, a corrupt pickle, or
a stale/mismatched schema all return None so callers can always fall back to
the median predictor.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.productivity.ml_evaluation import MLActivationDecision
from app.productivity.ml_features import CATEGORICAL_COLUMNS, FEATURE_SCHEMA_VERSION, NUMERIC_COLUMNS, build_feature_rows
from app.productivity.ml_model import build_ml_pipeline, train_ml_pipeline
from app.productivity.ml_persistence import MLModelMetadata, load_model_artifact, metadata_path, model_path, save_model_artifact
from app.productivity.reporting import ProductivityService
from tests.productivity.ml_fixtures import build_interleaved_dataset


def _fake_decision(is_active: bool = True) -> MLActivationDecision:
    return MLActivationDecision(
        is_active=is_active,
        reason="test reason",
        comparison=None,
        decided_at=datetime.now(timezone.utc).isoformat(),
    )


def _trained_pipeline():
    connection = get_connection(":memory:")
    try:
        repository = ExecutionRepository(connection)
        build_interleaved_dataset(repository, count=15)
        observations = ProductivityService(repository).build_observations(period="all_time")
        rows = build_feature_rows(observations)
    finally:
        connection.close()
    return train_ml_pipeline(rows, random_state=0)


def test_load_returns_none_when_no_artifact_exists(tmp_path) -> None:
    assert load_model_artifact(tmp_path) is None


def test_save_then_load_round_trips_successfully(tmp_path) -> None:
    pipeline = _trained_pipeline()
    decision = _fake_decision(is_active=True)

    save_model_artifact(pipeline, decision, trained_at="2024-01-01T00:00:00+00:00", data_dir=tmp_path)
    loaded = load_model_artifact(tmp_path)

    assert loaded is not None
    loaded_pipeline, metadata = loaded
    assert metadata.schema_version == FEATURE_SCHEMA_VERSION
    assert metadata.feature_columns == NUMERIC_COLUMNS + CATEGORICAL_COLUMNS
    assert metadata.activation_decision.is_active is True
    assert loaded_pipeline is not None


def test_load_returns_none_for_corrupt_joblib_file(tmp_path) -> None:
    pipeline = _trained_pipeline()
    decision = _fake_decision(is_active=True)
    save_model_artifact(pipeline, decision, trained_at="2024-01-01T00:00:00+00:00", data_dir=tmp_path)

    # Corrupt the model file after a valid metadata sidecar was written.
    model_path(tmp_path).write_bytes(b"not a valid joblib pickle")

    assert load_model_artifact(tmp_path) is None


def test_load_returns_none_for_mismatched_schema_version(tmp_path) -> None:
    pipeline = _trained_pipeline()
    decision = _fake_decision(is_active=True)
    save_model_artifact(pipeline, decision, trained_at="2024-01-01T00:00:00+00:00", data_dir=tmp_path)

    stale_metadata = MLModelMetadata(
        schema_version="not-the-real-version",
        feature_columns=NUMERIC_COLUMNS + CATEGORICAL_COLUMNS,
        trained_at="2024-01-01T00:00:00+00:00",
        sklearn_version="0.0.0",
        activation_decision=decision,
    )
    metadata_path(tmp_path).write_text(stale_metadata.model_dump_json(indent=2), encoding="utf-8")

    assert load_model_artifact(tmp_path) is None


def test_load_returns_none_for_mismatched_feature_columns(tmp_path) -> None:
    pipeline = _trained_pipeline()
    decision = _fake_decision(is_active=True)
    save_model_artifact(pipeline, decision, trained_at="2024-01-01T00:00:00+00:00", data_dir=tmp_path)

    stale_metadata = MLModelMetadata(
        schema_version=FEATURE_SCHEMA_VERSION,
        feature_columns=["some", "different", "columns"],
        trained_at="2024-01-01T00:00:00+00:00",
        sklearn_version="0.0.0",
        activation_decision=decision,
    )
    metadata_path(tmp_path).write_text(stale_metadata.model_dump_json(indent=2), encoding="utf-8")

    assert load_model_artifact(tmp_path) is None


def test_load_returns_none_for_malformed_metadata_json(tmp_path) -> None:
    pipeline = _trained_pipeline()
    decision = _fake_decision(is_active=True)
    save_model_artifact(pipeline, decision, trained_at="2024-01-01T00:00:00+00:00", data_dir=tmp_path)

    metadata_path(tmp_path).write_text("{ not valid json", encoding="utf-8")

    assert load_model_artifact(tmp_path) is None


def test_load_returns_none_when_model_file_missing_but_metadata_present(tmp_path) -> None:
    pipeline = _trained_pipeline()
    decision = _fake_decision(is_active=True)
    save_model_artifact(pipeline, decision, trained_at="2024-01-01T00:00:00+00:00", data_dir=tmp_path)

    model_path(tmp_path).unlink()

    assert load_model_artifact(tmp_path) is None


def test_build_ml_pipeline_is_not_a_saved_artifact_until_persisted(tmp_path) -> None:
    build_ml_pipeline()  # constructing a fresh pipeline must not write anything
    assert load_model_artifact(tmp_path) is None
