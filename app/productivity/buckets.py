"""
buckets.py

Time-of-day and day-of-week bucketing for productivity analysis.

Design decisions (see app/productivity/data_prep.py for how these are used):

    - Time bucket is derived from a task's *planned* start time
      (minutes-from-midnight, as stored on every TaskExecution regardless of
      status), not from when work actually began. This keeps every
      execution -- including ones that were never started -- eligible for
      time-bucket analysis, and matches this app's own "schedule slot"
      framing (planned_start already exists on every record; an actual
      session start does not).

    - Day of week is derived from a real wall-clock ISO timestamp (in
      practice, an execution's created_at) via datetime.weekday(), mapped
      through a hardcoded English weekday-name tuple -- deliberately not
      datetime.strftime("%A"), which is locale-dependent and would make
      output (and tests) vary by machine/locale.

The four time buckets are a documented convention, not a claim of universal
truth:

    Night:     22:00-05:59 (wraps past midnight)
    Morning:   06:00-11:59
    Afternoon: 12:00-17:59
    Evening:   18:00-21:59
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

_MINUTES_PER_DAY = 24 * 60

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


class TimeBucket(str, Enum):
    NIGHT = "night"
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


def time_bucket_for_minutes(minutes: int) -> TimeBucket:
    """
    Map a minutes-from-midnight value (0-1440, matching this app's existing
    time model) to a TimeBucket. 1440 (end of day / midnight) is treated the
    same as 0.
    """
    minute_of_day = minutes % _MINUTES_PER_DAY

    if minute_of_day < 6 * 60:
        return TimeBucket.NIGHT
    if minute_of_day < 12 * 60:
        return TimeBucket.MORNING
    if minute_of_day < 18 * 60:
        return TimeBucket.AFTERNOON
    if minute_of_day < 22 * 60:
        return TimeBucket.EVENING
    return TimeBucket.NIGHT


def day_of_week_for_timestamp(iso_timestamp: str) -> str:
    """
    Return the English weekday name ("Monday".."Sunday") for a timezone-aware
    ISO 8601 timestamp, independent of the host machine's locale.
    """
    parsed = datetime.fromisoformat(iso_timestamp)
    return _WEEKDAY_NAMES[parsed.weekday()]
