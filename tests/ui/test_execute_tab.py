"""The Day page's Execute tab as real widgets (Milestone 5) against a temporary database:
viewing a saved schedule writes no execution; Start -> Pause -> Resume -> Finish, Skip,
Cancel and Reschedule run through the buttons; a double click creates nothing twice;
started work explains why it cannot be moved; overdue work is shown as such; the state
survives a restart; and the Productivity page shows the new sections with honest
storage wording. Skipped without a display (see tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

import pytest

from tests.ui.test_desktop_app import WEDNESDAY, close_app, fill_form, open_app, pump
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display


def rows(app, table: str) -> list[tuple]:
    return [tuple(row) for row in app.services.connection.execute(f"SELECT * FROM {table} ORDER BY 1")]


@pytest.fixture(autouse=True)
def instant_feedback(monkeypatch):
    """The Finish/Skip feedback dialog submits a note at once."""
    import app.ui.execution_panel as panel_module

    class Submit:
        def __init__(self, parent, *, title, on_submit) -> None:
            on_submit(None, None, None, f"{title} note")

    monkeypatch.setattr(panel_module, "FeedbackDialog", Submit)


def scheduled_day(tmp_path: Path, names: tuple[str, ...]):
    app = open_app(tmp_path / "desktop.db", tmp_path)
    day = app.pages["day"]
    for name in names:
        fill_form(day, name=name, day="1", duration="30", start="540", end="1020")
        day.form.submit_button.invoke()
    day.make_schedule_button.invoke()
    pump(app, until=lambda: not day._busy)
    panel = day.execution_panel
    pump(app, until=lambda: "loading" not in panel.status_label.cget("text"))
    return app, day, panel


def select(app, panel, name: str) -> None:
    label = next(task.label for task in panel._tasks if task.task.name == name)
    panel.task_var.set(label)
    panel._on_task_selected(label)
    pump(app, until=lambda: "loading" not in panel.status_label.cget("text"))


def act(app, panel, action: str) -> None:
    panel._action_buttons[action].invoke()
    pump(app, until=lambda: not panel.busy and "loading" not in panel.status_label.cget("text")
         and "saving" not in panel.status_label.cget("text"))


def enabled(panel) -> set[str]:
    return {action for action, button in panel._action_buttons.items() if button.cget("state") == "normal"}


def test_the_lifecycle_through_the_buttons_and_viewing_writes_nothing(tmp_path: Path, dialogs) -> None:
    app, day, panel = scheduled_day(tmp_path, ("Study",))
    try:
        assert rows(app, "executions") == []  # selecting and refreshing only look up
        day.reload()
        pump(app, until=lambda: "loading" not in panel.status_label.cget("text"))
        assert rows(app, "executions") == []
        assert panel.status_label.cget("text").endswith("Not started")
        assert panel.timing_label.cget("text").startswith("Overdue")  # 2024: long past, derived, not stored
        assert enabled(panel) == {"start", "skip", "cancel", "reschedule"}
        assert "UTC" in panel.detail_label.cget("text") and "30 min estimate" in panel.detail_label.cget("text")

        panel._action_buttons["start"].invoke()
        panel._action_buttons["start"].invoke()  # a double click while the first is saving
        pump(app, until=lambda: not panel.busy and "In progress" in panel.status_label.cget("text"))
        assert len(rows(app, "executions")) == 1 and len(rows(app, "work_sessions")) == 1
        assert enabled(panel) == {"pause", "complete", "skip", "cancel"}
        assert "cannot be moved" in panel.blocked_label.cget("text")

        act(app, panel, "pause")
        assert panel.status_label.cget("text").endswith("Paused")
        act(app, panel, "resume")
        act(app, panel, "complete")
        assert panel.status_label.cget("text").endswith("Completed") and enabled(panel) == set()
        assert "Active time" in panel.elapsed_label.cget("text")
        [execution] = rows(app, "executions")
        assert len(rows(app, "work_sessions")) == 2 and dialogs.errors == []
        assert "Study: Completed" in panel.day_status_label.cget("text")
    finally:
        close_app(app)


def test_skip_cancel_and_reschedule(tmp_path: Path, dialogs, monkeypatch) -> None:
    app, day, panel = scheduled_day(tmp_path, ("Skip me", "Cancel me", "Move me"))
    try:
        select(app, panel, "Skip me")
        act(app, panel, "skip")
        assert panel.status_label.cget("text").endswith("Skipped")
        select(app, panel, "Cancel me")
        act(app, panel, "cancel")
        assert panel.status_label.cget("text").endswith("Cancelled")

        select(app, panel, "Move me")
        original = next(task.placement for task in panel._tasks if task.task.name == "Move me")
        latest_end = max(task.placement.planned_end for task in panel._tasks)
        target = (latest_end + timedelta(minutes=30)).astimezone(timezone.utc)
        monkeypatch.setattr(panel, "ask_reschedule_target", lambda task: target.strftime("%H:%M"))
        panel._action_buttons["reschedule"].invoke()
        pump(app, until=lambda: not panel.busy and not day._busy)
        pump(app, until=lambda: "loading" not in panel.status_label.cget("text"))
        assert dialogs.errors == []
        moved = next(task.placement for task in panel._tasks if task.task.name == "Move me")
        assert moved.id != original.id and moved.planned_start == target
        tombstone = app.services.connection.execute(
            "SELECT removal_reason, superseded_by_id FROM scheduled_tasks WHERE id = ?", (str(original.id),)).fetchone()
        assert tuple(tombstone) == ("rescheduled", str(moved.id))
        assert [item.name for item in day.snapshot.timeline].count("Move me") == 1

        select(app, panel, "Skip me")  # finished work: no Reschedule, and why
        assert "reschedule" not in enabled(panel) and "stays in history" in panel.blocked_label.cget("text")
        statuses = [row[0] for row in app.services.connection.execute("SELECT status FROM executions ORDER BY 1")]
        assert statuses == ["cancelled", "skipped"]  # the move created no execution
    finally:
        close_app(app)


def test_paused_and_completed_state_survive_a_restart(tmp_path: Path, dialogs) -> None:
    app, day, panel = scheduled_day(tmp_path, ("Paused", "Done"))
    try:
        select(app, panel, "Paused")
        act(app, panel, "start")
        act(app, panel, "pause")
        select(app, panel, "Done")
        act(app, panel, "start")
        act(app, panel, "complete")
    finally:
        close_app(app)

    app = open_app(tmp_path / "desktop.db", tmp_path)
    try:
        day = app.pages["day"]
        day.open_date(WEDNESDAY)
        panel = day.execution_panel
        pump(app, until=lambda: panel._tasks and "loading" not in panel.status_label.cget("text"))
        select(app, panel, "Paused")
        assert panel.status_label.cget("text").endswith("Paused") and enabled(panel) == {
            "resume", "complete", "skip", "cancel"}
        select(app, panel, "Done")
        assert panel.status_label.cget("text").endswith("Completed")

        app.show_page("productivity")
        page = app.pages["productivity"].content
        pump(app, until=lambda: page.history_page is not None and page.latest_cohort is not None)
        assert page.reset_button.cget("text") == "Delete history on this device..."
        assert page.cohort_basis_label.cget("text").startswith("Planned dates")
        assert page.history_page.total == 0  # the 2024 plan is outside the last 30 days of today's date
    finally:
        close_app(app)


def test_the_history_browser_shows_the_plan_and_sessions(tmp_path: Path, dialogs, monkeypatch) -> None:
    import app.ui.productivity_controller as controller_module  # the page reads the last N days of real time

    app, day, panel = scheduled_day(tmp_path, ("Browse me",))
    try:
        act(app, panel, "start")
        act(app, panel, "complete")
        app.show_page("productivity")
        page = app.pages["productivity"].content
        page.history_window_var.set("Last 90 days")
        monkeypatch.setattr(controller_module, "local_date_of", lambda _now, _tz: WEDNESDAY + timedelta(days=1))
        page.refresh_history()
        pump(app, until=lambda: page.history_page is not None and page.history_page.entries)
        text = page.history_detail.get("1.0", "end")
        assert "Browse me" in text and "Status: Completed" in text and "Session 1" in text and "UTC" in text
    finally:
        close_app(app)
