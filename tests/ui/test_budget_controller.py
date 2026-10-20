"""BudgetController (app/ui/budget_controller.py), Tk-free, over a temporary database: form text becomes service
calls, errors become ControllerResult failures with nothing saved, series and single-occurrence edits and deletes
are distinguished, and the Day page's view and amount edits handle several and recurring transactions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.budget.repository import BudgetRepository
from app.budget.service import BudgetService
from app.execution.db import get_connection
from app.persistence.errors import NotSignedInError
from app.planning.application import PlanningService
from app.planning.models import Project
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope
from app.ui.budget_controller import NO_PROJECT, OCCURRENCE, SERIES, BudgetController, EntryForm

TODAY = date(2026, 3, 10)


@pytest.fixture
def connection(tmp_path: Path):
    conn = get_connection(tmp_path / "budget.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def service(connection) -> BudgetService:
    return BudgetService(BudgetRepository(connection).scoped(OwnerScope.ownerless()))


@pytest.fixture
def planning(connection) -> PlanningService:
    return PlanningService(PlanningRepository(connection)).scoped(OwnerScope.ownerless())


@pytest.fixture
def controller(service, planning) -> BudgetController:
    return BudgetController(lambda: service, projects=planning.list_projects, today=lambda: TODAY)


def form(**fields) -> EntryForm:
    data = {"kind": "expense", "name": "Rent", "amount": "1200", "date": "2026-03-01", "category": "Housing"} | fields
    return EntryForm(**data)


def test_balance_and_entries_round_trip(controller, planning) -> None:
    empty = controller.overview()
    assert empty.ok and empty.value.balance is None and empty.value.income == empty.value.expenses == []
    assert empty.value.balance_text == "No balance entered yet."
    assert controller.save_balance(" 250.5 ").ok
    project = planning.create_project(Project(name="Thesis"))
    assert controller.save_entry(form(repeat="Monthly", project="Thesis")).ok
    assert controller.save_entry(form(kind="income", name="Bonus", amount="99.99", category="Work 2",
                                      date="2026-03-15")).ok
    overview = controller.overview().value
    assert overview.balance_text == "250.50, set on 2026-03-10"  # saved as today's balance
    assert overview.projects == [("Thesis", project.id)]
    (rent,) = overview.expenses
    assert (rent.name, rent.amount_text, rent.schedule_text, rent.project_name) == (
        "Rent", "1200.00", "Monthly from 2026-03-01", "Thesis")
    (bonus,) = overview.income
    assert (bonus.schedule_text, bonus.recurring) == ("On 2026-03-15", False)


@pytest.mark.parametrize(("fields", "message"), [
    ({"amount": "12.345"}, "decimal places"), ({"amount": "abc"}, "not an amount"),
    ({"date": "03/01/2026"}, "date like"), ({"category": "Work 1"}, "not an expense category"),
    ({"name": " "}, "cannot be empty"), ({"project": "Gone"}, "no longer exists"),
])
def test_invalid_forms_fail_with_a_message_and_save_nothing(controller, service, fields, message) -> None:
    result = controller.save_entry(form(**fields))
    assert not result.ok and message in result.error
    assert service.list_entries() == []
    assert not controller.save_balance("1.5.0").ok and service.current_balance() is None


def test_series_and_occurrence_edits_and_deletes(controller, service) -> None:
    controller.save_entry(form(repeat="Monthly"))
    entry = service.list_entries()[0]
    # One occurrence: only what differs from the series is stored.
    result = controller.save_entry(form(entry_id=entry.id, scope=OCCURRENCE, date="2026-04-01", amount="1300"))
    assert result.ok
    (edit,) = service.list_overrides(entry.id)
    assert (edit.amount_minor, edit.name, edit.category, edit.project_id) == (130000, None, None, None)
    assert controller.overview().value.expenses[0].edited_occurrences == 1
    # The entire series: the edited occurrence keeps its own amount.
    assert controller.save_entry(form(entry_id=entry.id, scope=SERIES, name="Flat", amount="1250",
                                      repeat="Monthly")).ok
    amounts = [(item.date, item.name, item.amount_minor) for item in service.occurrences(date(2026, 3, 1),
                                                                                          date(2026, 5, 31))]
    assert amounts == [(date(2026, 3, 1), "Flat", 125000), (date(2026, 4, 1), "Flat", 130000),
                       (date(2026, 5, 1), "Flat", 125000)]
    assert not controller.save_entry(form(entry_id=entry.id, scope=OCCURRENCE, date="2026-04-02")).ok
    # Delete one occurrence, then the whole series.
    assert controller.delete_entry(entry.id, scope=OCCURRENCE, occurrence_date="2026-05-01").ok
    assert [item.date for item in service.occurrences(date(2026, 5, 1), date(2026, 5, 31))] == []
    assert controller.delete_entry(entry.id, scope=SERIES).ok
    assert service.list_entries() == [] and controller.overview().value.expenses == []


def test_making_a_series_one_time_and_choosing_defaults(controller, service) -> None:
    controller.save_entry(form(repeat="Weekly", date="2026-03-02"))
    entry = service.list_entries()[0]
    assert controller.next_occurrence(entry) == date(2026, 3, 16)  # the first one from today
    assert controller.save_entry(form(entry_id=entry.id, date="2026-03-02", repeat=None, project=NO_PROJECT)).ok
    one_time = service.get_entry(entry.id)
    assert not one_time.is_recurring and controller.next_occurrence(one_time) == date(2026, 3, 2)


def test_the_day_view_and_its_amount_edits(controller, service) -> None:
    day = date(2026, 3, 1)
    controller.save_entry(form(repeat="Monthly"))
    controller.save_entry(form(name="Groceries", amount="40.10", category="Groceries"))
    controller.save_entry(form(kind="income", name="Gift", amount="20", category="Other"))
    view = controller.day(day).value
    assert (view.income_text, view.expense_text) == ("20.00", "1240.10")
    assert [item.name for item in view.expenses] == ["Groceries", "Rent"] and not view.empty
    rent = next(item for item in view.expenses if item.name == "Rent")
    groceries = next(item for item in view.expenses if item.name == "Groceries")

    changed = controller.set_day_amounts(day, {rent.key: "1100", groceries.key: "45"})
    assert changed.ok and changed.value.expense_text == "1145.00"
    # The recurring rent changed for this occurrence only; the one-time groceries entry itself changed.
    assert [item.amount_minor for item in service.occurrences(date(2026, 4, 1), date(2026, 4, 1))] == [120000]
    assert {entry.name: entry.amount_minor for entry in service.list_entries()}["Groceries"] == 4500

    # Back to the series' amount: the occurrence follows the series again.
    assert controller.set_day_amounts(day, {rent.key: "1200"}).ok
    assert service.list_overrides(rent.occurrence.entry_id) == []

    # All or nothing: one bad amount saves none of them.
    failed = controller.set_day_amounts(day, {rent.key: "900", groceries.key: "-1"})
    assert not failed.ok and controller.day(day).value.expense_text == "1245.00"
    assert controller.day(date(2026, 3, 2)).value.empty


def test_signed_out_storage_says_so(controller) -> None:
    def unavailable():
        raise NotSignedInError()

    signed_out = BudgetController(unavailable, today=lambda: TODAY)
    for result in (signed_out.overview(), signed_out.day(TODAY), signed_out.save_entry(form())):
        assert not result.ok and isinstance(result.cause, NotSignedInError) and "Sign in" in result.error


def test_stats_are_worded_from_the_stats_service(controller, service) -> None:
    empty = controller.stats().value
    assert [tile.value for tile in empty.tiles] == ["–", "–"] and empty.history_note.startswith("No history yet")
    assert empty.weekly == empty.monthly == empty.daily == [] and empty.forecast_points == []
    assert empty.forecast_summary.startswith("No forecast yet") and empty.assumptions == []

    controller.save_balance("100")
    controller.save_entry(form(name="Snacks", amount="150", date="2026-03-10", category="Dining"))
    view = controller.stats().value
    assert view.tiles[1].value == "150.00" and view.tiles[1].detail == "Tue, Mar 10, 2026"
    assert view.tiles[0].detail == "No such day recorded yet."
    assert view.daily == [("10", 15000)] and view.monthly == [("Mar 2026", 15000)]
    # 100.00 at the end of today; 10.00 a day of everyday spending reaches zero on the tenth day.
    assert view.forecast_summary == "Projected to reach zero or below on Fri, Mar 20, 2026 (in 10 days)."
    assert any("10.00 per day" in line for line in view.assumptions)
    assert len(view.forecast_points) == 90 and view.forecast_points[9] == (date(2026, 3, 20), 0)


def test_future_rows_progress_and_conversion(controller, service) -> None:
    empty = controller.future().value
    assert empty.ideas == empty.wishes == empty.converted == []
    assert controller.save_idea(None, name="Course", expected="500").ok
    idea = service.list_ideas()[0]
    assert controller.save_idea(idea.id, name="Course", expected="500", description="Teach", secured="125").ok
    (row,) = controller.future().value.ideas
    assert (row.status, row.progress_percent, row.progress_fraction) == ("In progress", 25, 0.25)
    assert row.progress_text == "125.00 of 500.00 received or secured (25%); 375.00 to go"
    assert not controller.save_idea(idea.id, name="Course", expected="500", secured="-1").ok

    assert not controller.save_wish(None, name="Lamp", amount="40", category="Work 1").ok
    assert controller.save_wish(None, name="Lamp", amount="40", category="Shopping", description="Desk lamp").ok
    wish = service.list_wishes()[0]
    assert controller.overview().value.expenses == [] and controller.day(TODAY).value.empty
    assert not controller.convert_wish(wish.id, "tomorrow").ok
    assert controller.convert_wish(wish.id, "2026-03-10").ok
    view = controller.future().value
    assert view.wishes == [] and [row.converted_text for row in view.converted] == [
        "Turned into an expense on 2026-03-10"]
    assert controller.day(TODAY).value.expense_text == "40.00"
    second = controller.convert_wish(wish.id, "2026-03-11")
    assert not second.ok and "already turned into an expense" in second.error
    assert controller.delete_idea(idea.id).ok and controller.future().value.ideas == []


def test_a_controller_stays_in_the_workspace_it_was_built_for(tmp_path: Path) -> None:
    import uuid

    from app.ui import background
    from app.ui.app_services import open_app_services

    previous = background.current_registry()
    services = open_app_services(tmp_path / "app.db", timezone="UTC", project_root=str(tmp_path),
                                 background_sync=False)
    try:
        guest = services.budget_controller(lambda: TODAY)
        services.switch_workspace(OwnerScope.account(uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")))
        account = services.budget_controller(lambda: TODAY)
        # A write started before the switch still lands in the guest workspace, never in the account's.
        assert guest.save_entry(form(name="Guest rent")).ok
        assert account.save_entry(form(name="Account rent")).ok
        assert [row.name for row in guest.overview().value.expenses] == ["Guest rent"]
        assert [row.name for row in account.overview().value.expenses] == ["Account rent"]
        assert services.budget_service().list_entries()[0].name == "Account rent"
    finally:
        services.close()
        background.install_registry(previous)


def test_history_is_filtered_but_balances_come_from_everything(controller, service, planning) -> None:
    project = planning.create_project(Project(name="Thesis"))
    controller.save_entry(form(name="Books", amount="30", date="2026-01-05", category="Education",
                               project="Thesis"))
    service.set_balance("1000", date(2026, 3, 1))
    controller.save_entry(form(name="Rent", amount="500", date="2026-03-02", repeat="Monthly"))
    controller.save_entry(form(kind="income", name="Pay", amount="200", date="2026-03-05", category="Work 1"))
    controller.save_balance("690")  # today, 2026-03-10: calculated 700, corrected by -10
    everything = controller.history("All history").value
    assert [line.type_text for line in everything.lines] == [
        "Balance changed manually", "Income", "Expense (repeating)", "Balance changed manually", "Expense"]
    latest = everything.lines[0]
    assert (latest.amount_text, latest.balance_text, latest.detail) == ("690.00", "690.00",
                                                                        "was 700.00 by calculation; -10.00")
    assert everything.lines[1].balance_text == "700.00" and everything.lines[-1].balance_text == "–"
    assert everything.lines[-1].detail == "Thesis" and everything.projects == ["Thesis"]
    assert everything.categories == ["Education", "Housing", "Work 1"]
    assert everything.summary == ("3 transaction(s): income 200.00, expenses 530.00, net -330.00; "
                                  "2 manual balance change(s).")
    recent = controller.history("Last 30 days", "Expenses").value
    assert [line.name for line in recent.lines] == ["Rent"] and recent.lines[0].balance_text == "500.00"
    assert [line.name for line in controller.history("All history", category="Education").value.lines] == ["Books"]
    assert [line.name for line in controller.history("All history", project="Thesis").value.lines] == ["Books"]
    assert len(controller.history("All history", "Balance changes").value.lines) == 2
    assert project.id is not None
