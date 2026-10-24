"""Tests for app/planning/compat.py: the legacy CSV day-index helpers."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.planning.compat import legacy_day_to_date

ANCHOR = date(2026, 1, 5)
TZ = "America/New_York"


# -----------------------------------------------------------------------------
# Legacy day-index <-> calendar date
# -----------------------------------------------------------------------------


def test_day_1_is_anchor_date():
    assert legacy_day_to_date(1, ANCHOR) == ANCHOR


def test_day_n_is_anchor_plus_n_minus_1():
    assert legacy_day_to_date(5, ANCHOR) == ANCHOR + timedelta(days=4)


def test_day_0_is_rejected():
    with pytest.raises(ValueError):
        legacy_day_to_date(0, ANCHOR)
