"""
ml_split.py

Chronological train/test splitting for the ML duration-prediction
experiment. Every evaluation path in ml_evaluation.py (the median-predictor
comparison arm and the ML arm) routes through chronological_split, so "never
a random split" is structural rather than a convention a future call site
could forget.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.productivity.ml_features import MLFeatureRow


class ChronoSplit(BaseModel):
    train_rows: list[MLFeatureRow]
    test_rows: list[MLFeatureRow]
    cutoff_created_at: str
    cutoff_index: int


def chronological_split(rows: list[MLFeatureRow], *, test_fraction: float = 0.2) -> ChronoSplit:
    """
    Sort `rows` by (created_at, execution_id) and slice the most recent
    `test_fraction` of them off as the held-out test set.

    Never shuffles: both train_rows and test_rows preserve chronological
    order, and every row in test_rows is chronologically at or after every
    row in train_rows.
    """
    if not 0.0 < test_fraction < 1.0:
        raise ValueError(f"test_fraction must be between 0 and 1 (exclusive), got {test_fraction}.")

    ordered = sorted(rows, key=lambda row: (row.created_at, row.execution_id))
    total = len(ordered)

    if total == 0:
        return ChronoSplit(train_rows=[], test_rows=[], cutoff_created_at="", cutoff_index=0)

    test_size = round(total * test_fraction)
    cutoff_index = total - test_size

    train_rows = ordered[:cutoff_index]
    test_rows = ordered[cutoff_index:]
    cutoff_created_at = test_rows[0].created_at if test_rows else ordered[-1].created_at

    return ChronoSplit(
        train_rows=train_rows,
        test_rows=test_rows,
        cutoff_created_at=cutoff_created_at,
        cutoff_index=cutoff_index,
    )
