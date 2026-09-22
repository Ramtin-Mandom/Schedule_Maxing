"""
ml_persistence.py

Load/save the trained ML duration-prediction pipeline and its activation
decision as a local artifact, stored under config.settings.DATA_DIR next to
the execution database (config.settings.ML_MODEL_FILENAME /
ML_MODEL_META_FILENAME) -- no network, no telemetry, consistent with this
app's local-only execution-tracking storage.

Two files, not one blob: the .joblib file holds only the fitted sklearn
Pipeline; the .meta.json sidecar holds a small MLModelMetadata that can be
read and validated cheaply (no unpickling) before ever attempting to load
the model itself.

load_model_artifact never raises. A missing file, a corrupt pickle, or a
schema/feature-column mismatch are all treated as "no usable model" and
return None, so runtime prediction (app/productivity/ml_prediction.py) can
always fall back safely to the median predictor.
"""

from __future__ import annotations

from pathlib import Path

import joblib
import sklearn
from pydantic import BaseModel
from sklearn.pipeline import Pipeline

from app.productivity.ml_evaluation import MLActivationDecision
from app.productivity.ml_features import CATEGORICAL_COLUMNS, FEATURE_SCHEMA_VERSION, NUMERIC_COLUMNS
from config import settings


class MLModelMetadata(BaseModel):
    schema_version: str
    feature_columns: list[str]
    trained_at: str
    sklearn_version: str
    activation_decision: MLActivationDecision


def model_path(data_dir: Path | str | None = None) -> Path:
    return Path(data_dir or settings.DATA_DIR) / settings.ML_MODEL_FILENAME


def metadata_path(data_dir: Path | str | None = None) -> Path:
    return Path(data_dir or settings.DATA_DIR) / settings.ML_MODEL_META_FILENAME


def save_model_artifact(
    pipeline: Pipeline,
    decision: MLActivationDecision,
    *,
    trained_at: str,
    data_dir: Path | str | None = None,
) -> None:
    """Persist the fitted pipeline plus its metadata sidecar, overwriting any existing artifact."""
    target_dir = Path(data_dir or settings.DATA_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    metadata = MLModelMetadata(
        schema_version=FEATURE_SCHEMA_VERSION,
        feature_columns=NUMERIC_COLUMNS + CATEGORICAL_COLUMNS,
        trained_at=trained_at,
        sklearn_version=sklearn.__version__,
        activation_decision=decision,
    )

    joblib.dump(pipeline, model_path(target_dir))
    metadata_path(target_dir).write_text(metadata.model_dump_json(indent=2), encoding="utf-8")


def load_model_artifact(data_dir: Path | str | None = None) -> tuple[Pipeline, MLModelMetadata] | None:
    """
    Load the persisted pipeline + metadata, or None if the artifact is
    missing, stale (schema/feature-column mismatch), or otherwise unusable.
    Never raises.
    """
    target_dir = Path(data_dir or settings.DATA_DIR)
    meta_file = metadata_path(target_dir)

    try:
        if not meta_file.exists():
            return None
        metadata = MLModelMetadata.model_validate_json(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    if metadata.schema_version != FEATURE_SCHEMA_VERSION:
        return None
    if metadata.feature_columns != NUMERIC_COLUMNS + CATEGORICAL_COLUMNS:
        return None

    model_file = model_path(target_dir)
    try:
        if not model_file.exists():
            return None
        pipeline = joblib.load(model_file)
    except Exception:
        return None

    if not isinstance(pipeline, Pipeline):
        return None

    return pipeline, metadata
