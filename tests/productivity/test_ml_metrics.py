"""Tests for app/productivity/ml_metrics.py: metric calculation correctness."""

from __future__ import annotations

from app.productivity.ml_metrics import mean_absolute_error, median_absolute_error, within_tolerance_rate


def test_mean_absolute_error_hand_computed() -> None:
    predicted = [60.0, 50.0, 70.0]
    actual = [65.0, 50.0, 55.0]
    # errors: 5, 0, 15 -> mean 6.666...
    assert mean_absolute_error(predicted, actual) == 6.67


def test_median_absolute_error_hand_computed() -> None:
    predicted = [60.0, 50.0, 70.0, 90.0]
    actual = [65.0, 50.0, 55.0, 100.0]
    # errors: 5, 0, 15, 10 -> sorted [0, 5, 10, 15] -> median 7.5
    assert median_absolute_error(predicted, actual) == 7.5


def test_within_tolerance_rate_hand_computed() -> None:
    predicted = [60.0, 50.0, 70.0, 90.0]
    actual = [65.0, 50.0, 55.0, 100.0]
    # errors: 5, 0, 15, 10
    assert within_tolerance_rate(predicted, actual, 15.0) == 1.0  # all <= 15
    assert within_tolerance_rate(predicted, actual, 9.0) == 0.5  # only 5 and 0 qualify
    assert within_tolerance_rate(predicted, actual, 0.0) == 0.25  # only the exact match


def test_empty_input_returns_zero_for_every_metric() -> None:
    assert mean_absolute_error([], []) == 0.0
    assert median_absolute_error([], []) == 0.0
    assert within_tolerance_rate([], [], 15.0) == 0.0


def test_perfect_predictions_have_zero_error_and_full_tolerance_rate() -> None:
    predicted = [60.0, 45.0, 90.0]
    actual = [60.0, 45.0, 90.0]

    assert mean_absolute_error(predicted, actual) == 0.0
    assert median_absolute_error(predicted, actual) == 0.0
    assert within_tolerance_rate(predicted, actual, 0.0) == 1.0
