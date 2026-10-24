"""BudgetService over the local SQLite database (app/budget): validation, CRUD, range expansion, occurrence and
series edits without double counting, the balance and its projection, owner isolation, and the separation from
the scheduling engines."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.budget.errors import BudgetConflictError, BudgetNotFoundError, BudgetValidationError
from app.budget.models import EXPENSE_CATEGORIES, INCOME_CATEGORIES
from app.budget.repository import BudgetRepository
from app.budget.service import MAX_RANGE_DAYS, BudgetService
from app.execution.db import get_connection
from app.planning.application import PlanningService
from app.planning.errors import ScopeError
from app.planning.models import Project
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope

ALICE = OwnerScope.account(uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
BOB = OwnerScope.account(uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))
JAN, DEC = date(2026, 1, 1), date(2026, 12, 31)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def connection(tmp_path: Path):
    conn = get_connection(tmp_path / "budget.db")
    try:
        yield conn
    finally:
        conn.close()


def service_for(connection, scope: OwnerScope = OwnerScope.ownerless()) -> BudgetService:
    return BudgetService(BudgetRepository(connection).scoped(scope), Clock())


@pytest.fixture
def budget(connection) -> BudgetService:
    return service_for(connection)


def salary(service: BudgetService, **fields):
    data = {"kind": "income", "name": "Salary", "amount": "2000.00", "category": "Work 1",
            "start_date": date(2026, 1, 31), "recurrence": "monthly"} | fields
    return service.create_entry(**data)


def rent(service: BudgetService, **fields):
    data = {"kind": "expense", "name": "Rent", "amount": "1200", "category": "Housing",
            "start_date": date(2026, 1, 1), "recurrence": "monthly"} | fields
    return service.create_entry(**data)


def amounts(service: BudgetService, start: date, end: date, name: str | None = None) -> list[tuple[date, int]]:
    return [(item.date, item.amount_minor) for item in service.occurrences(start, end)
            if name is None or item.name == name]


# -- categories, validation, CRUD ------------------------------------------------


def test_categories_include_the_required_ones() -> None:
    assert INCOME_CATEGORIES == ("Work 1", "Work 2", "Work 3", "Other")
    assert "Other" in EXPENSE_CATEGORIES and len(EXPENSE_CATEGORIES) > 5


@pytest.mark.parametrize(("fields", "message"), [
    ({"category": "Housing"}, "not an income category"),
    ({"amount": 2000.0}, "never a float"),
    ({"amount": "0"}, "greater than zero"),
    ({"amount": "-3"}, "negative"),
    ({"amount": "1.234"}, "decimal places"),
    ({"recurrence": "yearly"}, "recurrence must be one of"),
    ({"kind": "gift"}, "kind must be one of"),
    ({"name": "   "}, "cannot be empty"),
    ({"start_date": "31/01/2026"}, "date like"),
    ({"start_date": datetime(2026, 1, 31, 12)}, "calendar date"),
    ({"start_date": date(1800, 1, 1)}, "between"),
    ({"end_date": date(2026, 1, 1)}, "before the start date"),
    ({"recurrence": "none", "end_date": date(2026, 6, 1)}, "one-time entry has no end date"),
    ({"project_id": uuid.uuid4()}, "project of this workspace"),
])
def test_invalid_entries_are_refused_and_nothing_is_stored(budget, fields, message) -> None:
    with pytest.raises(BudgetValidationError, match=message):
        salary(budget, **fields)
    assert budget.list_entries() == []


def test_crud_and_versions(budget) -> None:
    created = rent(budget, end_date=date(2026, 6, 1))
    assert created.amount_minor == 120000 and created.version == 1 and created.user_id is None
    assert budget.get_entry(created.id) == created
    updated = budget.update_entry(created.id, expected_version=1, amount="1250.50", name="Flat rent")
    assert (updated.amount_minor, updated.name, updated.version) == (125050, "Flat rent", 2)
    assert budget.update_entry(created.id, amount="1250.5") == updated  # no change: no new version
    with pytest.raises(BudgetConflictError):
        budget.update_entry(created.id, expected_version=1, amount="1")
    with pytest.raises(BudgetValidationError, match="Unknown entry field"):
        budget.update_entry(created.id, colour="red")
    cleared = budget.update_entry(created.id, end_date=None)
    assert cleared.end_date is None
    budget.delete_entry(created.id)
    with pytest.raises(BudgetNotFoundError):
        budget.get_entry(created.id)
    assert budget.list_entries() == []
    assert budget.occurrences(JAN, DEC) == []


def test_a_project_link_must_name_a_live_project_of_the_same_workspace(connection, budget) -> None:
    planning = PlanningService(PlanningRepository(connection)).scoped(OwnerScope.ownerless())
    project = planning.create_project(Project(name="Thesis"))
    linked = salary(budget, kind="expense", category="Education", name="Books", project_id=project.id)
    assert linked.project_id == project.id
    assert [item.project_id for item in budget.occurrences(JAN, date(2026, 2, 28), project_id=project.id)] == [
        project.id, project.id]
    other = PlanningService(PlanningRepository(connection)).scoped(ALICE).create_project(
        Project(name="Alice's", user_id=ALICE.user_id))
    with pytest.raises(BudgetValidationError, match="project"):
        budget.update_entry(linked.id, project_id=other.id)


# -- range queries ---------------------------------------------------------------


def test_recurring_entries_expand_on_demand_without_stored_rows(connection, budget) -> None:
    entry = salary(budget)
    occurrences = budget.occurrences(date(2026, 1, 1), date(2035, 12, 31))
    assert len(occurrences) == 120
    assert connection.execute("SELECT COUNT(*) FROM budget_entries").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM budget_overrides").fetchone()[0] == 0
    assert occurrences[1].occurrence_date == date(2026, 2, 28) and occurrences[1].entry_id == entry.id


def test_ranges_are_validated(budget) -> None:
    with pytest.raises(BudgetValidationError, match="ends before"):
        budget.occurrences(DEC, JAN)
    with pytest.raises(BudgetValidationError, match="at most"):
        budget.occurrences(JAN, JAN + timedelta(days=MAX_RANGE_DAYS))
    assert budget.occurrences(JAN, JAN + timedelta(days=MAX_RANGE_DAYS - 1)) == []


def test_occurrences_are_ordered_and_filtered(budget) -> None:
    salary(budget)
    rent(budget)
    budget.create_entry(kind="expense", name="Coffee", amount="3.50", category="Dining", start_date=date(2026, 1, 31))
    listed = budget.occurrences(date(2026, 1, 31), date(2026, 2, 1))
    assert [(item.date, item.name) for item in listed] == [
        (date(2026, 1, 31), "Salary"), (date(2026, 1, 31), "Coffee"), (date(2026, 2, 1), "Rent")]
    assert [item.name for item in budget.occurrences(JAN, date(2026, 2, 1), kind="income")] == ["Salary"]
    assert listed[0].signed_amount_minor == 200000 and listed[1].signed_amount_minor == -350


# -- occurrence edits --------------------------------------------------------------


def test_editing_one_occurrence_replaces_it_once(budget) -> None:
    entry = salary(budget)
    edit = budget.edit_occurrence(entry.id, date(2026, 3, 31), amount="2500", name="Salary + bonus")
    assert amounts(budget, JAN, date(2026, 4, 30)) == [
        (date(2026, 1, 31), 200000), (date(2026, 2, 28), 200000), (date(2026, 3, 31), 250000),
        (date(2026, 4, 30), 200000)]
    march = budget.occurrences(date(2026, 3, 1), date(2026, 3, 31))
    assert [(item.name, item.override_id) for item in march] == [("Salary + bonus", edit.id)]
    # A second edit of the same occurrence updates the same override and keeps the earlier fields.
    again = budget.edit_occurrence(entry.id, date(2026, 3, 31), category="Work 2")
    assert again.id == edit.id and again.amount_minor == 250000 and again.category == "Work 2"
    assert len(budget.list_overrides(entry.id)) == 1
    assert budget.totals(JAN, date(2026, 4, 30)).income_minor == 850000


def test_moved_occurrences_count_once_at_their_new_date(budget) -> None:
    entry = salary(budget)
    budget.edit_occurrence(entry.id, date(2026, 3, 31), date=date(2026, 4, 2))  # across the month boundary
    assert amounts(budget, date(2026, 3, 1), date(2026, 3, 31)) == []  # moved out of March
    assert amounts(budget, date(2026, 4, 1), date(2026, 4, 30)) == [  # moved into April (slot outside the range)
        (date(2026, 4, 2), 200000), (date(2026, 4, 30), 200000)]
    assert budget.totals(date(2026, 3, 1), date(2026, 4, 30)).occurrence_count == 2
    april = budget.occurrences(date(2026, 4, 1), date(2026, 4, 5))
    assert april[0].occurrence_date == date(2026, 3, 31)
    # Moving it back to its scheduled date is the same as not moving it.
    budget.edit_occurrence(entry.id, date(2026, 3, 31), date=date(2026, 3, 31))
    assert budget.list_overrides(entry.id) == []


def test_skip_and_restore(budget) -> None:
    entry = rent(budget)
    budget.skip_occurrence(entry.id, date(2026, 2, 1))
    assert amounts(budget, JAN, date(2026, 3, 31)) == [(date(2026, 1, 1), 120000), (date(2026, 3, 1), 120000)]
    # An edit of a skipped occurrence brings it back with the new values.
    budget.edit_occurrence(entry.id, date(2026, 2, 1), amount="1100")
    assert amounts(budget, date(2026, 2, 1), date(2026, 2, 1)) == [(date(2026, 2, 1), 110000)]
    assert budget.restore_occurrence(entry.id, date(2026, 2, 1)) is True
    assert budget.restore_occurrence(entry.id, date(2026, 2, 1)) is False
    assert amounts(budget, date(2026, 2, 1), date(2026, 2, 1)) == [(date(2026, 2, 1), 120000)]


def test_occurrence_edits_are_validated(budget) -> None:
    entry = salary(budget)
    one_time = budget.create_entry(kind="expense", name="TV", amount="400", category="Shopping",
                                   start_date=date(2026, 5, 1))
    with pytest.raises(BudgetValidationError, match="not a scheduled date"):
        budget.edit_occurrence(entry.id, date(2026, 3, 30), amount="1")
    with pytest.raises(BudgetValidationError, match="one-time entry"):
        budget.skip_occurrence(one_time.id, date(2026, 5, 1))
    with pytest.raises(BudgetValidationError, match="income category"):
        budget.edit_occurrence(entry.id, date(2026, 3, 31), category="Housing")
    with pytest.raises(BudgetValidationError, match="Unknown occurrence field"):
        budget.edit_occurrence(entry.id, date(2026, 3, 31), kind="expense")
    with pytest.raises(BudgetValidationError, match="never a float"):
        budget.edit_occurrence(entry.id, date(2026, 3, 31), amount=1.5)
    assert budget.list_overrides(entry.id) == []


def test_series_edits_follow_except_where_an_occurrence_was_edited(budget) -> None:
    entry = salary(budget)
    budget.edit_occurrence(entry.id, date(2026, 3, 31), amount="2500")
    budget.edit_occurrence(entry.id, date(2026, 4, 30), name="April pay")
    budget.update_entry(entry.id, amount="2100", name="Pay")
    march, april, may = budget.occurrences(date(2026, 3, 1), date(2026, 5, 31))
    assert (march.name, march.amount_minor) == ("Pay", 250000)
    assert (april.name, april.amount_minor) == ("April pay", 210000)
    assert (may.name, may.amount_minor) == ("Pay", 210000)


def test_rescheduling_a_series_drops_edits_of_dates_it_no_longer_has(budget) -> None:
    entry = salary(budget)
    budget.edit_occurrence(entry.id, date(2026, 3, 31), amount="2500")
    budget.skip_occurrence(entry.id, date(2026, 6, 30))
    budget.update_entry(entry.id, end_date=date(2026, 5, 31))  # June is gone, March stays
    assert [item.occurrence_date for item in budget.list_overrides(entry.id)] == [date(2026, 3, 31)]
    budget.update_entry(entry.id, start_date=date(2026, 1, 15))  # now the 15th: March 31 is no longer a date
    assert budget.list_overrides(entry.id) == []
    assert amounts(budget, date(2026, 3, 1), date(2026, 3, 31)) == [(date(2026, 3, 15), 200000)]


def test_a_kind_change_resets_occurrence_categories_and_deletion_removes_edits(connection, budget) -> None:
    entry = salary(budget)
    budget.edit_occurrence(entry.id, date(2026, 3, 31), category="Work 2", amount="10")
    budget.update_entry(entry.id, kind="expense", category="Other")
    (edit,) = budget.list_overrides(entry.id)
    assert (edit.category, edit.amount_minor) == (None, 1000)
    budget.delete_entry(entry.id)
    live = connection.execute("SELECT COUNT(*) FROM budget_overrides WHERE deleted_at IS NULL").fetchone()[0]
    assert live == 0
    assert connection.execute("SELECT COUNT(*) FROM budget_overrides").fetchone()[0] == 1  # a tombstone


# -- balance -----------------------------------------------------------------------


def test_the_balance_and_its_projection(budget) -> None:
    assert budget.current_balance() is None and budget.projected_balance(DEC) is None
    first = budget.set_balance("1000.10", date(2026, 1, 31))
    assert (first.amount_minor, first.as_of) == (100010, date(2026, 1, 31))
    salary(budget)  # Jan 31 (on the balance date: already included), Feb 28, Mar 31
    rent(budget)    # Feb 1, Mar 1
    assert budget.projected_balance(date(2026, 1, 31)) == 100010
    assert budget.projected_balance(date(2026, 3, 31)) == 100010 + 2 * 200000 - 2 * 120000
    with pytest.raises(BudgetValidationError, match="balance's date"):
        budget.projected_balance(date(2026, 1, 30))
    second = budget.set_balance(-250, date(2026, 2, 15))  # minor units; an overdraft is allowed
    assert second.id != first.id and budget.current_balance() == second  # each change is kept
    assert [item.amount_minor for item in budget.list_balances()] == [100010, -250]
    third = budget.set_balance("5", date(2026, 2, 15))  # the same day again: the later entry is current
    assert budget.current_balance() == third
    with pytest.raises(BudgetValidationError):
        budget.set_balance(10.5, date(2026, 2, 15))


# -- ownership -----------------------------------------------------------------------


def test_workspaces_are_isolated(connection) -> None:
    alice, bob, guest = service_for(connection, ALICE), service_for(connection, BOB), service_for(connection)
    entry = salary(alice)
    alice.set_balance("10", JAN)
    assert entry.user_id == ALICE.user_id
    assert bob.list_entries() == [] and guest.list_entries() == []
    assert bob.occurrences(JAN, DEC) == [] and bob.current_balance() is None
    for call in (lambda: bob.get_entry(entry.id), lambda: bob.update_entry(entry.id, amount="1"),
                 lambda: bob.delete_entry(entry.id), lambda: bob.skip_occurrence(entry.id, date(2026, 2, 28))):
        with pytest.raises(BudgetNotFoundError):
            call()
    assert len(alice.occurrences(JAN, DEC)) == 12
    stolen = entry.model_copy(update={"user_id": BOB.user_id})
    with pytest.raises(ScopeError):
        BudgetRepository(connection).scoped(ALICE).update("budget_entry", stolen, expected_version=1)


def test_the_ownerless_workspace_never_creates_account_records(connection) -> None:
    connection.execute(
        "INSERT INTO sync_accounts (account_key, backend_url, user_id, active, created_at) "
        "VALUES ('k', 'https://example.test', ?, 1, '2026-01-01T00:00:00+00:00')", (str(ALICE.user_id),))
    with pytest.raises(ScopeError, match="ownerless"):
        salary(service_for(connection))
    assert connection.execute("SELECT COUNT(*) FROM budget_entries").fetchone()[0] == 0


# -- separation from scheduling ----------------------------------------------------------


def test_scheduling_code_never_imports_the_budget_domain() -> None:
    root = Path(__file__).resolve().parents[2] / "app"
    scheduling = [root / name for name in ("optimizer.py", "reward.py", "mode_objectives.py")]
    scheduling += sorted((root / "planning").glob("*.py"))
    offenders = [path.name for path in scheduling if "app.budget" in path.read_text(encoding="utf-8")]
    assert offenders == []
