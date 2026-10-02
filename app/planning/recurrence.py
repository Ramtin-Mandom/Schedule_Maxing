"""
app/planning/recurrence.py

The calendar of a recurring series (docs/recurrence.md): which local dates a
configured RecurrenceSpec produces, each occurrence's deterministic identity,
and the cadence rules recurring-to-recurring dependencies need. Pure and
storage-free -- app/planning/series.py materializes, edits and deletes the
concrete occurrence tasks; nothing here reads or writes records.

A series is *configured* when its rule names an explicit local start date
(the anchor) and an IANA time zone. Slots are local calendar dates in that
zone; a device's own time zone, where it travels and the machine clock never
change them, and no anchor is ever derived from a requested range.

Calendar semantics (a "slot" is one original recurrence date):

    daily    every `interval` days from the anchor: anchor + k * interval.
    weekly   Monday-anchored weeks, every `interval` weeks counted from the
             anchor's week; in each such week the selected weekdays (or, when
             none are selected, the anchor's weekday).
    monthly  every `interval` months counted from the anchor's month, on the
             explicit day of month (or the anchor's day). A month without that
             day (the 31st in April, February 29th in a common year) has no
             slot: nonexistent days are skipped, never clamped.

    - No slot precedes the anchor (e.g. weekdays of the anchor week before it).
    - end_date is inclusive. An end date before the anchor is a retired series
      segment with no slots (what a "this and every later occurrence" split
      leaves of a segment cut at its own start).
    - count counts valid slots from the anchor, independently of any requested
      range; a skipped, deleted or moved occurrence still consumes its slot.
      count and end_date stay mutually exclusive (RecurrenceSpec).

Bounded work: slots are found by arithmetic seeking (a slot index or the first
slot on/after a date is computed, never scanned day by day from a decades-old
anchor) and every enumeration is capped by MAX_SLOT_SCAN loop steps.

Identity: occurrence_task_id(series_id, slot) is a uuid5 of the series'
stable id and the slot's original local date only -- never of where an
occurrence is currently placed, of the series' mutable definition version, or
of the device -- so two devices expanding the same rule mint the same ids.
"""

from __future__ import annotations

import calendar
import functools
import math
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.planning.models import RecurrenceFrequency, RecurrenceSpec

#: The namespace of occurrence ids: uuid5(OCCURRENCE_NAMESPACE, "<series id>/<slot ISO date>").
OCCURRENCE_NAMESPACE = uuid.UUID("7f3c1a52-4be0-4b1e-9d0c-8a7e0f3d5b21")

#: The most loop steps one slot enumeration or index computation may take.
MAX_SLOT_SCAN = 20_000

#: The Gregorian calendar repeats its month lengths every 400 years.
_MONTHS_PER_CYCLE = 4800


class RecurrenceError(ValueError):
    """A series rule cannot be used as asked (not configured, or a request beyond the work budget)."""


class SeriesNotConfiguredError(RecurrenceError):
    """The series has no explicit start date and time zone: it needs configuration before it can repeat."""


class RecurrenceBudgetError(RecurrenceError):
    """Computing the requested slots would exceed the bounded work budget."""


def occurrence_task_id(series_id: uuid.UUID, slot: date_) -> uuid.UUID:
    """The deterministic id of the occurrence of `series_id` at its original local slot date."""
    return uuid.uuid5(OCCURRENCE_NAMESPACE, f"{series_id}/{slot.isoformat()}")


def _monday(day: date_) -> date_:
    return day - timedelta(days=day.weekday())


def _month_number(day: date_) -> int:
    return day.year * 12 + day.month - 1


def _month_date(month_number: int, day: int) -> date_ | None:
    year, month = divmod(month_number, 12)
    month += 1
    if not 1 <= year <= 9999 or day > calendar.monthrange(year, month)[1]:
        return None
    return date_(year, month, day)


def _days_in(month_number: int) -> int:
    year, month = divmod(month_number, 12)
    return calendar.monthrange(year, month + 1)[1]


@functools.lru_cache(maxsize=256)
def _valid_month_prefix(first_month_mod: int, interval: int, day: int) -> tuple[int, ...]:
    """
    prefix[k] = how many of the cadence months first, first+interval, ...
    (k of them) have a day `day`, over one full period of the Gregorian
    month-length cycle. Months are counted modulo 4800 (400 years), so the
    pattern -- and this table -- repeats exactly every len(prefix) - 1 steps.
    """
    period = _MONTHS_PER_CYCLE // math.gcd(interval, _MONTHS_PER_CYCLE)
    prefix = [0]
    # Year 2000 starts a 400-year cycle, so month numbers offset from it have the right lengths.
    base = 2000 * 12
    for step in range(period):
        month = base + (first_month_mod + step * interval) % _MONTHS_PER_CYCLE
        prefix.append(prefix[-1] + (1 if day <= _days_in(month) else 0))
    return tuple(prefix)


