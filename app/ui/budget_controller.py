"""
app/ui/budget_controller.py

BudgetController: the Tk-free layer between the Budgeting page / the Day
page's money strip and app.budget.service.BudgetService. It turns form text
into service calls and service results into display rows, and returns a
ControllerResult instead of raising -- nothing here is drawn, and nothing here
calculates money: totals, occurrences and validation are the service's.

The controller is bound to one workspace when it is built
(AppServices.budget_controller / DirectAppServices.budget_controller), so a
write started before a workspace switch can never land in the next one.

Editing a recurring entry always says which it means:
    scope "series"      the whole series (BudgetService.update_entry / delete_entry)
    scope "occurrence"  one occurrence, named by its scheduled date
                        (edit_occurrence / skip_occurrence); a field equal to
                        the series' value is stored as "follow the series", so a
                        later series edit still reaches it
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.budget.errors import BudgetError
from app.budget.models import (
    CATEGORIES,
    BudgetBalance,
    BudgetEntry,
    BudgetKind,
    BudgetOccurrence,
    BudgetRecurrence,
    IncomeIdea,
    WishlistItem,
)
from app.budget.money import format_amount, parse_amount
from app.budget.recurrence import occurrence_dates
from app.budget.stats import (
    AVERAGE_DAYS,
    HORIZON_DAYS,
    BalanceForecast,
    HistoryRow,
    balance_forecast,
    balance_history,
    budget_statistics,
)
from app.persistence.errors import DirectPersistenceError, NotSignedInError
from app.planning.errors import PlanningError, ScopeError
from app.ui.background import ControllerResult

SERIES, OCCURRENCE = "series", "occurrence"
#: The repeat choices of the forms, in order (label -> recurrence).
REPEAT_CHOICES = {"Daily": BudgetRecurrence.DAILY, "Weekly": BudgetRecurrence.WEEKLY,
                  "Every 2 weeks": BudgetRecurrence.BIWEEKLY, "Monthly": BudgetRecurrence.MONTHLY}
REPEAT_LABELS = {value: label for label, value in REPEAT_CHOICES.items()}
NO_PROJECT = "(no project)"
#: How far ahead the next occurrence of a series is looked for (one year and a little).
_LOOKAHEAD_DAYS = 400


def categories(kind: BudgetKind | str) -> tuple[str, ...]:
    return CATEGORIES[BudgetKind(kind)]


def money_text(amount_minor: int) -> str:
    return format_amount(amount_minor)


@dataclass(frozen=True)
class EntryRow:
    """One saved entry as the Budgeting page lists it."""

    entry: BudgetEntry
    name: str
    amount_text: str
    schedule_text: str
    category: str
    project_name: str | None
    #: How many of its occurrences were edited or skipped.
    edited_occurrences: int

    @property
    def recurring(self) -> bool:
        return self.entry.is_recurring


@dataclass(frozen=True)
class BudgetOverview:
    balance: BudgetBalance | None
    balance_text: str
    income: list[EntryRow]
    expenses: list[EntryRow]
    #: The workspace's live projects as (name, id), for the forms' project choice.
    projects: list[tuple[str, uuid.UUID]] = field(default_factory=list)


@dataclass(frozen=True)
class DayItem:
    """One transaction of a day (an occurrence, with any occurrence edit applied)."""

    occurrence: BudgetOccurrence
    amount_text: str

    @property
    def key(self) -> tuple[uuid.UUID, date]:
        return self.occurrence.entry_id, self.occurrence.occurrence_date

    @property
    def name(self) -> str:
        return self.occurrence.name

    @property
    def recurring(self) -> bool:
        return self.occurrence.recurrence is not BudgetRecurrence.ONE_TIME


@dataclass(frozen=True)
class BudgetDayView:
    day: date
    income: list[DayItem]
    expenses: list[DayItem]
    income_minor: int
    expense_minor: int

    @property
    def income_text(self) -> str:
        return money_text(self.income_minor)

    @property
    def expense_text(self) -> str:
        return money_text(self.expense_minor)

    @property
    def empty(self) -> bool:
        return not self.income and not self.expenses

    def items(self, kind: BudgetKind | str) -> list[DayItem]:
        return self.income if BudgetKind(kind) is BudgetKind.INCOME else self.expenses


@dataclass(frozen=True)
class StatTile:
    title: str
    value: str
    detail: str


@dataclass(frozen=True)
class StatsView:
    """Budgeting > Stats, worded: every figure comes from app/budget/stats.py."""

    tiles: list[StatTile]
    #: Which dates the history covers, or why there is none.
    history_note: str
    #: (label, minor units), oldest first.
    weekly: list[tuple[str, int]]
    monthly: list[tuple[str, int]]
    daily: list[tuple[str, int]]
    forecast: BalanceForecast
    forecast_summary: str
    assumptions: list[str]

    @property
    def forecast_points(self) -> list[tuple[date, int]]:
        return [(day.day, day.balance_minor) for day in self.forecast.days]


def _day_text(day: date) -> str:
    return f"{day:%a}, {day:%b} {day.day}, {day.year}"


#: History filters: the periods (label -> days back from today, None = all history) and the row types.
HISTORY_PERIODS = {"Last 30 days": 30, "Last 90 days": 90, "Last 12 months": 365, "All history": None}
HISTORY_TYPES = ("All", "Income", "Expenses", "Balance changes")
ANY = "(any)"
#: At most this many rows are listed (newest first); the summary counts them all.
HISTORY_ROW_LIMIT = 500


@dataclass(frozen=True)
class HistoryLine:
    """One row of the History list, worded."""

    row: HistoryRow
    date_text: str
    type_text: str
    name: str
    category: str
    amount_text: str
    balance_text: str
    #: For a manual balance change: what it was before and the correction; else the item's project.
    detail: str


@dataclass(frozen=True)
class HistoryView:
    #: Newest first, at most HISTORY_ROW_LIMIT.
    lines: list[HistoryLine]
    #: How many rows match the filters (the list may show fewer).
    matching: int
    summary: str
    categories: list[str]
    projects: list[str]


#: How an income idea's progress is defined (shown on the page).
PROGRESS_DEFINITION = ("Progress is the amount already received or firmly secured (for example agreed, signed or "
                       "invoiced) against the expected amount.")


@dataclass(frozen=True)
class IdeaRow:
    idea: IncomeIdea
    name: str
    expected_text: str
    secured_text: str
    remaining_text: str
    progress_percent: int
    progress_text: str
    status: str

    @property
    def progress_fraction(self) -> float:
        """For a progress bar only (capped at 1); the figures themselves are the texts above."""
        return min(1.0, self.progress_percent / 100)


@dataclass(frozen=True)
class WishRow:
    wish: WishlistItem
    name: str
    amount_text: str
    category: str
    project_name: str | None
    description: str
    converted_text: str


@dataclass(frozen=True)
class FutureView:
    ideas: list[IdeaRow]
    #: Wishlist items not yet turned into an expense, and those that were.
    wishes: list[WishRow]
    converted: list[WishRow]
    projects: list[tuple[str, uuid.UUID]] = field(default_factory=list)


@dataclass(frozen=True)
class EntryForm:
    """What an income or expense form holds (text as typed)."""

    kind: str
    name: str
    amount: str
    date: str
    category: str
    project: str = NO_PROJECT
    #: None = one-time; else a REPEAT_CHOICES label.
    repeat: str | None = None
    #: Editing: the entry, and whether the change is for the series or one occurrence (on `date`).
    entry_id: uuid.UUID | None = None
    scope: str = SERIES


def _date(text: str, name: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise BudgetError(f"{name} must be a date like 2026-10-19.") from None


class BudgetController:
    def __init__(self, service_factory: Callable[[], object], *,
                 projects: Callable[[], list] | None = None, today: Callable[[], date] = date.today) -> None:
        #: Returns the workspace's BudgetService (raises NotSignedInError in signed-out direct storage).
        self._service_factory = service_factory
        self._projects = projects or (lambda: [])
        self.today = today

    # ------------------------------------------------------------------

    def _call(self, operation: Callable[[], object]) -> ControllerResult:
        try:
            return ControllerResult.success(operation())
        except NotSignedInError as error:
            return ControllerResult.failure("Sign in to use budgeting with this storage.", error)
        except (BudgetError, ScopeError, PlanningError, DirectPersistenceError) as error:
            return ControllerResult.failure(str(error), error)
        except ValueError as error:
            return ControllerResult.failure(str(error), error)

    def _service(self):
        return self._service_factory()

    def _project_choices(self) -> list[tuple[str, uuid.UUID]]:
        try:
            projects = self._projects()
        except Exception:  # noqa: BLE001 - projects are only a convenience for the forms
            return []
        return sorted(((project.name, project.id) for project in projects), key=lambda item: item[0].casefold())

    def _project_id(self, choice: str, projects: list[tuple[str, uuid.UUID]] | None = None) -> uuid.UUID | None:
        if not choice or choice == NO_PROJECT:
            return None
        for name, project_id in projects if projects is not None else self._project_choices():
            if name == choice:
                return project_id
        raise BudgetError(f"The project {choice!r} no longer exists.")

    # ------------------------------------------------------------------
    # General: balance and entries
    # ------------------------------------------------------------------

    def overview(self) -> ControllerResult[BudgetOverview]:
        def read() -> BudgetOverview:
            service = self._service()
            balance = service.current_balance()
            projects = self._project_choices()
            names = {project_id: name for name, project_id in projects}
            edits: dict[uuid.UUID, int] = {}
            for override in service.repository.list("budget_override"):
                edits[override.entry_id] = edits.get(override.entry_id, 0) + 1
            rows = {kind: [] for kind in BudgetKind}
            for entry in sorted(service.list_entries(), key=lambda item: (item.start_date, item.name.casefold())):
                rows[entry.kind].append(EntryRow(
                    entry=entry, name=entry.name, amount_text=money_text(entry.amount_minor),
                    schedule_text=self.schedule_text(entry), category=entry.category,
                    project_name=names.get(entry.project_id) if entry.project_id else None,
                    edited_occurrences=edits.get(entry.id, 0)))
            balance_text = (f"{money_text(balance.amount_minor)}, set on {balance.as_of.isoformat()}" if balance
                            else "No balance entered yet.")
            return BudgetOverview(balance, balance_text, rows[BudgetKind.INCOME], rows[BudgetKind.EXPENSE], projects)

        return self._call(read)

    @staticmethod
    def schedule_text(entry: BudgetEntry) -> str:
        if not entry.is_recurring:
            return f"On {entry.start_date.isoformat()}"
        text = f"{REPEAT_LABELS[entry.recurrence]} from {entry.start_date.isoformat()}"
        return text + (f" until {entry.end_date.isoformat()}" if entry.end_date else "")

    def save_balance(self, amount: str) -> ControllerResult[BudgetBalance]:
        """Record the balance the user typed as today's (a manual change, kept in the history)."""
        return self._call(lambda: self._service().set_balance(amount.strip(), self.today()))

    def save_entry(self, form: EntryForm) -> ControllerResult[BudgetEntry | object]:
        """Create an entry, or change a series or one of its occurrences (see the module docstring)."""

        def save():
            service = self._service()
            day = _date(form.date, "The date")
            project_id = self._project_id(form.project)
            recurrence = REPEAT_CHOICES[form.repeat] if form.repeat else BudgetRecurrence.ONE_TIME
            if form.entry_id is None:
                return service.create_entry(kind=form.kind, name=form.name, amount=form.amount.strip(),
                                            category=form.category, start_date=day, recurrence=recurrence,
                                            project_id=project_id)
            entry = service.get_entry(form.entry_id)
            if form.scope == OCCURRENCE:
                amount = parse_amount(form.amount.strip())
                changes = {"name": form.name.strip(), "amount": amount, "category": form.category,
                           "project_id": project_id}
                series = {"name": entry.name, "amount": entry.amount_minor, "category": entry.category,
                          "project_id": entry.project_id}
                return service.edit_occurrence(entry.id, day, **{
                    name: (None if value == series[name] else value) for name, value in changes.items()})
            changes = {"name": form.name, "amount": form.amount.strip(), "category": form.category,
                       "start_date": day, "recurrence": recurrence, "project_id": project_id}
            if recurrence is BudgetRecurrence.ONE_TIME:
                changes["end_date"] = None
            return service.update_entry(entry.id, expected_version=entry.version, **changes)

        return self._call(save)

    def delete_entry(self, entry_id: uuid.UUID, *, scope: str = SERIES,
                     occurrence_date: str | None = None) -> ControllerResult[None]:
        """Delete the whole entry, or (scope "occurrence") skip only its occurrence on `occurrence_date`."""

        def delete() -> None:
            service = self._service()
            if scope == OCCURRENCE:
                service.skip_occurrence(entry_id, _date(occurrence_date or "", "The occurrence date"))
            else:
                service.delete_entry(entry_id)

        return self._call(delete)

    def next_occurrence(self, entry: BudgetEntry) -> date:
        """The entry's first scheduled date from today on (else its last one, else its start): a form's default."""
        today = self.today()
        upcoming = next(occurrence_dates(entry, today, today + timedelta(days=_LOOKAHEAD_DAYS)), None)
        if upcoming is not None:
            return upcoming
        if entry.end_date is not None and entry.end_date < today:
            past = list(occurrence_dates(entry, max(entry.start_date, entry.end_date - timedelta(days=62)),
                                         entry.end_date))
            if past:
                return past[-1]
        return entry.start_date

    # ------------------------------------------------------------------
    # Future: income ideas and the wishlist (never transactions)
    # ------------------------------------------------------------------

    def future(self) -> ControllerResult[FutureView]:
        def read() -> FutureView:
            service = self._service()
            projects = self._project_choices()
            names = {project_id: name for name, project_id in projects}
            ideas = [self.idea_row(idea) for idea in service.list_ideas()]
            wishes = [self._wish_row(wish, names) for wish in service.list_wishes()]
            return FutureView(ideas, [row for row in wishes if not row.wish.converted],
                              [row for row in wishes if row.wish.converted], projects)

        return self._call(read)

    @staticmethod
    def idea_row(idea: IncomeIdea) -> IdeaRow:
        percent = idea.progress_percent
        status = "Reached" if idea.reached else ("In progress" if idea.secured_amount_minor else "Not started")
        return IdeaRow(
            idea, idea.name, money_text(idea.expected_amount_minor), money_text(idea.secured_amount_minor),
            money_text(idea.remaining_minor), percent,
            f"{money_text(idea.secured_amount_minor)} of {money_text(idea.expected_amount_minor)} received or "
            f"secured ({percent}%)" + ("" if idea.reached else f"; {money_text(idea.remaining_minor)} to go"),
            status)

    @staticmethod
    def _wish_row(wish: WishlistItem, names: dict) -> WishRow:
        converted = (f"Turned into an expense on {wish.converted_on.isoformat()}" if wish.converted else "")
        return WishRow(wish, wish.name, money_text(wish.amount_minor), wish.category,
                       names.get(wish.project_id) if wish.project_id else None, wish.description or "", converted)

    def save_idea(self, idea_id: uuid.UUID | None, *, name: str, expected: str, description: str | None = None,
                  secured: str | None = None) -> ControllerResult[IncomeIdea]:
        """Add an idea (id None) or change one; `secured` is the amount received or secured so far."""

        def save() -> IncomeIdea:
            service = self._service()
            secured_text = (secured or "").strip() or "0"
            if idea_id is None:
                return service.create_idea(name=name, expected_amount=expected.strip(), secured_amount=secured_text,
                                           description=description)
            changes = {"name": name, "expected_amount": expected.strip(), "description": description}
            if secured is not None:
                changes["secured_amount"] = secured_text
            return service.update_idea(idea_id, **changes)

        return self._call(save)

    def delete_idea(self, idea_id: uuid.UUID) -> ControllerResult[None]:
        return self._call(lambda: self._service().delete_idea(idea_id))

    def save_wish(self, wish_id: uuid.UUID | None, *, name: str, amount: str, category: str,
                  project: str = NO_PROJECT, description: str | None = None) -> ControllerResult[WishlistItem]:
        def save() -> WishlistItem:
            service = self._service()
            fields = {"name": name, "amount": amount.strip(), "category": category,
                      "project_id": self._project_id(project), "description": description}
            if wish_id is None:
                return service.create_wish(**fields)
            return service.update_wish(wish_id, **fields)

        return self._call(save)

    def delete_wish(self, wish_id: uuid.UUID) -> ControllerResult[None]:
        return self._call(lambda: self._service().delete_wish(wish_id))

    def convert_wish(self, wish_id: uuid.UUID, on_date: str) -> ControllerResult[tuple]:
        """Turn the item into one dated expense (and mark it converted, so it cannot be converted again)."""
        return self._call(lambda: self._service().convert_wish(wish_id, _date(on_date, "The date")))

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> ControllerResult[StatsView]:
        def read() -> StatsView:
            service, today = self._service(), self.today()
            return self.stats_view(budget_statistics(service, today), balance_forecast(service, today))

        return self._call(read)

    @staticmethod
    def stats_view(stats, forecast: BalanceForecast) -> StatsView:
        tiles = []
        for title, best in (("Highest income day", stats.highest_income_day),
                            ("Highest expense day", stats.highest_expense_day)):
            tiles.append(StatTile(title, money_text(best.amount_minor), _day_text(best.day)) if best
                         else StatTile(title, "–", "No such day recorded yet."))
        if stats.has_history:
            note = (f"History from {stats.history_start.isoformat()} (your first entry) to today, "
                    f"{stats.today.isoformat()}. Future items are not history.")
        else:
            note = "No history yet: statistics appear once an entry is dated today or earlier."
        weekly = [(f"{item.start:%b} {item.start.day}", item.amount_minor) for item in stats.weekly_expenses]
        monthly = [(f"{item.start:%b %Y}", item.amount_minor) for item in stats.monthly_expenses]
        daily = [(f"{item.day.day}", item.amount_minor) for item in stats.daily_expenses]
        return StatsView(tiles, note, weekly, monthly, daily, forecast, *BudgetController._forecast_text(forecast))

    @staticmethod
    def _forecast_text(forecast: BalanceForecast) -> tuple[str, list[str]]:
        if not forecast.available:
            return ("No forecast yet: enter your current balance (General) to see the next "
                    f"{HORIZON_DAYS} days.", [])
        if forecast.start_balance_minor <= 0:
            summary = f"Your balance is already at or below zero today ({money_text(forecast.start_balance_minor)})."
        elif forecast.zero_day is not None:
            days = (forecast.zero_day - forecast.today).days
            summary = (f"Projected to reach zero or below on {_day_text(forecast.zero_day)} "
                       f"(in {days} day{'s' if days != 1 else ''}).")
        else:
            summary = (f"Projected to stay above zero through day {HORIZON_DAYS} "
                       f"({_day_text(forecast.days[-1].day)}), ending at {money_text(forecast.end_balance_minor)}.")
        window_start, window_end = forecast.average_window
        balance = forecast.balance
        assumptions = [
            f"Starts from your balance of {money_text(balance.amount_minor)} as of {balance.as_of.isoformat()}; "
            "nothing dated on or before that day is counted again.",
            f"Today's projected balance: {money_text(forecast.start_balance_minor)} (that balance plus what is "
            "recorded after it up to today).",
            f"Everyday spending: {money_text(forecast.average_sample_minor)} of one-time expenses from "
            f"{window_start.isoformat()} to {window_end.isoformat()} ({AVERAGE_DAYS} days, days without spending "
            f"count as zero) = {money_text(forecast.average_daily_spend_minor)} per day, subtracted every day.",
            "Repeating expenses are not part of that average; they are subtracted on their own dates.",
            "Future income (one-time and repeating) is added, and future one-time expenses are subtracted, on their "
            "dates.",
            "Wishlist items and future income ideas are not included.",
        ]
        return summary, assumptions

    # ------------------------------------------------------------------
    # Stats > History
    # ------------------------------------------------------------------

    def history(self, period: str = "Last 90 days", kind: str = "All", category: str = ANY,
                project: str = ANY) -> ControllerResult[HistoryView]:
        """The ledger up to today, filtered; balances are always calculated over the whole history."""

        def read() -> HistoryView:
            service, today = self._service(), self.today()
            rows = balance_history(service, today)
            projects = self._project_choices()
            names = {project_id: name for name, project_id in projects}
            days = HISTORY_PERIODS.get(period)
            first = today - timedelta(days=days - 1) if days else None
            wanted_kind = {"Income": "income", "Expenses": "expense", "Balance changes": "balance"}.get(kind)
            project_id = next((pid for name, pid in projects if name == project), None)
            matching = [
                row for row in rows
                if (first is None or row.day >= first) and (wanted_kind is None or row.kind == wanted_kind)
                and (category == ANY or row.category == category)
                and (project == ANY or (project_id is not None and row.project_id == project_id))]
            income = sum(row.amount_minor for row in matching if row.kind == "income")
            expense = -sum(row.amount_minor for row in matching if row.kind == "expense")
            transactions = sum(1 for row in matching if not row.is_balance)
            manual = len(matching) - transactions
            summary = (f"{transactions} transaction(s): income {money_text(income)}, expenses {money_text(expense)}, "
                       f"net {money_text(income - expense)}; {manual} manual balance change(s).")
            if len(matching) > HISTORY_ROW_LIMIT:
                summary += f" Showing the latest {HISTORY_ROW_LIMIT}."
            lines = [self._history_line(row, names) for row in reversed(matching[-HISTORY_ROW_LIMIT:])]
            categories = sorted({row.category for row in rows if row.category}, key=str.casefold)
            return HistoryView(lines, len(matching), summary, categories, [name for name, _ in projects])

        return self._call(read)

    @staticmethod
    def _history_line(row: HistoryRow, names: dict) -> HistoryLine:
        balance = money_text(row.balance_minor) if row.balance_minor is not None else "–"
        if row.is_balance:
            if row.previous_balance_minor is None:
                detail = "first balance entered"
            else:
                correction = row.correction_minor
                detail = (f"was {money_text(row.previous_balance_minor)} by calculation; "
                          f"{'+' if correction >= 0 else ''}{money_text(correction)}")
            return HistoryLine(row, row.day.isoformat(), "Balance changed manually", row.name, "",
                               money_text(row.amount_minor), balance, detail)
        sign = "+" if row.amount_minor >= 0 else ""
        detail = names.get(row.project_id, "") if row.project_id else ""
        type_text = ("Income" if row.kind == "income" else "Expense") + (" (repeating)" if row.recurring else "")
        return HistoryLine(row, row.day.isoformat(), type_text, row.name, row.category or "",
                           f"{sign}{money_text(row.amount_minor)}", balance, detail)

    # ------------------------------------------------------------------
    # The Day page
    # ------------------------------------------------------------------

    def day(self, day: date) -> ControllerResult[BudgetDayView]:
        return self._call(lambda: self._day_view(self._service(), day))

    @staticmethod
    def _day_view(service, day: date) -> BudgetDayView:
        occurrences = service.occurrences(day, day)
        totals = service.totals(day, day)
        items = {kind: [DayItem(item, money_text(item.amount_minor)) for item in occurrences if item.kind is kind]
                 for kind in BudgetKind}
        return BudgetDayView(day, items[BudgetKind.INCOME], items[BudgetKind.EXPENSE], totals.income_minor,
                             totals.expense_minor)

    def set_day_amounts(self, day: date, amounts: dict[tuple[uuid.UUID, date], str]) -> ControllerResult[BudgetDayView]:
        """
        Change the amounts of a day's transactions ({DayItem.key: amount text}),
        all or nothing. A one-time entry's amount changes; a recurring one
        changes for that occurrence only (back to "follow the series" when it is
        the series' amount again). Returns the day as stored afterwards.
        """

        def save() -> BudgetDayView:
            service = self._service()
            view = self._day_view(service, day)
            current = {item.key: item for item in view.income + view.expenses}
            with service.repository.transaction():
                for key, text in amounts.items():
                    item = current.get(key)
                    if item is None:
                        raise BudgetError("A transaction of this day changed meanwhile; reload the day.")
                    amount = parse_amount(text.strip())
                    if amount == item.occurrence.amount_minor:
                        continue
                    entry = service.get_entry(item.occurrence.entry_id)
                    if not entry.is_recurring:
                        service.update_entry(entry.id, expected_version=entry.version, amount=amount)
                    else:
                        service.edit_occurrence(entry.id, item.occurrence.occurrence_date,
                                                amount=None if amount == entry.amount_minor else amount)
            return self._day_view(service, day)

        return self._call(save)
