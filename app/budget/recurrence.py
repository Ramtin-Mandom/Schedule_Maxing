"""
app/budget/recurrence.py

The scheduled dates of a budget entry. Nothing is materialized: a range
query computes the dates it needs, so a recurring entry without an end date
never stores future rows.

    one-time   start_date only
    daily      every day from start_date
    weekly     every 7 days from start_date (same weekday)
    biweekly   every 14 days from start_date
    monthly    the start date's day number in every month from start_date. In
               a month without that day, the occurrence falls on the month's
               last day (Jan 31 -> Feb 28, or Feb 29 in a leap year -> Mar 31
               -> Apr 30); the next month returns to the original day, so
               dates never drift.

A recurring entry ends with its end_date (inclusive) when it has one.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from datetime import date, timedelta

from app.budget.models import MAX_BUDGET_DATE, BudgetEntry, BudgetRecurrence

_STEP_DAYS = {BudgetRecurrence.DAILY: 1, BudgetRecurrence.WEEKLY: 7, BudgetRecurrence.BIWEEKLY: 14}


def monthly_date(anchor: date, months_after: int) -> date:
    """The monthly occurrence `months_after` months after `anchor` (clamped to the month's last day)."""
    index = anchor.year * 12 + (anchor.month - 1) + months_after
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def occurrence_dates(entry: BudgetEntry, start: date, end: date) -> Iterator[date]:
    """The entry's scheduled dates within [start, end] (inclusive), in order."""
    first, last = max(start, entry.start_date), min(end, entry.end_date or MAX_BUDGET_DATE)
    if first > last:
        return
    if entry.recurrence is BudgetRecurrence.ONE_TIME:
        if first == entry.start_date:
            yield entry.start_date
        return
    anchor = entry.start_date
    if entry.recurrence is BudgetRecurrence.MONTHLY:
        index = (first.year - anchor.year) * 12 + (first.month - anchor.month)
        while True:
            day = monthly_date(anchor, index)
            if day > last:
                return
            if day >= first:
                yield day
            index += 1
    step = _STEP_DAYS[entry.recurrence]
    offset = -(-(first - anchor).days // step)  # the first index on or after `first` (ceiling division)
    day = anchor + timedelta(days=offset * step)
    while day <= last:
        yield day
        day += timedelta(days=step)


def is_occurrence_date(entry: BudgetEntry, day: date) -> bool:
    """Whether `day` is one of the entry's scheduled dates."""
    return next(occurrence_dates(entry, day, day), None) is not None
