"""Budget records in direct PostgreSQL mode (app/persistence/budget.py): the same BudgetService as the desktop's
SQLite path, writing through the server's mutation path -- server versions, change-log entries a synchronizing
client pulls, the server's validation, account isolation, and all-or-nothing units of work.

SQLite by default; PostgreSQL with BACKEND_TESTS_ON_POSTGRES=1 (tests/direct/conftest.py)."""

from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa

from app.budget.errors import BudgetConflictError, BudgetNotFoundError, BudgetValidationError
from app.persistence import errors
from app.planning.models import Project
from backend import models

JAN, DEC = date(2026, 1, 1), date(2026, 12, 31)


def changes(engine, user_id) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(sa.select(models.ChangeLogEntry.entity_type, models.ChangeLogEntry.operation)
                                  .where(models.ChangeLogEntry.user_id == user_id)
                                  .order_by(models.ChangeLogEntry.seq)).all()
    return [tuple(row) for row in rows]


def test_the_budget_service_works_on_the_server_schema(engine, alice) -> None:
    budget = alice.budget_service()
    project = alice.planning_service().create_project(Project(name="Thesis", user_id=alice.user_id))
    budget.set_balance("250.75", JAN)
    pay = budget.create_entry(kind="income", name="Salary", amount="2000", category="Work 3",
                              start_date=date(2026, 1, 31), recurrence="monthly", project_id=project.id)
    assert pay.user_id == alice.user_id and pay.version == 1
    budget.edit_occurrence(pay.id, date(2026, 2, 28), amount="2600", date=date(2026, 3, 1))
    budget.skip_occurrence(pay.id, date(2026, 4, 30))
    assert [(item.date, item.amount_minor) for item in budget.occurrences(date(2026, 2, 1), date(2026, 4, 30))] == [
        (date(2026, 3, 1), 260000), (date(2026, 3, 31), 200000)]
    assert budget.projected_balance(date(2026, 4, 30)) == 25075 + 200000 + 260000 + 200000

    updated = budget.update_entry(pay.id, expected_version=1, end_date=date(2026, 3, 31))  # April's edit goes
    assert updated.version == 2
    assert [item.occurrence_date for item in budget.list_overrides(pay.id)] == [date(2026, 2, 28)]
    with pytest.raises(BudgetConflictError):
        budget.update_entry(pay.id, expected_version=1, amount="1")

    budget.delete_entry(pay.id)
    with pytest.raises(BudgetNotFoundError):
        budget.get_entry(pay.id)
    log = changes(engine, alice.user_id)
    assert ("budget_balance", "upsert") in log and log[-2:] == [("budget_override", "delete"),
                                                                ("budget_entry", "delete")]


def test_validation_isolation_and_atomicity(engine, alice, bob) -> None:
    budget = alice.budget_service()
    with pytest.raises(BudgetValidationError, match="income category"):
        budget.create_entry(kind="income", name="Pay", amount="1", category="Housing", start_date=JAN)
    with pytest.raises(BudgetValidationError):
        budget.create_entry(kind="income", name="Pay", amount=1.5, category="Other", start_date=JAN)
    bobs_project = bob.planning_service().create_project(Project(name="Bob's", user_id=bob.user_id))
    with pytest.raises(BudgetValidationError, match="project"):
        budget.create_entry(kind="expense", name="Books", amount="9", category="Education", start_date=JAN,
                            project_id=bobs_project.id)
    entry = budget.create_entry(kind="expense", name="Gym", amount="30", category="Health", start_date=JAN,
                                recurrence="weekly")
    assert bob.budget_service().list_entries() == [] and bob.budget_service().occurrences(JAN, DEC) == []
    with pytest.raises(BudgetNotFoundError):
        bob.budget_service().delete_entry(entry.id)

    # A unit of work that fails part-way leaves nothing behind (one mutation, one transaction).
    budget.edit_occurrence(entry.id, date(2026, 1, 8), amount="35")
    before = changes(engine, alice.user_id)
    with pytest.raises(RuntimeError), budget.repository.transaction():
        budget.skip_occurrence(entry.id, date(2026, 1, 15))
        raise RuntimeError("interrupted")
    assert changes(engine, alice.user_id) == before
    assert [item.occurrence_date for item in budget.list_overrides(entry.id)] == [date(2026, 1, 8)]


def test_a_signed_out_session_refuses_budget_work(alice) -> None:
    budget = alice.budget_service()
    alice.sign_out()
    with pytest.raises(errors.NotSignedInError):
        budget.list_entries()


def test_future_items_and_conversion_in_direct_mode(engine, alice, bob) -> None:
    budget = alice.budget_service()
    idea = budget.create_idea(name="Raise", expected_amount="300")
    assert budget.update_idea(idea.id, secured_amount="150").progress_percent == 50
    wish = budget.create_wish(name="Chair", amount="120", category="Shopping")
    assert budget.occurrences(JAN, DEC) == []
    converted, entry = budget.convert_wish(wish.id, date(2026, 5, 5))
    assert converted.converted_entry_id == entry.id and converted.version == 2
    assert [(item.date, item.amount_minor) for item in budget.occurrences(JAN, DEC)] == [(date(2026, 5, 5), 12000)]
    with pytest.raises(BudgetConflictError):
        budget.convert_wish(wish.id, date(2026, 5, 6))
    assert ("budget_wish", "upsert") in changes(engine, alice.user_id)
    assert bob.budget_service().list_wishes() == [] and bob.budget_service().list_ideas() == []
