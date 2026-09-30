"""
app/ui/time_fields.py

Typing and showing times, durations and dates in the desktop forms
(Milestone 4, Prompt 3). Tk-free (tests/ui/test_task_form_model.py).

Times are shown as "h:mm AM/PM" and are exact to the minute; nobody types
minutes-from-midnight. Every form types a time in the shared
[ Hour ] : [ Minute ] [ AM/PM ] input (app/ui/clock_input.py), whose parts
are converted by clock_to_minutes / minutes_to_clock / parse_clock_parts
below. The forms carry the result as "h:mm AM/PM" text, read back by
parse_clock, which also accepts "10:13", "10:13pm", "10 am", "22:13",
"noon" and "midnight" (e.g. from an import). A time is a minute of the day
(0..1439). An *end* time may also be the following midnight (1440), shown as
"12:00 AM (next day)" and typed as "12:00 AM", "midnight" or "24:00" --
an interval can never end at the start of its own day.

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


# -----------------------------------------------------------------------------
# [ Hour ] : [ Minute ] [ AM/PM ] -- the parts of the shared time input
# (app/ui/clock_input.py). Every clock time the desktop asks for is typed as
# an hour 1-12, a minute 00-59 and an AM/PM toggle; these functions are the
# one conversion to and from the engine's minutes from local midnight.
# -----------------------------------------------------------------------------

AM, PM = "AM", "PM"
MERIDIEMS = (AM, PM)


def toggle_meridiem(meridiem: str) -> str:
    """AM -> PM, PM -> AM."""
    return PM if meridiem == AM else AM


def clock_to_minutes(hour: int, minute: int, meridiem: str, *, end_of_interval: bool = False) -> int:
    """
    12-hour clock parts as minutes from midnight: 12:00 AM -> 0, 12:30 AM ->
    30, 1:00 AM -> 60, 12:00 PM -> 720, 1:15 PM -> 795, 11:59 PM -> 1439.
    With end_of_interval, 12:00 AM is the following midnight (1440).
    """
    if meridiem not in MERIDIEMS:
        raise FieldError("Choose AM or PM.")
    if not 1 <= hour <= 12:
        raise FieldError("The hour goes from 1 to 12.")
    if not 0 <= minute <= 59:
        raise FieldError("Minutes go from 00 to 59.")
    minutes = (hour % 12 + (12 if meridiem == PM else 0)) * 60 + minute
    if end_of_interval and minutes == 0:
        return MINUTES_PER_DAY
    return minutes


def minutes_to_clock(minutes: int) -> tuple[int, int, str]:
    """Minutes from midnight (0..1440) as (hour 1-12, minute, AM/PM); 1440 is 12:00 AM of the next day."""
    if not 0 <= minutes <= MINUTES_PER_DAY:
        raise ValueError(f"minutes must be within [0, {MINUTES_PER_DAY}]")
    hour, minute = divmod(minutes % MINUTES_PER_DAY, 60)
    return (hour % 12) or 12, minute, AM if hour < 12 else PM


def parse_clock_parts(hour_text: str, minute_text: str, meridiem: str, *, end_of_interval: bool = False) -> int:
    """
    The typed parts of the time input as minutes from midnight. An empty
    minute means :00; an empty hour is refused. Raises FieldError with what
    to type instead -- never rounds.
    """
    hour_raw, minute_raw = (hour_text or "").strip(), (minute_text or "").strip()
    if not hour_raw:
        raise FieldError("Enter the hour (1 to 12)." if minute_raw else "Enter a time, like 10:13 AM.")
    if not hour_raw.isdigit():
        raise FieldError("The hour is a number from 1 to 12.")
    if minute_raw and not minute_raw.isdigit():
        raise FieldError("The minutes are a number from 00 to 59.")
    if len(minute_raw) > 2:
        raise FieldError("Minutes go from 00 to 59.")
    return clock_to_minutes(int(hour_raw), int(minute_raw or 0), meridiem, end_of_interval=end_of_interval)


def clock_parts_text(hour_text: str, minute_text: str, meridiem: str, *, end_of_interval: bool = False) -> str:
    """
    The parts as the text the forms carry: "" when nothing was typed, the
    canonical "h:mm AM/PM" when they are valid, else the raw parts (which
    parse_clock then refuses with its own message; nothing is guessed).
    """
    hour_raw, minute_raw = (hour_text or "").strip(), (minute_text or "").strip()
    if not hour_raw and not minute_raw:
        return ""
    try:
        return format_clock(parse_clock_parts(hour_raw, minute_raw, meridiem, end_of_interval=end_of_interval))
    except FieldError:
        return f"{hour_raw}:{minute_raw.zfill(2) if minute_raw.isdigit() else minute_raw} {meridiem}"


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
