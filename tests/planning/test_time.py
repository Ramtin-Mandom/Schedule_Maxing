"""Tests for app/planning/time.py: the aware-datetime / local-day-window time contract.

Covers midnight, noon, 23:59, 24:00, unsupported overnight windows, timezone
validation, and DST ambiguous/nonexistent/offset-transition policies.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.planning.time import (
    AmbiguousLocalTimeError,
    LocalDayWindow,
    UnsupportedSchedulingWindowError,
    elapsed_minutes,
    to_utc,
    validate_timezone,
)


# -----------------------------------------------------------------------------
# validate_timezone
# -----------------------------------------------------------------------------


def test_validate_timezone_accepts_known_iana_name():
    validate_timezone("America/New_York")  # does not raise


def test_validate_timezone_rejects_unknown_name():
    with pytest.raises(ValueError):
        validate_timezone("Not/AZone")


def test_validate_timezone_rejects_empty_string():
    with pytest.raises(ValueError):
        validate_timezone("")


# -----------------------------------------------------------------------------
# to_utc / elapsed_minutes
# -----------------------------------------------------------------------------


def test_to_utc_requires_aware_datetime():
    with pytest.raises(ValueError):
        to_utc(datetime(2024, 1, 1, 12, 0))


def test_to_utc_normalizes_offset_to_utc():
    eastern_noon = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    result = to_utc(eastern_noon)
    assert result.tzinfo == timezone.utc
    assert result == eastern_noon


def test_elapsed_minutes_computes_from_utc_instants():
    start = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 11, 30, tzinfo=timezone.utc)
    assert elapsed_minutes(start, end) == 90.0


def test_elapsed_minutes_rejects_naive_input():
    with pytest.raises(ValueError):
        elapsed_minutes(datetime(2024, 1, 1, 10, 0), datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc))


# -----------------------------------------------------------------------------
# LocalDayWindow: midnight / noon / 23:59 / 24:00 boundaries
# -----------------------------------------------------------------------------


def test_window_starting_at_midnight():
    window = LocalDayWindow(day=date(2024, 6, 1), tz_name="UTC", start_minute=0, end_minute=60)
    start, end = window.to_utc_instants()
    assert start == datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2024, 6, 1, 1, 0, tzinfo=timezone.utc)


def test_window_at_noon():
    window = LocalDayWindow(day=date(2024, 6, 1), tz_name="UTC", start_minute=720, end_minute=780)
    start, end = window.to_utc_instants()
    assert start == datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert end == datetime(2024, 6, 1, 13, 0, tzinfo=timezone.utc)


def test_window_ending_at_23_59():
    window = LocalDayWindow(day=date(2024, 6, 1), tz_name="UTC", start_minute=1439, end_minute=1439 + 1)
    start, end = window.to_utc_instants()
    assert start == datetime(2024, 6, 1, 23, 59, tzinfo=timezone.utc)
    assert end == datetime(2024, 6, 2, 0, 0, tzinfo=timezone.utc)


def test_window_ending_at_24_00_via_end_day_offset():
    """A same-day midnight endpoint is the following date at 00:00."""
    window = LocalDayWindow(
        day=date(2024, 6, 1), tz_name="UTC", start_minute=1380, end_minute=0, end_day_offset=1
    )
    start, end = window.to_utc_instants()
    assert start == datetime(2024, 6, 1, 23, 0, tzinfo=timezone.utc)
    assert end == datetime(2024, 6, 2, 0, 0, tzinfo=timezone.utc)


def test_end_minute_1440_same_day_is_rejected_directly():
    """end_minute must be expressed via end_day_offset=1, not a raw 1440 end
    combined with end_day_offset=0 (which would fail the ordering check the
    same as any other same-day case)."""
    with pytest.raises(ValueError):
        LocalDayWindow(day=date(2024, 6, 1), tz_name="UTC", start_minute=1440, end_minute=1440, end_day_offset=0)


def test_end_day_offset_1_requires_end_minute_zero():
    with pytest.raises(UnsupportedSchedulingWindowError):
        LocalDayWindow(day=date(2024, 6, 1), tz_name="UTC", start_minute=0, end_minute=30, end_day_offset=1)


def test_unsupported_multi_day_overnight_offset_rejected():
    with pytest.raises(UnsupportedSchedulingWindowError):
        LocalDayWindow(day=date(2024, 6, 1), tz_name="UTC", start_minute=0, end_minute=0, end_day_offset=2)


def test_end_before_start_same_day_rejected():
    with pytest.raises(ValueError):
        LocalDayWindow(day=date(2024, 6, 1), tz_name="UTC", start_minute=600, end_minute=300)


# -----------------------------------------------------------------------------
# DST: ambiguous, nonexistent, and offset-crossing windows
# -----------------------------------------------------------------------------


def test_ambiguous_local_time_is_rejected():
    """2024-11-03 01:30 America/New_York occurs twice (fall-back)."""
    window = LocalDayWindow(day=date(2024, 11, 3), tz_name="America/New_York", start_minute=90, end_minute=120)
    with pytest.raises(AmbiguousLocalTimeError):
        window.to_utc_instants()


def test_nonexistent_local_time_is_rejected():
    """2024-03-10 02:30 America/New_York never occurs (spring-forward gap)."""
    window = LocalDayWindow(day=date(2024, 3, 10), tz_name="America/New_York", start_minute=150, end_minute=180)
    with pytest.raises(AmbiguousLocalTimeError):
        window.to_utc_instants()


def test_window_crossing_dst_offset_transition_is_rejected():
    window = LocalDayWindow(day=date(2024, 3, 10), tz_name="America/New_York", start_minute=60, end_minute=240)
    with pytest.raises(UnsupportedSchedulingWindowError):
        window.to_utc_instants()


def test_window_on_ordinary_day_outside_dst_transition_succeeds():
    window = LocalDayWindow(day=date(2024, 6, 1), tz_name="America/New_York", start_minute=540, end_minute=600)
    start, end = window.to_utc_instants()
    assert start.tzinfo == timezone.utc
    assert end.tzinfo == timezone.utc
    assert (end - start).total_seconds() / 60 == 60
