"""
app/budget/stats.py

Budget statistics and the 90-day balance forecast, computed from the stored
records through BudgetService (so occurrence edits and skips apply exactly
as everywhere else). Tk-free and deterministic: every result depends only on
the records and the `today` passed in. Money stays exact -- totals are
integer minor units, and the forecast's running balance is a Fraction (the
average daily spend is rarely a whole number of cents); only display values
are rounded (half to even, to the cent).

History (budget_statistics) is the occurrences dated from the first entry's
date up to and including today -- nothing before the first record and nothing
in the future is reported, so no observation is invented:
    highest income day / highest expense day   the date with the largest total
                                               (the earliest such date on a tie)
    weekly expenses     Monday-to-Sunday weeks, the current one included (to
                        today), at most WEEKS of them, none before the history
    monthly expenses    every calendar month of the history
    daily expenses      at most DAYS days up to today, none before the history
History older than BudgetService.MAX_RANGE_DAYS before today is not read.

The history (balance_history) lists, oldest first, every transaction dated
from the first entry or balance up to today and every manual balance change,
with the balance after each: from the first manual balance on, the running
balance adds each transaction; a manual change (a snapshot at the end of its
date, so that date's transactions come before it) replaces it and records the
balance calculated just before, so the size of the correction shows. Before
the first manual balance the balance is unknown (None).

The forecast (balance_forecast):
    - The entered balance is a snapshot at the end of its as-of date: no
      transaction dated on or before it is counted again.
    - The starting point is that balance plus the recorded transactions after
      its as-of date up to today.
    - The average daily spend is the total of the one-time (non-recurring)
      expenses dated in the last AVERAGE_DAYS calendar days, today included,
      divided by AVERAGE_DAYS -- days without spending count as zero.
      Occurrences of recurring entries are excluded (they are scheduled below).
    - For each of the next HORIZON_DAYS days: subtract the average daily
      spend, subtract the recurring and the one-time expenses scheduled that
      day, and add that day's income (recurring and one-time).
    - Days up to a future as-of date are the snapshot itself (nothing is
      applied before it).
    - The zero day is the first forecast day whose projected balance is at or
      below zero (None when it stays above zero through the horizon).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction

import uuid

from app.budget.models import BudgetBalance, BudgetKind, BudgetOccurrence, BudgetRecurrence
from app.budget.service import MAX_RANGE_DAYS, BudgetService

WEEKS = 20
DAYS = 30
AVERAGE_DAYS = 15
HORIZON_DAYS = 90


def round_minor(value: Fraction) -> int:
    """An exact amount of minor units rounded half to even to a whole minor unit (for display)."""
    whole, rest = divmod(value.numerator, value.denominator)  # floor division: 0 <= rest < denominator
    if 2 * rest > value.denominator or (2 * rest == value.denominator and whole % 2):
        whole += 1
    return whole


def occurrences_between(service: BudgetService, start: date, end: date) -> list[BudgetOccurrence]:
    """service.occurrences over any span (read in MAX_RANGE_DAYS pieces; each occurrence once, by its date)."""
    found: list[BudgetOccurrence] = []
    while start <= end:
        piece_end = min(end, start + timedelta(days=MAX_RANGE_DAYS - 1))
        found.extend(service.occurrences(start, piece_end))
        start = piece_end + timedelta(days=1)
    return found


@dataclass(frozen=True)
class DayTotal:
    day: date
    amount_minor: int


@dataclass(frozen=True)
class PeriodTotal:
    start: date
    end: date
    amount_minor: int


@dataclass(frozen=True)
class BudgetStatistics:
    today: date
    #: The first date of the history (the earliest entry's date), or None when nothing is dated up to today.
    history_start: date | None
    highest_income_day: DayTotal | None
    highest_expense_day: DayTotal | None
    weekly_expenses: list[PeriodTotal] = field(default_factory=list)
    monthly_expenses: list[PeriodTotal] = field(default_factory=list)
    daily_expenses: list[DayTotal] = field(default_factory=list)

    @property
    def has_history(self) -> bool:
        return self.history_start is not None


@dataclass(frozen=True)
class ForecastDay:
    day: date
    income_minor: int
    recurring_expense_minor: int
    one_time_expense_minor: int
    #: The average daily spend subtracted this day (0 on days covered by a future balance snapshot).
    average_spend: Fraction
    #: The exact projected balance at the end of the day.
    balance: Fraction

    @property
    def balance_minor(self) -> int:
        return round_minor(self.balance)


@dataclass(frozen=True)
class BalanceForecast:
    today: date
    #: The entered balance (None: no forecast can be made).
    balance: BudgetBalance | None
    #: The projected balance at the end of today (the snapshot plus what was recorded after it).
    start_balance_minor: int = 0
    average_window: tuple[date, date] | None = None
    #: One-time expenses in the average window, and the average daily spend derived from them.
    average_sample_minor: int = 0
    average_daily_spend: Fraction = Fraction(0)
    days: list[ForecastDay] = field(default_factory=list)
    #: The first day at or below zero (None: above zero through the horizon).
    zero_day: date | None = None

    @property
    def available(self) -> bool:
        return self.balance is not None

    @property
    def average_daily_spend_minor(self) -> int:
        return round_minor(self.average_daily_spend)

    @property
    def end_balance_minor(self) -> int | None:
        return self.days[-1].balance_minor if self.days else None


BALANCE_ROW = "balance"


@dataclass(frozen=True)
class HistoryRow:
    day: date
    #: "income", "expense", or BALANCE_ROW for a manual balance change.
    kind: str
    name: str
    category: str | None
    project_id: uuid.UUID | None
    #: A transaction's amount, signed (+ income, - expense); for a balance change, the balance entered.
    amount_minor: int
    #: The balance after this row (None while no manual balance precedes it).
    balance_minor: int | None
    #: A balance change: the balance calculated just before it (None: there was none).
    previous_balance_minor: int | None = None
    recurring: bool = False

    @property
    def is_balance(self) -> bool:
        return self.kind == BALANCE_ROW

    @property
    def correction_minor(self) -> int | None:
        """A balance change: how far it moved the calculated balance."""
        if not self.is_balance or self.previous_balance_minor is None:
            return None
        return self.amount_minor - self.previous_balance_minor


def balance_history(service: BudgetService, today: date) -> list[HistoryRow]:
    """Every transaction and manual balance change up to today, oldest first (see the module docstring)."""
    balances = [item for item in service.list_balances() if item.as_of <= today]
    starts = [entry.start_date for entry in service.list_entries() if entry.start_date <= today]
    starts += [item.as_of for item in balances]
    if not starts:
        return []
    start = max(min(starts), today - timedelta(days=MAX_RANGE_DAYS - 1))
    by_day: dict[date, list[BudgetOccurrence]] = defaultdict(list)
    for item in occurrences_between(service, start, today):
        by_day[item.date].append(item)
    changes: dict[date, list[BudgetBalance]] = defaultdict(list)
    for item in balances:
        changes[max(item.as_of, start)].append(item)
    rows: list[HistoryRow] = []
    running: int | None = None
    for day in sorted(set(by_day) | set(changes)):
        for item in by_day.get(day, []):  # occurrences(): by date, income first, then name
            signed = item.signed_amount_minor
            running = running + signed if running is not None else None
            rows.append(HistoryRow(day, item.kind.value, item.name, item.category, item.project_id, signed, running,
                                   recurring=item.recurrence is not BudgetRecurrence.ONE_TIME))
        for change in changes.get(day, []):
            rows.append(HistoryRow(day, BALANCE_ROW, "Balance set manually", None, None, change.amount_minor,
                                   change.amount_minor, running))
            running = change.amount_minor
    return rows


def _daily(occurrences: list[BudgetOccurrence], kind: BudgetKind) -> dict[date, int]:
    totals: dict[date, int] = defaultdict(int)
    for item in occurrences:
        if item.kind is kind:
            totals[item.date] += item.amount_minor
    return totals


def _highest(totals: dict[date, int]) -> DayTotal | None:
    positive = [(amount, day) for day, amount in totals.items() if amount > 0]
    if not positive:
        return None
    amount, day = max(positive, key=lambda item: (item[0], -item[1].toordinal()))
    return DayTotal(day, amount)


def history_start(service: BudgetService, today: date) -> date | None:
    """The first date of the history: the earliest entry date up to today, within MAX_RANGE_DAYS of today."""
    starts = [entry.start_date for entry in service.list_entries() if entry.start_date <= today]
    if not starts:
        return None
    return max(min(starts), today - timedelta(days=MAX_RANGE_DAYS - 1))


def budget_statistics(service: BudgetService, today: date) -> BudgetStatistics:
    start = history_start(service, today)
    if start is None:
        return BudgetStatistics(today, None, None, None)
    occurrences = service.occurrences(start, today)
    expenses, income = _daily(occurrences, BudgetKind.EXPENSE), _daily(occurrences, BudgetKind.INCOME)

    week_start = today - timedelta(days=today.weekday())
    weeks = []
    for index in range(WEEKS):
        first = week_start - timedelta(weeks=index)
        last = min(first + timedelta(days=6), today)
        if last < start:
            break
        weeks.append(PeriodTotal(first, last, sum(amount for day, amount in expenses.items() if first <= day <= last)))
    months = []
    year, month = start.year, start.month
    while (year, month) <= (today.year, today.month):
        first = date(year, month, 1)
        following = date(year + month // 12, month % 12 + 1, 1)
        last = min(following - timedelta(days=1), today)
        months.append(PeriodTotal(first, last, sum(amount for day, amount in expenses.items() if first <= day <= last)))
        year, month = following.year, following.month
    days = [DayTotal(day, expenses.get(day, 0))
            for day in (today - timedelta(days=offset) for offset in range(DAYS - 1, -1, -1)) if day >= start]
    return BudgetStatistics(today, start, _highest(income), _highest(expenses), list(reversed(weeks)), months, days)


def balance_forecast(service: BudgetService, today: date, horizon: int = HORIZON_DAYS) -> BalanceForecast:
    balance = service.current_balance()
    if balance is None:
        return BalanceForecast(today, None)
    # The snapshot plus everything recorded after it up to today (nothing on or before its as-of date again).
    start = Fraction(balance.amount_minor)
    if balance.as_of < today:
        start += _net(occurrences_between(service, balance.as_of + timedelta(days=1), today))
    window = (today - timedelta(days=AVERAGE_DAYS - 1), today)
    sample = sum(item.amount_minor for item in service.occurrences(*window)
                 if item.kind is BudgetKind.EXPENSE and item.recurrence is BudgetRecurrence.ONE_TIME)
    average = Fraction(sample, AVERAGE_DAYS)

    first, last = today + timedelta(days=1), today + timedelta(days=horizon)
    scheduled: dict[date, list[BudgetOccurrence]] = defaultdict(list)
    for item in occurrences_between(service, max(first, balance.as_of + timedelta(days=1)), last):
        scheduled[item.date].append(item)
    running, days, zero_day = start, [], None
    for offset in range(1, horizon + 1):
        day = today + timedelta(days=offset)
        items = scheduled.get(day, [])
        income = sum(item.amount_minor for item in items if item.kind is BudgetKind.INCOME)
        recurring = sum(item.amount_minor for item in items
                        if item.kind is BudgetKind.EXPENSE and item.recurrence is not BudgetRecurrence.ONE_TIME)
        one_time = sum(item.amount_minor for item in items
                       if item.kind is BudgetKind.EXPENSE and item.recurrence is BudgetRecurrence.ONE_TIME)
        spend = average if day > balance.as_of else Fraction(0)
        if day > balance.as_of:
            running += income - recurring - one_time - spend
        else:
            running = Fraction(balance.amount_minor)
        days.append(ForecastDay(day, income, recurring, one_time, spend, running))
        if zero_day is None and running <= 0:
            zero_day = day
    return BalanceForecast(today, balance, round_minor(start), window, sample, average, days, zero_day)


def _net(occurrences: list[BudgetOccurrence]) -> Fraction:
    return Fraction(sum(item.signed_amount_minor for item in occurrences))
