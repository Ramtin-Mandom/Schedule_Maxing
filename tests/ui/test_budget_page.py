"""The Budgeting page and the Day page's money widgets as real windows against a temporary database: General
saves the balance and income/expense entries (repeating ones too), edits one occurrence or the entire series,
deletes after asking which; the Day page shows the date's transactions under To Do and its ↑/↓ totals beside the
points, a double-click opens them to change amounts (recurring occurrences only for that date), the strip scrolls
sideways only when it must, and everything works with no budget data and at a narrow width. Skipped without a
display (see tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.budget.models import BudgetKind
from app.ui.budget_controller import OCCURRENCE, SERIES
from app.ui.day_money import DayMoneyDialog
from tests.ui.test_desktop_app import WEDNESDAY, close_app, open_app, pump
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display
from tests.window_placement import place


def budget_page(app):
    app.show_page("budget")
    pump(app)
    return app.pages["budget"].content


def fill(panel, *, name: str, amount: str, day: str, category: str | None = None, repeat: str | None = None) -> None:
    panel.name_field.variable.set(name)
    panel.amount_field.variable.set(amount)
    panel.date_field.variable.set(day)
    if category is not None:
        panel.category_field.variable.set(category)
    panel.repeat_var.set(repeat is not None)
    panel._show_repeat()
    if repeat is not None:
        panel.frequency_field.variable.set(repeat)


def service(app):
    return app.services.budget_service()


def test_general_saves_edits_and_deletes(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "budget.db", tmp_path)
    try:
        page = budget_page(app)
        assert page.section == "General" and page.balance_label.cget("text") == "No balance entered yet."
        assert [row for panel in page.panels.values() for row in panel.rows] == []

        assert not hasattr(page, "balance_date")  # the balance has no date field: it is today's
        page.balance_amount.variable.set("1500.25")
        page.balance_button.invoke()
        pump(app)
        assert page.balance_label.cget("text") == "1500.25, set on 2024-06-05"

        income, expenses = page.panels[BudgetKind.INCOME], page.panels[BudgetKind.EXPENSE]
        assert income.category_field.values == ["Work 1", "Work 2", "Work 3", "Other"]
        assert not income.frequency_field.winfo_ismapped()
        fill(income, name="Salary", amount="2000", day="2024-06-05", category="Work 2", repeat="Monthly")
        assert income.frequency_field.winfo_manager() == "grid"  # "Repeats" reveals the choices
        income.save_button.invoke()
        pump(app)
        fill(expenses, name="Rent", amount="1200", day="2024-06-01", category="Housing", repeat="Monthly")
        expenses.save_button.invoke()
        pump(app)
        fill(expenses, name="Coffee", amount="3.5", day="2024-06-05", category="Dining")
        expenses.save_button.invoke()
        pump(app)
        assert [row["row"].name for row in income.rows] == ["Salary"]
        assert [row["row"].name for row in expenses.rows] == ["Rent", "Coffee"]
        assert income.name_field.get() == "" and dialogs.errors == []

        # An invalid amount is refused with a message; nothing is added.
        fill(expenses, name="Bad", amount="1.234", day="2024-06-05", category="Dining")
        expenses.save_button.invoke()
        pump(app)
        assert "decimal places" in expenses.error_label.cget("text") and len(expenses.rows) == 2

        # Edit one occurrence of the rent, then the entire series.
        rent_row = expenses.rows[0]
        rent_row["edit"].invoke()
        assert expenses.scope_frame.winfo_manager() == "grid" and expenses.scope_var.get() == SERIES
        expenses.occurrence_radio.invoke()
        assert expenses.date_field.get() == "2024-07-01"  # the next occurrence from today
        expenses.amount_field.variable.set("1300")
        expenses.save_button.invoke()
        pump(app)
        july = [(item.name, item.amount_minor) for item in service(app).occurrences(date(2024, 7, 1), date(2024, 7, 1))]
        assert july == [("Rent", 130000)]
        expenses.rows[0]["edit"].invoke()
        expenses.amount_field.variable.set("1250")
        expenses.save_button.invoke()
        pump(app)
        amounts = [item.amount_minor for item in service(app).occurrences(date(2024, 6, 1), date(2024, 8, 1))
                   if item.name == "Rent"]
        assert amounts == [125000, 130000, 125000]

        # Deleting a repeating entry asks which: one occurrence, then the entire series.
        expenses.rows[0]["delete"].invoke()
        dialog = page.dialog
        assert dialog.scope_var.get() == OCCURRENCE and dialog.date_field.get() == "2024-07-01"
        dialog.primary_button.invoke()
        pump(app)
        assert [item.name for item in service(app).occurrences(date(2024, 7, 1), date(2024, 7, 1))] == []
        expenses.rows[0]["delete"].invoke()
        page.dialog.series_radio.invoke()
        page.dialog.primary_button.invoke()
        pump(app)
        assert [row["row"].name for row in expenses.rows] == ["Coffee"]
        # A one-time entry is confirmed, then deleted.
        expenses.rows[0]["delete"].invoke()
        page.dialog.primary_button.invoke()
        pump(app)
        assert expenses.rows == [] and expenses.empty_label.winfo_manager() == "grid"
    finally:
        close_app(app)


def test_the_day_page_shows_and_edits_the_dates_transactions(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "day.db", tmp_path)
    try:
        day = app.pages["day"]
        assert day.money_strip.winfo_manager() == "" and day.money_totals.text(BudgetKind.INCOME) == "↑ 0.00"
        day.open_money(BudgetKind.INCOME)  # no data: an empty list that only closes
        assert day.money_dialog.items == []
        day.money_dialog.cancel()

        budget = service(app)
        budget.create_entry(kind="income", name="Salary", amount="2000", category="Work 1", start_date=WEDNESDAY,
                            recurrence="weekly")
        budget.create_entry(kind="expense", name="Lunch", amount="12.40", category="Dining", start_date=WEDNESDAY)
        budget.create_entry(kind="expense", name="Gym", amount="30", category="Health", start_date=WEDNESDAY,
                            recurrence="daily")
        day.reload()
        pump(app)
        assert day.money_totals.text(BudgetKind.INCOME) == "↑ 2000.00"
        assert day.money_totals.text(BudgetKind.EXPENSE) == "↓ 42.40"
        assert day.money_totals.grid_info()["column"] == 0 and day.points_label.grid_info()["column"] == 1
        assert [(card["kind"], card["name"].cget("text"), card["amount"].cget("text"))
                for card in day.money_strip.cards] == [
            (BudgetKind.INCOME, "Salary", "+2000.00"), (BudgetKind.EXPENSE, "Gym", "−30.00"),
            (BudgetKind.EXPENSE, "Lunch", "−12.40")]

        # Double-clicking the expense total opens the day's expenses; changed amounts are saved together.
        day.open_money(BudgetKind.EXPENSE)
        dialog: DayMoneyDialog = day.money_dialog
        assert [item.name for item in dialog.items] == ["Gym", "Lunch"]
        fields = {item.name: field for item, field in dialog.fields.values()}
        fields["Gym"].variable.set("25")
        fields["Lunch"].variable.set("10")
        dialog.save()
        pump(app)
        assert not dialog.winfo_exists() and day.money_totals.text(BudgetKind.EXPENSE) == "↓ 35.00"
        assert [card["amount"].cget("text") for card in day.money_strip.cards[1:]] == ["−25.00", "−10.00"]
        tomorrow = [(item.name, item.amount_minor) for item in budget.occurrences(date(2024, 6, 6), date(2024, 6, 6))]
        assert tomorrow == [("Gym", 3000)]  # the recurring item changed for that date only

        # A bad amount keeps the dialog open with the reason; nothing changes.
        day.open_money(BudgetKind.EXPENSE)
        next(iter(day.money_dialog.fields.values()))[1].variable.set("abc")
        day.money_dialog.save()
        pump(app)
        assert "not an amount" in day.money_dialog.error_label.cget("text")
        day.money_dialog.cancel()
        assert day.money_totals.text(BudgetKind.EXPENSE) == "↓ 35.00"

        # Many transactions scroll sideways only when they are wider than the section; a narrow window works.
        app.deiconify()
        app.geometry(place("1440x880+30+30"))
        pump(app)
        assert not day.money_strip.needs_scrolling()
        for index in range(14):
            budget.create_entry(kind="expense", name=f"Snack {index}", amount="1", category="Dining",
                                start_date=WEDNESDAY)
        app.geometry(place("700x800+30+30"))
        day.reload()
        pump(app)
        app.update()
        day.money_strip._update_scrollbar()
        assert len(day.money_strip.cards) == 17 and day.money_strip.needs_scrolling()
        assert day.money_strip.area._scrollbar.winfo_manager() == "grid"
        assert day.score_box.grid_info()["row"] == 1  # the narrow header puts the totals on their own line
        assert dialogs.errors == []
    finally:
        close_app(app)


def test_stats_show_records_charts_and_the_forecast(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "stats.db", tmp_path)
    try:
        page = budget_page(app)
        page.section_buttons["Stats"].invoke()
        pump(app)
        assert page.section == "Stats" and page.history_label.cget("text").startswith("No history yet")
        assert page.weekly_chart.find_withtag("empty") and page.forecast_chart.find_withtag("empty")

        budget = service(app)
        budget.set_balance("100", WEDNESDAY)
        budget.create_entry(kind="expense", name="Snacks", amount="150", category="Dining", start_date=WEDNESDAY)
        budget.create_entry(kind="income", name="Pay", amount="40", category="Work 1", start_date=WEDNESDAY)
        page.refresh()
        pump(app)
        assert [labels["value"].cget("text") for labels in page.stat_tiles] == ["40.00", "150.00"]
        assert len(page.daily_chart.find_withtag("bar")) == 1 and page.monthly_chart.rows == [("Jun 2024", 15000)]
        assert page.forecast_summary.cget("text").startswith("Projected to reach zero or below on")
        assert page.forecast_chart.find_withtag("balance") and page.forecast_chart.find_withtag("zero_day")
        assert "Everyday spending" in page.assumptions_label.cget("text")
        assert dialogs.errors == []
    finally:
        close_app(app)


def test_future_ideas_progress_wishlist_and_conversion(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "future.db", tmp_path)
    try:
        page = budget_page(app)
        page.section_buttons["Future"].invoke()
        pump(app)
        future = page.future
        assert future.idea_empty.winfo_manager() == "grid" and future.wish_empty.winfo_manager() == "grid"
        assert future.detail.winfo_manager() == ""  # nothing selected

        future.idea_name.variable.set("Tutoring")
        future.idea_expected.variable.set("600")
        future.add_idea_button.invoke()
        pump(app)
        future.idea_rows[0]["button"].invoke()
        assert future.detail.winfo_manager() == "grid" and future.detail_secured.get() == "0.00"
        future.detail_secured.variable.set("150")
        future.detail_description.insert("1.0", "Two students")
        future.save_idea_button.invoke()
        pump(app)
        assert future.selected_idea.progress_percent == 25 and abs(future.progress_bar.get() - 0.25) < 1e-6
        assert "150.00 of 600.00 received or secured (25%)" in future.progress_label.cget("text")
        assert service(app).list_ideas()[0].description == "Two students"

        future.wish_name.variable.set("Monitor")
        future.wish_amount.variable.set("199.99")
        future.wish_category.variable.set("Shopping")
        future.save_wish_button.invoke()
        pump(app)
        (row,) = future.wish_rows
        assert service(app).occurrences(date(2024, 1, 1), date(2024, 12, 31)) == []  # counts nowhere yet
        row["convert"].invoke()
        dialog = page.dialog
        assert dialog.date_field.get() == WEDNESDAY.isoformat()
        dialog.primary_button.invoke()
        pump(app)
        assert future.wish_rows == [] and "Monitor · 199.99 · Turned into an expense on 2024-06-05" in \
            future.converted_label.cget("text")
        assert [(item.date, item.amount_minor) for item in service(app).occurrences(WEDNESDAY, WEDNESDAY)] == [
            (WEDNESDAY, 19999)]
        # The Day page shows the new expense when it is next shown.
        app.show_page("day")
        pump(app)
        assert app.pages["day"].money_totals.text(BudgetKind.EXPENSE) == "↓ 199.99"
        assert dialogs.errors == []
    finally:
        close_app(app)


def test_stats_history_lists_transactions_and_balance_changes(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "history.db", tmp_path)
    try:
        page = budget_page(app)
        budget = service(app)
        budget.create_entry(kind="expense", name="Groceries", amount="40", category="Groceries",
                            start_date=date(2024, 6, 3))
        budget.create_entry(kind="income", name="Pay", amount="100", category="Work 1", start_date=date(2024, 6, 4))
        page.balance_amount.variable.set("1000")
        page.balance_button.invoke()
        pump(app)
        page.section_buttons["Stats"].invoke()
        pump(app)
        table = page.history_table
        rows = [table.item(item, "values") for item in table.get_children()]
        assert [row[1] for row in rows] == ["Balance changed manually", "Income", "Expense"]
        assert rows[0][4:7] == ("1000.00", "1000.00", "first balance entered") and rows[1][5] == "–"
        assert "2 transaction(s)" in page.history_summary.cget("text")
        page.history_type.choose("Expenses")
        pump(app)
        assert [table.item(item, "values")[2] for item in table.get_children()] == ["Groceries"]
        assert table.item(table.get_children()[0], "tags") == ("expense",)
        assert dialogs.errors == []
    finally:
        close_app(app)
