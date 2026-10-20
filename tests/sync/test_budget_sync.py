"""Budget records through synchronization (app/budget, the "budget" sync feature): a second device sees the same
balance, entries, occurrence edits and totals; deleting an entry removes its edits everywhere; concurrent edits of
one occurrence become an explicit conflict instead of a duplicate; a server without the feature keeps nothing
from being lost (the records wait and are uploaded after its upgrade); a client upgraded from a version without
budgets pulls the budget history it skipped; the ownerless workspace's budget is claimed only by association."""

from __future__ import annotations

from datetime import date

from app.budget.repository import BudgetRepository
from app.budget.service import BudgetService
from tests.sync.conftest import InProcessTransport

EMAIL = "alice@example.com"
JAN, DEC = date(2026, 1, 1), date(2026, 12, 31)


def budget(device) -> BudgetService:
    return BudgetService(BudgetRepository(device.connection).scoped(device.sync.workspace_scope()))


def summary(service: BudgetService) -> tuple:
    occurrences = [(item.date, item.name, item.amount_minor) for item in service.occurrences(JAN, DEC)]
    balance = service.current_balance()
    return occurrences, (balance.amount_minor, balance.as_of) if balance else None, service.totals(JAN, DEC)


class OlderServerTransport(InProcessTransport):
    """The same server, announcing no "budget" feature (and ignoring include_budget) until `upgraded` is set."""

    upgraded = False

    def capabilities(self, token: str) -> dict:
        answer = super().capabilities(token)
        if not self.upgraded:
            answer["features"] = [name for name in answer["features"] if name != "budget"]
        return answer

    def pull(self, token, after, limit):
        page = super().pull(token, after, limit)
        if not self.upgraded:  # an older client never asked for budget records
            page.changes[:] = [change for change in page.changes if not change["entity_type"].startswith("budget")]
        return page


def test_a_second_device_sees_the_same_budget_and_deletions_propagate(alice_server, make_device) -> None:
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in(EMAIL)
    b.sign_in(EMAIL)
    on_a = budget(a)
    on_a.set_balance("1500.25", date(2026, 1, 15))
    pay = on_a.create_entry(kind="income", name="Salary", amount="2000", category="Work 2",
                            start_date=date(2026, 1, 31), recurrence="monthly")
    rent = on_a.create_entry(kind="expense", name="Rent", amount="1200", category="Housing",
                             start_date=date(2026, 2, 1), recurrence="monthly", end_date=date(2026, 6, 1))
    on_a.edit_occurrence(pay.id, date(2026, 2, 28), amount="2500.10", date=date(2026, 3, 2))
    on_a.skip_occurrence(rent.id, date(2026, 4, 1))
    assert a.sync_now().status == "ok" and a.dirty() == []

    assert b.sync_now().status == "ok"
    on_b = budget(b)
    assert summary(on_b) == summary(on_a)
    assert on_b.projected_balance(date(2026, 6, 30)) == on_a.projected_balance(date(2026, 6, 30))
    assert alice_server.get(EMAIL, f"/budget-entries/{pay.id}")["amount_minor"] == 200000

    # A series edit on B reaches A; A's occurrence edit keeps its own amount.
    on_b.update_entry(pay.id, amount="2100")
    assert b.sync_now().status == "ok" and a.sync_now().status == "ok"
    assert summary(on_a) == summary(on_b)
    assert [item.amount_minor for item in on_a.occurrences(date(2026, 3, 1), date(2026, 3, 31))
            if item.name == "Salary"] == [250010, 210000]

    # Deleting the entry deletes its occurrence edits on the server and on the other device.
    on_a.delete_entry(rent.id)
    assert a.sync_now().status == "ok" and b.sync_now().status == "ok"
    assert [item.name for item in on_b.occurrences(JAN, DEC)].count("Rent") == 0
    assert [item.entry_id for item in on_b.repository.list("budget_override")] == [pay.id]
    assert a.sync.status().conflicts == 0 and b.sync.status().conflicts == 0


def test_concurrent_edits_of_one_occurrence_conflict_explicitly(alice_server, make_device) -> None:
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in(EMAIL)
    b.sign_in(EMAIL)
    pay = budget(a).create_entry(kind="income", name="Salary", amount="2000", category="Work 1",
                                 start_date=date(2026, 1, 31), recurrence="monthly")
    assert a.sync_now().status == "ok" and b.sync_now().status == "ok"
    budget(a).edit_occurrence(pay.id, date(2026, 3, 31), amount="2500")
    budget(b).edit_occurrence(pay.id, date(2026, 3, 31), amount="1000")  # offline, the same occurrence
    assert a.sync_now().status == "ok"
    b.sync_now()
    conflicts = b.sync.list_conflicts()
    assert conflicts and {conflict.entity_type for conflict in conflicts} == {"budget_override"}
    assert "already_exists" in {(conflict.error or {}).get("code") for conflict in conflicts}
    # Never two live edits of one occurrence, so it is never counted twice.
    assert len(budget(b).occurrences(date(2026, 3, 1), date(2026, 3, 31))) == 1
    for conflict in conflicts:
        b.sync.resolve_conflict(conflict.id, "accept_remote")
    assert b.sync_now().status == "ok"
    assert [item.amount_minor for item in budget(b).occurrences(date(2026, 3, 1), date(2026, 3, 31))] == [250000]
    assert b.sync.status().conflicts == 0


