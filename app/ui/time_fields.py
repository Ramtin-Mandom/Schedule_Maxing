"""
app/ui/time_fields.py

Typing and showing times, durations and dates in the desktop forms
(Milestone 4, Prompt 3). Tk-free (tests/ui/test_task_form_model.py).

Times are shown as "h:mm AM/PM" and are exact to the minute; nobody types
minutes-from-midnight. Accepted input: "10:13", "10:13 AM", "10:13pm",
"10 am", "22:13", "noon", "midnight". A time is a minute of the day (0..1439).
An *end* time may also be the following midnight (1440), shown as
"12:00 AM (next day)" and typed as "12:00 AM", "midnight" or "24:00" --
an interval can never end at the start of its own day. Arrow keys and the
mouse wheel step a time by one minute (15 with Shift), wrapping within the
day; stepping and typing produce the same values.

Durations are whole minutes from 1 to 24 hours: "13", "13 min", "1 h",
"1 h 13 min", "1h13m", "1:13". Dates are ISO "YYYY-MM-DD".

Nothing here rounds: an input that is not an exact minute is refused with a
message that says what to type instead.
"""

from __future__ import annotations

import re
from datetime import date

MINUTES_PER_DAY = 1440
MAX_DURATION_MINUTES = MINUTES_PER_DAY
NEXT_DAY_SUFFIX = " (next day)"


class FieldError(ValueError):
    """An input that cannot be read; the message says what to type instead."""


_CLOCK = re.compile(r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap])?\.?\s*m?\.?$")


def format_clock(minutes: int) -> str:
    """0 -> "12:00 AM", 613 -> "10:13 AM", 720 -> "12:00 PM", 1440 -> "12:00 AM (next day)"."""
    if not 0 <= minutes <= MINUTES_PER_DAY:
        raise ValueError(f"minutes must be within [0, {MINUTES_PER_DAY}]")
    suffix = NEXT_DAY_SUFFIX if minutes == MINUTES_PER_DAY else ""
    hour, minute = divmod(minutes % MINUTES_PER_DAY, 60)
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}{suffix}"


def parse_clock(text: str, *, end_of_interval: bool = False) -> int:
    """
    A typed time as a minute of the day. With end_of_interval, midnight
    ("12:00 AM", "midnight", "24:00") means the following midnight, 1440.
    """
    raw = (text or "").strip().lower()
    if raw.endswith(NEXT_DAY_SUFFIX.strip().lower()):
        raw = raw[: -len(NEXT_DAY_SUFFIX.strip())].strip()
    if not raw:
        raise FieldError("Enter a time, like 10:13 AM.")
    if raw == "noon":
        return 720
    if raw == "midnight":
        return MINUTES_PER_DAY if end_of_interval else 0
    compact = raw.replace(" ", "")
    if compact in ("24:00", "24"):
        if end_of_interval:
            return MINUTES_PER_DAY
        raise FieldError("A start time cannot be 24:00; use 12:00 AM for the start of the day.")
    match = _CLOCK.match(compact)
    if match is None:
        raise FieldError(f"“{text.strip()}” is not a time. Type it like 10:13 AM or 22:13.")
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    if minute > 59:
        raise FieldError("Minutes go from 00 to 59.")
    if match["meridiem"]:
        if not 1 <= hour <= 12:
            raise FieldError("With AM/PM, the hour goes from 1 to 12.")
        hour = hour % 12 + (12 if match["meridiem"] == "p" else 0)
    elif hour > 23:
        raise FieldError("The hour goes from 0 to 23 (or 1 to 12 with AM/PM).")
    minutes = hour * 60 + minute
    if end_of_interval and minutes == 0:
        return MINUTES_PER_DAY  # an interval ending at midnight ends at the following midnight
    return minutes


def step_clock(minutes: int, delta: int, *, end_of_interval: bool = False) -> int:
    """`minutes` moved by `delta`, wrapping around the day (1..1440 for an end time, 0..1439 otherwise)."""
    if end_of_interval:
        return (minutes - 1 + delta) % MINUTES_PER_DAY + 1
    return (minutes + delta) % MINUTES_PER_DAY


def format_duration(minutes: int) -> str:
    """13 -> "13 min", 60 -> "1 h", 73 -> "1 h 13 min"."""
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} h {rest} min"
    return f"{hours} h" if hours else f"{rest} min"


_DURATION_PARTS = re.compile(r"^(?:(?P<hours>\d+)\s*(?:h|hr|hrs|hour|hours))?\s*(?:(?P<minutes>\d+)\s*"
                             r"(?:m|min|mins|minute|minutes)?)?$")


def parse_duration(text: str) -> int:
    """A typed duration in whole minutes (1 minute to 24 hours)."""
    raw = (text or "").strip().lower()
    if not raw:
        raise FieldError("Enter how long it takes, like 45 min or 1 h 15 min.")
    if ":" in raw:
        hours_text, _, minutes_text = raw.partition(":")
        if not (hours_text.isdigit() and minutes_text.isdigit() and len(minutes_text) == 2):
            raise FieldError("Type a duration like 1:15 (hours:minutes), 75, or 1 h 15 min.")
        minutes = int(hours_text) * 60 + int(minutes_text)
        if int(minutes_text) > 59:
            raise FieldError("Minutes go from 00 to 59.")
    else:
        match = _DURATION_PARTS.match(raw)
        if match is None or not (match["hours"] or match["minutes"]):
            raise FieldError(f"“{text.strip()}” is not a duration. Type it like 45 min or 1 h 15 min.")
        minutes = int(match["hours"] or 0) * 60 + int(match["minutes"] or 0)
    if minutes < 1:
        raise FieldError("A task takes at least 1 minute.")
    if minutes > MAX_DURATION_MINUTES:
        raise FieldError("A task can take at most 24 h; split longer work into several tasks.")
    return minutes


def parse_date(text: str) -> date:
    raw = (text or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise FieldError(f"“{raw}” is not a date. Type it like 2026-09-23 (year-month-day).") from None


def format_date(value: date | None) -> str:
    return value.isoformat() if value is not None else ""
