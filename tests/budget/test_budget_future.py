"""Budgeting > Future in the domain (app/budget): income ideas and their progress (received or secured against
expected), wishlist items without a date, their persistence per workspace, the one-time conversion into exactly
one expense, and their exclusion from every financial figure before conversion."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest

from app.budget.errors import BudgetConflictError, BudgetNotFoundError, BudgetValidationError
from app.budget.models import wish_expense_id
from app.budget.repository import BudgetRepository
from app.budget.service import BudgetService
from app.budget.stats import balance_forecast, budget_statistics
from app.execution.db import get_connection
from app.planning.scope import OwnerScope

TODAY = date(2026, 6, 15)
ALICE = OwnerScope.account(uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))


@pytest.fixture
def connection(tmp_path: Path):
    conn = get_connection(tmp_path / "future.db")
    try:
        yield conn
    finally:
        conn.close()


def service_for(connection, scope=OwnerScope.ownerless()) -> BudgetService:
    return BudgetService(BudgetRepository(connection).scoped(scope))


@pytest.fixture
def budget(connection) -> BudgetService:
    return service_for(connection)


def test_income_ideas_and_their_progress(budget, connection) -> None:
    idea = budget.create_idea(name="Freelance site", expected_amount="1200", description="  For Ana  ")
    assert (idea.secured_amount_minor, idea.progress_percent, idea.remaining_minor, idea.reached) == (0, 0, 120000,
                                                                                                       False)
    assert idea.description == "For Ana"
    halfway = budget.update_idea(idea.id, secured_amount="600")
    assert (halfway.progress_percent, halfway.remaining_minor, halfway.version) == (50, 60000, 2)
    third = budget.update_idea(idea.id, secured_amount="400.01")
    assert third.progress_percent == 33  # rounded down: never shows more than was secured
    beyond = budget.update_idea(idea.id, secured_amount="1500", description="")
    assert (beyond.progress_percent, beyond.remaining_minor, beyond.reached, beyond.description) == (125, 0, True,
                                                                                                      None)
    for bad in ({"secured_amount": "-1"}, {"expected_amount": "0"}, {"expected_amount": 12.5}, {"name": ""},
                {"description": "x" * 4001}, {"colour": "red"}):
        with pytest.raises(BudgetValidationError):
            budget.update_idea(idea.id, **bad)
    budget.delete_idea(idea.id)
    assert budget.list_ideas() == []
    with pytest.raises(BudgetNotFoundError):
        budget.update_idea(idea.id, name="Gone")
    # Persisted: a fresh service on the same database reads it back (a tombstone, not a hard delete).
    assert connection.execute("SELECT COUNT(*) FROM budget_ideas WHERE deleted_at IS NOT NULL").fetchone()[0] == 1


def test_wishlist_items_are_validated_and_editable_until_converted(budget) -> None:
    with pytest.raises(BudgetValidationError, match="not an expense category"):
        budget.create_wish(name="Bike", amount="400", category="Work 1")
    with pytest.raises(BudgetValidationError, match="project"):
        budget.create_wish(name="Bike", amount="400", category="Transport", project_id=uuid.uuid4())
    wish = budget.create_wish(name="Bike", amount="400", category="Transport", description="Gravel")
    edited = budget.update_wish(wish.id, amount="380.50", category="Shopping")
    assert (edited.amount_minor, edited.category, edited.converted) == (38050, "Shopping", False)
    assert [item.name for item in budget.list_wishes(include_converted=False)] == ["Bike"]


def test_future_items_count_nowhere_until_converted(budget) -> None:
    budget.set_balance("1000", TODAY)
    budget.create_idea(name="Bonus", expected_amount="5000", secured_amount="5000")
    wish = budget.create_wish(name="Camera", amount="900", category="Shopping")
    assert budget.occurrences(date(2026, 1, 1), date(2026, 12, 31)) == []
    assert budget.totals(date(2026, 1, 1), date(2026, 12, 31)).occurrence_count == 0
    assert not budget_statistics(budget, TODAY).has_history
    forecast = balance_forecast(budget, TODAY)
    assert {day.balance_minor for day in forecast.days} == {100000} and forecast.zero_day is None
    assert budget.projected_balance(date(2026, 9, 1)) == 100000

    converted, entry = budget.convert_wish(wish.id, date(2026, 6, 20))
    assert converted.converted and converted.converted_on == date(2026, 6, 20)
    assert converted.converted_entry_id == entry.id == wish_expense_id(wish.id)
    assert (entry.kind.value, entry.name, entry.amount_minor, entry.category, entry.is_recurring) == (
        "expense", "Camera", 90000, "Shopping", False)
    # Exactly one expense, counted once, on its date.
    assert [(item.date, item.amount_minor) for item in budget.occurrences(date(2026, 1, 1), date(2026, 12, 31))] == [
        (date(2026, 6, 20), 90000)]
    assert balance_forecast(budget, TODAY).days[4].one_time_expense_minor == 90000
    assert budget.projected_balance(date(2026, 9, 1)) == 10000

    # It cannot be converted (or edited) again; deleting the item keeps the expense.
    with pytest.raises(BudgetConflictError, match="already turned into an expense"):
        budget.convert_wish(wish.id, date(2026, 7, 1))
    with pytest.raises(BudgetConflictError):
        budget.update_wish(wish.id, amount="1")
    assert len(budget.list_entries()) == 1 and budget.list_wishes(include_converted=False) == []
    budget.delete_wish(wish.id)
    assert budget.list_wishes() == [] and len(budget.list_entries()) == 1


def test_a_failed_conversion_changes_nothing(budget) -> None:
    wish = budget.create_wish(name="Desk", amount="300", category="Shopping")
    with pytest.raises(BudgetValidationError):
        budget.convert_wish(wish.id, "someday")
    with pytest.raises(BudgetValidationError):
        budget.convert_wish(wish.id, date(1800, 1, 1))
    assert budget.list_entries() == [] and not budget.list_wishes()[0].converted


def test_future_items_stay_in_their_workspace(connection) -> None:
    guest, alice = service_for(connection), service_for(connection, ALICE)
    idea = alice.create_idea(name="Raise", expected_amount="100")
    wish = alice.create_wish(name="Phone", amount="700", category="Shopping")
    assert guest.list_ideas() == [] and guest.list_wishes() == []
    with pytest.raises(BudgetNotFoundError):
        guest.convert_wish(wish.id, TODAY)
    with pytest.raises(BudgetNotFoundError):
        guest.update_idea(idea.id, secured_amount="1")
    assert [item.user_id for item in alice.list_ideas() + alice.list_wishes()] == [ALICE.user_id] * 2
