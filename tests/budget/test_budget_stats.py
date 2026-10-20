"""Budget statistics and the 90-day forecast (app/budget/stats.py): history only from recorded data, the 15-day
average of one-time spending (zero days included, recurring excluded), recurring and one-time future items, the
balance as a snapshot at its as-of date, the zero-crossing day, and exact arithmetic."""

from __future__ import annotations

from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from app.budget.repository import BudgetRepository
from app.budget.service import BudgetService
from app.budget.stats import (
    AVERAGE_DAYS,
    HORIZON_DAYS,
    balance_forecast,
    budget_statistics,
    round_minor,
)
from app.execution.db import get_connection
from app.planning.scope import OwnerScope

TODAY = date(2026, 6, 15)  # a Monday


@pytest.fixture
def budget(tmp_path: Path):
    conn = get_connection(tmp_path / "stats.db")
    try:
        yield BudgetService(BudgetRepository(conn).scoped(OwnerScope.ownerless()))
    finally:
        conn.close()


def expense(service, name, amount, day, recurrence="none", category="Other"):
    return service.create_entry(kind="expense", name=name, amount=amount, category=category, start_date=day,
                                recurrence=recurrence)


def income(service, name, amount, day, recurrence="none"):
    return service.create_entry(kind="income", name=name, amount=amount, category="Work 1", start_date=day,
                                recurrence=recurrence)


def ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def ahead(days: int) -> date:
    return TODAY + timedelta(days=days)


# -- zero data -------------------------------------------------------------------------


def test_no_records_report_nothing_and_no_forecast(budget) -> None:
    stats = budget_statistics(budget, TODAY)
    assert not stats.has_history and stats.highest_income_day is None and stats.highest_expense_day is None
    assert stats.weekly_expenses == stats.monthly_expenses == stats.daily_expenses == []
    forecast = balance_forecast(budget, TODAY)
    assert not forecast.available and forecast.days == [] and forecast.zero_day is None
    # Only future records: still no history (nothing is invented before the first record).
    expense(budget, "Trip", "300", ahead(10))
    assert not budget_statistics(budget, TODAY).has_history


def test_a_balance_without_transactions_stays_flat(budget) -> None:
    budget.set_balance("100", TODAY)
    forecast = balance_forecast(budget, TODAY)
    assert forecast.available and forecast.average_daily_spend == 0 and len(forecast.days) == HORIZON_DAYS
    assert {day.balance_minor for day in forecast.days} == {10000} and forecast.zero_day is None
    assert forecast.days[0].day == ahead(1) and forecast.days[-1].day == ahead(90)
    budget.set_balance("0", TODAY)
    assert balance_forecast(budget, TODAY).zero_day == ahead(1)  # zero counts as reaching zero


# -- the 15-day average ---------------------------------------------------------------


def test_the_average_counts_one_time_spending_of_the_last_15_days_including_zero_days(budget) -> None:
    budget.set_balance("1000", TODAY)
    expense(budget, "Today", "40", TODAY)
    expense(budget, "Oldest in window", "60", ago(AVERAGE_DAYS - 1))
    expense(budget, "Just outside", "500", ago(AVERAGE_DAYS))
    expense(budget, "Subscription", "9.99", ago(30), recurrence="daily")  # recurring: never averaged
    income(budget, "Pay", "999", ago(3))  # income: never averaged
    forecast = balance_forecast(budget, TODAY)
    assert forecast.average_window == (ago(14), TODAY)
    assert forecast.average_sample_minor == 10000
    assert forecast.average_daily_spend == Fraction(10000, 15)  # 6.666... per day, kept exact
    assert forecast.average_daily_spend_minor == 667
    # An edited occurrence of a recurring entry still does not enter the average.
    sub = next(entry for entry in budget.list_entries() if entry.name == "Subscription")
    budget.edit_occurrence(sub.id, ago(1), amount="500")
    assert balance_forecast(budget, TODAY).average_sample_minor == 10000


def test_exact_arithmetic_and_rounding() -> None:
    assert round_minor(Fraction(5, 2)) == 2 and round_minor(Fraction(7, 2)) == 4  # half to even
    assert round_minor(Fraction(-5, 2)) == -2 and round_minor(Fraction(10001, 3)) == 3334
    assert sum(Fraction(10000, 15) for _ in range(15)) == 10000


# -- recurrence, one-time items and income in the future --------------------------------


def test_each_future_day_applies_average_recurring_one_time_and_income(budget) -> None:
    budget.set_balance("5000", TODAY)
    expense(budget, "Groceries", "150", ago(2))  # average: 150 / 15 = 10.00 per day
    rent = expense(budget, "Rent", "1200", date(2026, 6, 1), recurrence="monthly")
    income(budget, "Salary", "2000", date(2026, 5, 29), recurrence="monthly")
    expense(budget, "Laptop", "900", ahead(5))
    income(budget, "Refund", "25.50", ahead(5))
    budget.skip_occurrence(rent.id, date(2026, 8, 1))
    forecast = balance_forecast(budget, TODAY)
    by_day = {day.day: day for day in forecast.days}
    assert forecast.start_balance_minor == 500000  # nothing after the snapshot yet
    assert by_day[ahead(1)].balance == 500000 - 1000
    five = by_day[ahead(5)]
    assert (five.one_time_expense_minor, five.income_minor, five.recurring_expense_minor) == (90000, 2550, 0)
    assert five.balance == 500000 - 5 * 1000 - 90000 + 2550
    july_first = by_day[date(2026, 7, 1)]
    assert july_first.recurring_expense_minor == 120000 and by_day[date(2026, 6, 29)].income_minor == 200000
    assert by_day[date(2026, 8, 1)].recurring_expense_minor == 0  # the skipped occurrence is not subtracted
    # The running balance is exactly the sum of its parts.
    expected = Fraction(500000)
    for day in forecast.days:
        expected += day.income_minor - day.recurring_expense_minor - day.one_time_expense_minor - day.average_spend
        assert day.balance == expected


