"""
ml_metrics.py

Pure metric functions over parallel predicted/actual minute lists. Used
identically for all three predictors in ml_evaluation.py so the comparison
is apples-to-apples in one place rather than reimplemented per predictor.
"""

from __future__ import annotations

import statistics


def mean_absolute_error(predicted: list[float], actual: list[float]) -> float:
    """Mean of |predicted - actual|, in minutes. 0.0 for empty input."""
    if not predicted:
        return 0.0
    errors = [abs(p - a) for p, a in zip(predicted, actual)]
    return round(statistics.mean(errors), 2)


def median_absolute_error(predicted: list[float], actual: list[float]) -> float:
    """Median of |predicted - actual|, in minutes. 0.0 for empty input."""
    if not predicted:
        return 0.0
    errors = [abs(p - a) for p, a in zip(predicted, actual)]
    return round(statistics.median(errors), 2)


def within_tolerance_rate(predicted: list[float], actual: list[float], tolerance_minutes: float) -> float:
    """Fraction of predictions within `tolerance_minutes` of the actual value. 0.0 for empty input."""
    if not predicted:
        return 0.0
    errors = [abs(p - a) for p, a in zip(predicted, actual)]
    return round(sum(1 for error in errors if error <= tolerance_minutes) / len(errors), 4)
