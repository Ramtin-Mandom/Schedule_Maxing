"""
app/planning/compat.py

Helpers for the legacy schedule CSV format (app/planning/csv_import.py and
the schedule pages): abstract 1-based day indexes and minutes-from-midnight
converted to calendar dates and aware instants.

Legacy day-index contract:
    day 1 == anchor_date
    day N == anchor_date + (N - 1) days
An anchor_date is always required explicitly -- this module never infers a
calendar date for a legacy day index from today's date (see
app.planning.time and the milestone's time-handling rules).

Identity contract:
    Legacy CSV rows have no stable identity: an import mints a fresh UUID
    per row, so re-importing the same file produces a new set of IDs.
"""

from __future__ import annotations

from datetime import date as date_
from datetime import datetime, timedelta

from app.planning.time import LocalDayWindow, MINUTES_PER_DAY, UnsupportedSchedulingWindowError


# -----------------------------------------------------------------------------
# Legacy day-index <-> calendar date
# -----------------------------------------------------------------------------


def legacy_day_to_date(day: int, anchor_date: date_) -> date_:
    """day 1 == anchor_date, day N == anchor_date + (N - 1) days."""
    if day < 1:
        raise ValueError(f"legacy day index must be >= 1, got {day}")
    return anchor_date + timedelta(days=day - 1)


def _window_from_legacy_minutes(
    day: date_,
    tz_name: str,
    start_minute: int,
    end_minute: int,
) -> LocalDayWindow:
    """
    Legacy minutes-from-midnight (0-1440, where 1440 means "the following
    midnight") -> an explicit LocalDayWindow. A legacy end_time of exactly
    1440 and a same-day midnight both resolve to the identical instant
    (the following date at 00:00); anything past 1440 is not representable
    within one legacy day and is rejected.
    """
    if end_minute > MINUTES_PER_DAY:
        raise UnsupportedSchedulingWindowError(
            f"legacy end_time {end_minute} is beyond 24:00 (1440) and is not supported yet"
        )
    if end_minute == MINUTES_PER_DAY:
        return LocalDayWindow(day=day, tz_name=tz_name, start_minute=start_minute, end_minute=0, end_day_offset=1)
    return LocalDayWindow(day=day, tz_name=tz_name, start_minute=start_minute, end_minute=end_minute, end_day_offset=0)


def legacy_minutes_to_utc(
    day: date_,
    tz_name: str,
    start_minute: int,
    end_minute: int,
) -> tuple[datetime, datetime]:
    """
    Public form of the legacy minutes-from-midnight contract above: the aware
    UTC (start, end) instants of [start_minute, end_minute) on local `day` in
    `tz_name` (end_minute == 1440 means the following local midnight).
    """
    return _window_from_legacy_minutes(day, tz_name, start_minute, end_minute).to_utc_instants()

