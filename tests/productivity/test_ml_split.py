"""Tests for app/productivity/ml_split.py: chronological (never random) train/test splitting."""

from __future__ import annotations

import random

import pytest

from app.productivity.ml_features import MLFeatureRow
from app.productivity.ml_split import chronological_split


def _row(execution_id: str, created_at: str) -> MLFeatureRow:
    return MLFeatureRow(
        execution_id=execution_id,
        created_at=created_at,
        planned_duration=60,
        priority=5,
        planned_start=540,
        category="study",
        tag="math",
        time_bucket="morning",
        day_of_week="Monday",
        category_median_duration_so_far=None,
        category_sample_count_so_far=0,
        category_time_bucket_median_duration_so_far=None,
        category_time_bucket_sample_count_so_far=0,
        global_median_duration_so_far=None,
        global_sample_count_so_far=0,
        actual_duration_minutes=60.0,
    )


def _ordered_rows(count: int) -> list[MLFeatureRow]:
    return [_row(str(i), f"2024-01-{i + 1:02d}T09:00:00+00:00") for i in range(count)]


def test_split_sizes_and_cutoff_index() -> None:
    rows = _ordered_rows(10)
    split = chronological_split(rows, test_fraction=0.2)

    assert split.cutoff_index == 8
    assert len(split.train_rows) == 8
    assert len(split.test_rows) == 2


def test_all_test_rows_are_chronologically_at_or_after_all_train_rows() -> None:
    rows = _ordered_rows(10)
    split = chronological_split(rows, test_fraction=0.3)

    latest_train = max(row.created_at for row in split.train_rows)
    earliest_test = min(row.created_at for row in split.test_rows)
    assert latest_train <= earliest_test


def test_split_never_shuffles_even_with_scrambled_input_order() -> None:
    rows = _ordered_rows(10)
    scrambled = rows[:]
    random.Random(3).shuffle(scrambled)

    split = chronological_split(scrambled, test_fraction=0.2)

    assert [row.execution_id for row in split.train_rows] == [row.execution_id for row in rows[:8]]
    assert [row.execution_id for row in split.test_rows] == [row.execution_id for row in rows[8:]]


def test_cutoff_created_at_matches_first_test_row() -> None:
    rows = _ordered_rows(5)
    split = chronological_split(rows, test_fraction=0.2)

    assert split.cutoff_created_at == split.test_rows[0].created_at


def test_empty_input_returns_empty_split() -> None:
    split = chronological_split([], test_fraction=0.2)

    assert split.train_rows == []
    assert split.test_rows == []
    assert split.cutoff_index == 0
    assert split.cutoff_created_at == ""


def test_invalid_test_fraction_raises() -> None:
    rows = _ordered_rows(5)
    with pytest.raises(ValueError):
        chronological_split(rows, test_fraction=0.0)
    with pytest.raises(ValueError):
        chronological_split(rows, test_fraction=1.0)
    with pytest.raises(ValueError):
        chronological_split(rows, test_fraction=-0.1)
