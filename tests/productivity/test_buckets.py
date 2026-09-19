"""Tests for app/productivity/buckets.py: time-bucket boundaries (including
the midnight wraparound) and locale-independent weekday naming."""

from __future__ import annotations

import pytest

from app.productivity.buckets import TimeBucket, day_of_week_for_timestamp, time_bucket_for_minutes


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, TimeBucket.NIGHT),
        (359, TimeBucket.NIGHT),
        (360, TimeBucket.MORNING),  # 06:00
        (719, TimeBucket.MORNING),
        (720, TimeBucket.AFTERNOON),  # 12:00 noon
        (1079, TimeBucket.AFTERNOON),
        (1080, TimeBucket.EVENING),  # 18:00
        (1319, TimeBucket.EVENING),
        (1320, TimeBucket.NIGHT),  # 22:00
        (1439, TimeBucket.NIGHT),
        (1440, TimeBucket.NIGHT),  # end of day wraps to the same bucket as 0
    ],
)
def test_time_bucket_boundaries(minutes: int, expected: TimeBucket) -> None:
    assert time_bucket_for_minutes(minutes) == expected


def test_day_of_week_matches_known_date() -> None:
    # 2024-01-01 is a Monday.
    assert day_of_week_for_timestamp("2024-01-01T09:00:00+00:00") == "Monday"
    assert day_of_week_for_timestamp("2024-01-07T09:00:00+00:00") == "Sunday"


def test_day_of_week_is_locale_independent_by_construction() -> None:
    # Uses datetime.weekday() through a hardcoded name tuple, not strftime("%A"),
    # so this does not depend on the process locale. Spot-check all 7 days.
    names = [day_of_week_for_timestamp(f"2024-01-0{day}T00:00:00+00:00") for day in range(1, 8)]
    assert names == [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