@dataclass(frozen=True)
class SeriesRule:
    """A configured RecurrenceSpec with its anchor-derived defaults resolved."""

    frequency: RecurrenceFrequency
    interval: int
    start: date_
    timezone: str
    end: date_ | None
    count: int | None
    #: Weekly: the selected weekdays (0 = Monday), or the anchor's weekday.
    weekdays: tuple[int, ...]
    #: Monthly: the explicit day of month, or the anchor's day.
    day: int

    @classmethod
    def of(cls, spec: RecurrenceSpec) -> "SeriesRule":
        if spec.start_date is None or spec.timezone is None:
            raise SeriesNotConfiguredError(
                "this recurring task has no start date and time zone yet; configure them before it can repeat."
            )
        return cls(
            frequency=spec.frequency, interval=spec.interval, start=spec.start_date, timezone=spec.timezone,
            end=spec.end_date, count=spec.count,
            weekdays=tuple(spec.weekdays) if spec.weekdays else (spec.start_date.weekday(),),
            day=spec.day_of_month or spec.start_date.day,
        )

    # ------------------------------------------------------------------ slot test

    def matches_cadence(self, day: date_) -> bool:
        """Whether `day` is a slot of the rule ignoring its bounds (start, end, count)."""
        if self.frequency == RecurrenceFrequency.DAILY:
            return (day - self.start).days % self.interval == 0
        if self.frequency == RecurrenceFrequency.WEEKLY:
            weeks = (_monday(day) - _monday(self.start)).days // 7
            return day.weekday() in self.weekdays and weeks % self.interval == 0
        months = _month_number(day) - _month_number(self.start)
        return day.day == self.day and months % self.interval == 0

    def is_slot(self, day: date_) -> bool:
        """Whether `day` is one of the series' slots (bounds and count included)."""
        if day < self.start or (self.end is not None and day > self.end) or not self.matches_cadence(day):
            return False
        return self.count is None or self.slot_index(day) < self.count

    # ------------------------------------------------------------------ slot index

    def slot_index(self, day: date_) -> int:
        """How many slots lie in [start, day) -- day's 0-based index when it is a slot. Arithmetic, bounded."""
        if day <= self.start:
            return 0
        if self.frequency == RecurrenceFrequency.DAILY:
            return math.ceil((day - self.start).days / self.interval)
        if self.frequency == RecurrenceFrequency.WEEKLY:
            return self._weekly_index(day)
        return self._monthly_index(day)

    def _weekly_index(self, day: date_) -> int:
        first_week = _monday(self.start)
        weeks = (_monday(day) - first_week).days // 7
        in_first = sum(1 for weekday in self.weekdays if weekday >= self.start.weekday())
        if weeks == 0:
            return sum(1 for weekday in self.weekdays if self.start.weekday() <= weekday < day.weekday())
        cadence_weeks_before = math.ceil(weeks / self.interval)  # cadence weeks j with 0 < j*interval < weeks, plus j=0
        full_after_first = cadence_weeks_before - 1
        count = in_first + full_after_first * len(self.weekdays)
        if weeks % self.interval == 0:
            count += sum(1 for weekday in self.weekdays if weekday < day.weekday())
        return count

    def _valid_months(self, steps: int) -> int:
        """How many of the first `steps` cadence months (k = 0 .. steps-1) contain the rule's day."""
        if steps <= 0:
            return 0
        if self.day <= 28:
            return steps
        prefix = _valid_month_prefix(_month_number(self.start) % _MONTHS_PER_CYCLE, self.interval, self.day)
        period = len(prefix) - 1
        full, rest = divmod(steps, period)
        return full * prefix[-1] + prefix[rest]

    def _monthly_index(self, day: date_) -> int:
        months = _month_number(day) - _month_number(self.start)
        cadence_before = math.ceil(months / self.interval)  # cadence months strictly before day's month
        count = self._valid_months(cadence_before)
        if self.day < self.start.day:
            count -= 1  # the anchor month's slot would precede the anchor
        if months % self.interval == 0 and day.day > self.day and _month_date(_month_number(day), self.day):
            count += 1  # day's own month, slot earlier in that month
        return max(count, 0)

    # ------------------------------------------------------------------ enumeration

    def slots(self, first: date_, last: date_) -> Iterator[date_]:
        """The slots in [first, last] (inclusive), in order. Raises RecurrenceBudgetError past MAX_SLOT_SCAN steps."""
        low = max(first, self.start)
        high = last if self.end is None else min(last, self.end)
        if high < low:
            return
        remaining = None
        if self.count is not None:
            remaining = self.count - self.slot_index(low)
            if remaining <= 0:
                return
        steps = 0
        for day in self._candidates(low, high):
            steps += 1
            if steps > MAX_SLOT_SCAN:
                raise RecurrenceBudgetError(f"listing the slots of {first} .. {last} exceeds the work budget.")
            if remaining is not None:
                if remaining <= 0:
                    return
                remaining -= 1
            yield day

    def _candidates(self, low: date_, high: date_) -> Iterator[date_]:
        if self.frequency == RecurrenceFrequency.DAILY:
            offset = math.ceil((low - self.start).days / self.interval) * self.interval
            day = self.start + timedelta(days=offset)
            while day <= high:
                yield day
                day += timedelta(days=self.interval)
            return
        if self.frequency == RecurrenceFrequency.WEEKLY:
            first_week = _monday(self.start)
            weeks = (_monday(low) - first_week).days // 7
            week = first_week + timedelta(weeks=math.ceil(weeks / self.interval) * self.interval)
            while week <= high:
                for weekday in self.weekdays:
                    day = week + timedelta(days=weekday)
                    if low <= day <= high:
                        yield day
                week += timedelta(weeks=self.interval)
            return
        first_month = _month_number(self.start)
        month = first_month + math.ceil((_month_number(low) - first_month) / self.interval) * self.interval
        while True:
            day = _month_date(month, self.day)
            month_start = _month_date(month, 1)
            if month_start is None or month_start > high:
                return
            if day is not None and low <= day <= high:
                yield day
            month += self.interval

    def first_slot_on_or_after(self, day: date_, *, horizon_days: int = 3700) -> date_ | None:
        """The first slot on/after `day` within `horizon_days`, or None."""
        return next(self.slots(day, day + timedelta(days=horizon_days)), None)