def test_a_server_without_budgets_never_loses_them_and_an_upgraded_client_pulls_the_history(
        alice_server, make_device) -> None:
    older = OlderServerTransport(alice_server.client)
    b = make_device("b", older)  # a device that synchronized before it knew about budgets
    b.sign_in(EMAIL)
    a = make_device("a", InProcessTransport(alice_server.client))
    a.sign_in(EMAIL)
    budget(a).set_balance("10", JAN)
    a.add_task("Write")
    assert a.sync_now().status == "ok"
    assert b.sync_now().status == "ok"
    assert budget(b).current_balance() is None  # skipped, and b's cursor moved past it

    # b creates budget records while its server (as b sees it) cannot take them: they wait, nothing is lost.
    entry = budget(b).create_entry(kind="expense", name="Gym", amount="30", category="Health",
                                   start_date=date(2026, 1, 5), recurrence="weekly")
    report = b.sync_now()
    assert report.status == "ok" and report.held == 1 and "budget" in report.message
    assert ("budget_entry", str(entry.id)) in [(kind, key) for kind, key, _ in b.dirty()]

    older.upgraded = True
    b.sync._capabilities = None  # a new session asks the server again
    upgraded = b.sync_now()
    assert upgraded.status == "ok" and upgraded.held == 0 and b.dirty() == []
    assert budget(b).current_balance().amount_minor == 1000  # the history it had skipped
    assert alice_server.get(EMAIL, f"/budget-entries/{entry.id}")["name"] == "Gym"
    assert a.sync_now().status == "ok"
    assert summary(budget(a)) == summary(budget(b))
    assert len(budget(b).occurrences(JAN, DEC)) == 52


def test_guest_budget_records_join_an_account_only_by_association(alice_server, make_device) -> None:
    device = make_device("a", InProcessTransport(alice_server.client))
    guest = budget(device)
    guest.create_entry(kind="income", name="Gift", amount="50", category="Other", start_date=JAN)
    device.sign_in(EMAIL, associate=False)
    assert device.sync_now().status == "ok"
    assert alice_server.get(EMAIL, "/budget-entries")["items"] == []
    assert budget(device).list_entries() == []  # the account workspace does not show guest records
    assert device.sync.association_preview().live_counts["budget_entry"] == 1
    device.sync.associate_local_data()
    assert device.sync_now().status == "ok"
    assert [item["name"] for item in alice_server.get(EMAIL, "/budget-entries")["items"]] == ["Gift"]
    assert [item.name for item in budget(device).list_entries()] == ["Gift"]


def test_future_items_synchronize_and_one_wish_becomes_one_expense(alice_server, make_device) -> None:
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in(EMAIL)
    b.sign_in(EMAIL)
    idea = budget(a).create_idea(name="Side gig", expected_amount="800", secured_amount="200")
    wish = budget(a).create_wish(name="Tent", amount="250", category="Travel")
    assert a.sync_now().status == "ok" and b.sync_now().status == "ok"
    assert [item.progress_percent for item in budget(b).list_ideas()] == [25]
    assert [item.name for item in budget(b).list_wishes()] == ["Tent"]
    assert budget(b).occurrences(JAN, DEC) == []  # a wish is no transaction anywhere

    # Both devices turn the same item into an expense while offline: one expense record, never two.
    budget(a).convert_wish(wish.id, date(2026, 7, 1))
    budget(b).convert_wish(wish.id, date(2026, 7, 2))
    assert a.sync_now().status == "ok"
    b.sync_now()
    for conflict in b.sync.list_conflicts():
        b.sync.resolve_conflict(conflict.id, "accept_remote")
    assert b.sync_now().status == "ok" and a.sync_now().status == "ok"
    assert len(alice_server.get(EMAIL, "/budget-entries")["items"]) == 1
    for device in (a, b):
        assert [(item.date, item.amount_minor) for item in budget(device).occurrences(JAN, DEC)] == [
            (date(2026, 7, 1), 25000)]
        assert budget(device).list_wishes()[0].converted_on == date(2026, 7, 1)
    budget(b).update_idea(idea.id, secured_amount="800")
    assert b.sync_now().status == "ok" and a.sync_now().status == "ok"
    assert budget(a).list_ideas()[0].reached
