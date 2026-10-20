"""Budget recurrence (app/budget/recurrence.py): one-time, daily, weekly, biweekly and monthly dates, end dates,
and monthly items in shorter months (the month's last day, without drift)."""

from __future__ import annotations

from datetime import date

import pytest

from app.budget.models import BudgetEntry
from app.budget.recurrence import is_occurrence_date, occurrence_dates


def entry(start: date, recurrence: str = "none", end: date | None = None) -> BudgetEntry:
    return BudgetEntry(kind="expense", name="Item", amount_minor=100, category="Other", start_date=start,
                       recurrence=recurrence, end_date=end)


def dates(item: BudgetEntry, start: date, end: date) -> list[date]:
    return list(occurrence_dates(item, start, end))


def test_a_one_time_entry_occurs_once() -> None:
    item = entry(date(2026, 5, 10))
    assert dates(item, date(2026, 1, 1), date(2026, 12, 31)) == [date(2026, 5, 10)]
    assert dates(item, date(2026, 5, 11), date(2026, 12, 31)) == []
    assert dates(item, date(2026, 5, 10), date(2026, 5, 10)) == [date(2026, 5, 10)]


def test_daily_weekly_and_biweekly_step_from_the_start_date() -> None:
    start = date(2026, 3, 2)  # a Monday
    assert dates(entry(start, "daily"), date(2026, 3, 1), date(2026, 3, 4)) == [
        date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]
    assert dates(entry(start, "weekly"), date(2026, 3, 3), date(2026, 3, 23)) == [
        date(2026, 3, 9), date(2026, 3, 16), date(2026, 3, 23)]
    assert dates(entry(start, "biweekly"), date(2026, 3, 3), date(2026, 4, 30)) == [
        date(2026, 3, 16), date(2026, 3, 30), date(2026, 4, 13), date(2026, 4, 27)]
    assert all(day.weekday() == 0 for day in dates(entry(start, "weekly"), start, date(2027, 3, 1)))


def test_monthly_keeps_the_day_number_and_uses_the_last_day_of_shorter_months() -> None:
    assert dates(entry(date(2026, 1, 31), "monthly"), date(2026, 1, 1), date(2026, 6, 30)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31),
        date(2026, 6, 30)]
    # A leap year's February has a 29th; the 30th falls back to it, and then returns to the 30th (no drift).
    assert dates(entry(date(2027, 12, 30), "monthly"), date(2028, 1, 1), date(2028, 3, 31)) == [
        date(2028, 1, 30), date(2028, 2, 29), date(2028, 3, 30)]
    # A day every month has is unaffected.
    assert dates(entry(date(2026, 1, 15), "monthly"), date(2026, 11, 1), date(2027, 2, 28)) == [
        date(2026, 11, 15), date(2026, 12, 15), date(2027, 1, 15), date(2027, 2, 15)]


def test_the_end_date_is_inclusive_and_nothing_occurs_before_the_start() -> None:
    item = entry(date(2026, 3, 2), "weekly", end=date(2026, 3, 16))
    assert dates(item, date(2026, 1, 1), date(2026, 12, 31)) == [date(2026, 3, 2), date(2026, 3, 9), date(2026, 3, 16)]
    assert dates(item, date(2026, 2, 1), date(2026, 3, 1)) == []


def test_occurrence_membership() -> None:
    item = entry(date(2026, 1, 31), "monthly")
    assert is_occurrence_date(item, date(2026, 2, 28))
    assert not is_occurrence_date(item, date(2026, 2, 27))
    assert not is_occurrence_date(item, date(2025, 12, 31))


def test_a_long_range_is_computed_without_materializing() -> None:
    item = entry(date(2026, 1, 1), "daily")
    assert len(dates(item, date(2026, 1, 1), date(2035, 12, 31))) == 3652


@pytest.mark.parametrize("recurrence", ["none", "daily", "weekly", "biweekly", "monthly"])
def test_every_recurrence_is_accepted(recurrence) -> None:
    assert dates(entry(date(2026, 3, 2), recurrence), date(2026, 3, 2), date(2026, 3, 2)) == [date(2026, 3, 2)]