# -----------------------------------------------------------------------------
# Recurring-to-recurring dependencies
# -----------------------------------------------------------------------------


def cadence_problem(dependent: SeriesRule, prerequisite: SeriesRule) -> str | None:
    """
    Why `dependent`'s occurrences cannot each depend on `prerequisite`'s
    occurrence of the *same original slot*, or None when they can. Supported:
    the same time zone, and either a prerequisite that repeats every day, or
    the same frequency and interval in phase -- daily: the dependent's interval
    a multiple of the prerequisite's, anchors a whole number of prerequisite
    intervals apart; weekly: the dependent's weekdays among the prerequisite's,
    same interval, anchor weeks in step; monthly: the same day of month and
    interval, anchor months in step. Bounds (start, end, count) are checked per
    slot when occurrences are materialized.
    """
    if dependent.timezone != prerequisite.timezone:
        return (f"the prerequisite series repeats in {prerequisite.timezone}, this one in {dependent.timezone}; "
                "recurring dependencies need the same time zone.")
    if prerequisite.frequency == RecurrenceFrequency.DAILY and prerequisite.interval == 1:
        return None
    if dependent.frequency != prerequisite.frequency:
        return ("the two series repeat on different cadences; a recurring dependency needs the same frequency, or a "
                "prerequisite that repeats every day.")
    if dependent.frequency == RecurrenceFrequency.DAILY:
        aligned = (dependent.interval % prerequisite.interval == 0
                   and (dependent.start - prerequisite.start).days % prerequisite.interval == 0)
    elif dependent.frequency == RecurrenceFrequency.WEEKLY:
        weeks = (_monday(dependent.start) - _monday(prerequisite.start)).days // 7
        aligned = (dependent.interval == prerequisite.interval and weeks % prerequisite.interval == 0
                   and set(dependent.weekdays) <= set(prerequisite.weekdays))
    else:
        months = _month_number(dependent.start) - _month_number(prerequisite.start)
        aligned = (dependent.interval == prerequisite.interval and dependent.day == prerequisite.day
                   and months % prerequisite.interval == 0)
    if not aligned:
        return ("the two series do not repeat on the same dates; each occurrence must find the prerequisite's "
                "occurrence of the same date.")
    return None


# -----------------------------------------------------------------------------
# Daylight-saving reporting
# -----------------------------------------------------------------------------


def offset_transition(day: date_, tz_name: str) -> bool:
    """Whether the UTC offset of `tz_name` changes during local date `day` (a DST change day)."""
    zone = ZoneInfo(tz_name)
    start = datetime.combine(day, time(0), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)
    return start.utcoffset() != end.utcoffset()
