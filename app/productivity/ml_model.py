"""
ml_model.py

Defines and fits the scikit-learn pipeline used by the ML duration
predictor. Preprocessing (imputation, scaling, one-hot encoding) lives
entirely inside the sklearn Pipeline/ColumnTransformer -- no encoder or
imputer is ever fit outside of it anywhere in this codebase, and
OneHotEncoder(handle_unknown="ignore") means a category never seen during
training is encoded as all-zero columns at predict time instead of raising.

Model choice: Ridge regression. The realistic data volume for this
single-user app is tens to low hundreds of completed tasks (the stock test
fixture has 19), where a small linear model is far less prone to
overfitting one-hot-encoded categorical features than a tree ensemble
would be, and is fully deterministic given a fixed random_state. If real
history grows into the hundreds/thousands of rows,
RandomForestRegressor(n_estimators=100, random_state=0, max_depth=4) is a
reasonable thing to try next -- not the default here.
"""

from __future__ import annotations

import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.productivity.ml_features import CATEGORICAL_COLUMNS, MLFeatureRow, NUMERIC_COLUMNS


def build_ml_pipeline(*, random_state: int = 0) -> Pipeline:
    """Construct (but do not fit) the full preprocessing + regression pipeline."""
    preprocessor = ColumnTransformer(
        transformers=[
            (
                "numeric",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="median")),
                        ("scale", StandardScaler()),
                    ]
                ),
                NUMERIC_COLUMNS,
            ),
            (
                "categorical",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="most_frequent")),
                        ("encode", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
                    ]
                ),
                CATEGORICAL_COLUMNS,
            ),
        ]
    )
    return Pipeline(
        [
            ("preprocess", preprocessor),
            ("regressor", Ridge(alpha=1.0, random_state=random_state)),
        ]
    )


def rows_to_frame(rows: list[MLFeatureRow]) -> pd.DataFrame:
    """Feature-only DataFrame (no target column), in the pipeline's expected column order."""
    records = [row.model_dump() for row in rows]
    frame = pd.DataFrame(records)
    for column in NUMERIC_COLUMNS + CATEGORICAL_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame[NUMERIC_COLUMNS + CATEGORICAL_COLUMNS]


def train_ml_pipeline(train_rows: list[MLFeatureRow], *, random_state: int = 0) -> Pipeline:
    """Fit a fresh pipeline on `train_rows`. Deterministic given the same rows and random_state."""
    pipeline = build_ml_pipeline(random_state=random_state)
    features = rows_to_frame(train_rows)
    target = pd.Series([row.actual_duration_minutes for row in train_rows])
    pipeline.fit(features, target)
    return pipeline


def predict_rows(pipeline: Pipeline, rows: list[MLFeatureRow]) -> list[float]:
    """Predict actual durations for `rows` using an already-fitted pipeline."""
    if not rows:
        return []
    features = rows_to_frame(rows)
    return [float(value) for value in pipeline.predict(features)]