# -- the balance snapshot -------------------------------------------------------------------


def test_the_balance_is_a_snapshot_at_its_as_of_date(budget) -> None:
    budget.set_balance("300", ago(5))
    expense(budget, "Before the snapshot", "1000", ago(6))  # already in the balance
    expense(budget, "On the snapshot date", "1000", ago(5))  # already in the balance
    expense(budget, "After it", "50", ago(2))  # recorded since: counted once
    income(budget, "Since", "20", TODAY)
    forecast = balance_forecast(budget, TODAY)
    assert forecast.start_balance_minor == 30000 - 5000 + 2000
    # A snapshot dated in the future: nothing is applied before it.
    budget.set_balance("80", ahead(3))
    future = balance_forecast(budget, TODAY)
    assert [day.balance_minor for day in future.days[:3]] == [8000, 8000, 8000]
    assert future.days[3].average_spend == future.average_daily_spend


# -- the zero-crossing day ----------------------------------------------------------------------


def test_the_first_day_at_or_below_zero(budget) -> None:
    budget.set_balance("100", TODAY)
    expense(budget, "Spending", "150", TODAY)  # 10.00 per day on average
    forecast = balance_forecast(budget, TODAY)
    assert forecast.zero_day == ahead(10)  # 100 - 10 x 10 = 0 on the tenth day
    assert forecast.days[8].balance > 0 and forecast.days[9].balance == 0
    income(budget, "Top-up", "1000", ahead(9))
    assert balance_forecast(budget, TODAY).zero_day is None  # stays above zero through day 90
    assert balance_forecast(budget, TODAY).end_balance_minor == 10000 + 100000 - 90 * 1000


# -- statistics ----------------------------------------------------------------------------------


def test_history_statistics(budget) -> None:
    expense(budget, "Rent", "1000", date(2026, 1, 10), recurrence="monthly")
    expense(budget, "Gift", "1000", date(2026, 3, 3))
    expense(budget, "Coffee", "4", ago(1))
    expense(budget, "Future", "99999", ahead(1))  # never history
    income(budget, "Pay", "300", date(2026, 2, 1), recurrence="monthly")
    income(budget, "Bonus", "700", date(2026, 4, 1))
    stats = budget_statistics(budget, TODAY)
    assert stats.history_start == date(2026, 1, 10)
    assert (stats.highest_expense_day.day, stats.highest_expense_day.amount_minor) == (date(2026, 1, 10), 100000)
    assert (stats.highest_income_day.day, stats.highest_income_day.amount_minor) == (date(2026, 4, 1), 100000)
    assert [(item.start, item.amount_minor) for item in stats.monthly_expenses] == [
        (date(2026, 1, 1), 100000), (date(2026, 2, 1), 100000), (date(2026, 3, 1), 200000),
        (date(2026, 4, 1), 100000), (date(2026, 5, 1), 100000), (date(2026, 6, 1), 100400)]
    assert stats.monthly_expenses[-1].end == TODAY
    weeks = stats.weekly_expenses
    assert len(weeks) == 20 and weeks[-1].start == TODAY and weeks[-1].end == TODAY
    assert weeks[0].start == TODAY - timedelta(weeks=19) and all(week.start.weekday() == 0 for week in weeks)
    assert weeks[-2].amount_minor == 100400 and weeks[-3].amount_minor == 0  # June 8-14: rent (10th) + coffee
    assert len(stats.daily_expenses) == 30 and stats.daily_expenses[-1].day == TODAY
    assert stats.daily_expenses[-2].amount_minor == 400 and stats.daily_expenses[-1].amount_minor == 0


def test_a_short_history_is_not_padded_before_its_start(budget) -> None:
    expense(budget, "First", "5", ago(9))
    stats = budget_statistics(budget, TODAY)
    assert [day.day for day in stats.daily_expenses] == [ago(offset) for offset in range(9, -1, -1)]
    assert len(stats.weekly_expenses) == 3 and len(stats.monthly_expenses) == 1  # June 6 is in the week of June 1
    assert stats.highest_income_day is None


# -- history ---------------------------------------------------------------------------------------


def test_the_history_lists_transactions_and_manual_balance_changes_with_running_balances(budget) -> None:
    from app.budget.stats import balance_history

    expense(budget, "Before any balance", "20", ago(10))
    budget.set_balance("500", ago(5))
    expense(budget, "Rent", "100", ago(5), recurrence="weekly")  # on the balance's date: already in it
    income(budget, "Pay", "50", ago(3))
    expense(budget, "Coffee", "4.50", ago(3))
    budget.set_balance("430", ago(1))  # a manual correction
    expense(budget, "Future", "999", ahead(2))  # never history
    rows = balance_history(budget, TODAY)
    assert [(row.day, row.kind, row.name, row.amount_minor, row.balance_minor) for row in rows] == [
        (ago(10), "expense", "Before any balance", -2000, None),
        (ago(5), "expense", "Rent", -10000, None),
        (ago(5), "balance", "Balance set manually", 50000, 50000),
        (ago(3), "income", "Pay", 5000, 55000),
        (ago(3), "expense", "Coffee", -450, 54550),
        (ago(1), "balance", "Balance set manually", 43000, 43000),
    ]
    first, correction = rows[2], rows[5]
    assert first.previous_balance_minor is None and first.correction_minor is None
    assert (correction.previous_balance_minor, correction.correction_minor) == (54550, -11550)
    assert rows[1].recurring and not rows[3].recurring
    assert balance_history(budget, ago(11)) == []  # nothing dated yet
