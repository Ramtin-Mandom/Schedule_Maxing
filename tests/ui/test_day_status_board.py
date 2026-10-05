"""The Day page's Uncompleted | Tasks | Completed board as real widgets against a temporary
database: it replaces the former task list and Execute tab; scheduled tasks start in Tasks;
x and the arrows move them and back; an optimizer-unscheduled task is in no column; Make
Schedule again keeps every status; everything survives a restart; the columns stack on a narrow
window; and completed work reaches the Productivity history. Skipped without a display
(tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.execution.lifecycle import TaskOutcome
from app.ui.shell_state import LayoutMode
from tests.ui.test_desktop_app import WEDNESDAY, board_names, close_app, fill_form, open_app, press, pump
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display


def scheduled_day(db: Path, tmp_path: Path, names: tuple[str, ...]):
    app = open_app(db, tmp_path)
    day = app.pages["day"]
    for index, name in enumerate(names):
        start = 540 + 90 * index
        fill_form(day, name=name, duration="30", start=str(start), end=str(start + 60))
        day.form.submit_button.invoke()
        pump(app)
    day.make_schedule_button.invoke()
    pump(app, until=lambda: not day._busy)
    pump(app)
    return app, day


def test_the_board_replaces_the_old_tabs_and_moves_tasks_both_ways(tmp_path: Path, dialogs) -> None:
    app, day = scheduled_day(tmp_path / "board.db", tmp_path, ("Read", "Write", "Review"))
    try:
        assert not hasattr(day, "execution_panel") and not hasattr(day, "added_tasks_panel")
        assert not hasattr(day, "right_tabs")
        assert board_names(day) == {"uncompleted": [], "pending": ["Read", "Review", "Write"], "completed": []}
        assert app.services.execution_controller.list_executions().value == []  # viewing writes nothing

        press(app, day, "Read", "right")  # Tasks -> Completed
        press(app, day, "Write", "left")  # Tasks -> Uncompleted
        assert board_names(day) == {"uncompleted": ["Write"], "pending": ["Review"], "completed": ["Read"]}
        press(app, day, "Read", "left")  # Completed -> Tasks
        press(app, day, "Write", "right")  # Uncompleted -> Tasks
        assert board_names(day) == {"uncompleted": [], "pending": ["Read", "Review", "Write"], "completed": []}
        press(app, day, "Review", "right")
        assert dialogs.errors == [] and day.status_board.count_labels[TaskOutcome.COMPLETED].cget("text") == "1"
    finally:
        close_app(app)


def test_unscheduled_tasks_stay_out_and_a_rerun_keeps_every_status(tmp_path: Path, dialogs) -> None:
    app, day = scheduled_day(tmp_path / "rerun.db", tmp_path, ("A", "B", "C"))
    try:
        press(app, day, "A", "right")
        press(app, day, "C", "left")
        fill_form(day, name="Too long", duration="1440")  # a whole day: never fits beside the kept work
        day.form.submit_button.invoke()
        pump(app)
        fill_form(day, name="D", duration="30", start="1200", end="1260")
        day.form.submit_button.invoke()
        pump(app)
        day.make_schedule_button.invoke()
        pump(app, until=lambda: not day._busy)
        pump(app)
        names = board_names(day)
        assert names["completed"] == ["A"] and names["uncompleted"] == ["C"]  # answered work never moves
        scheduled = sorted(item.task.name for item in day.snapshot.executables)
        unplaced = {task.name for task in day.snapshot.unplaced}
        # Every newly scheduled task (B stays; D when the allocator places it) waits in Tasks...
        assert names["pending"] == sorted(set(scheduled) - {"A", "C"}) and "B" in names["pending"]
        # ...and what the scheduler left out is only in Available tasks, never in a column.
        assert "Too long" in unplaced and not unplaced & set(sum(names.values(), []))
    finally:
        close_app(app)


def test_statuses_survive_a_restart_and_reach_the_history(tmp_path: Path, dialogs, monkeypatch) -> None:
    import app.ui.productivity_controller as controller_module

    db = tmp_path / "restart.db"
    app, day = scheduled_day(db, tmp_path, ("Kept", "Missed", "Open"))
    try:
        press(app, day, "Kept", "right")
        press(app, day, "Missed", "left")
    finally:
        close_app(app)

    app = open_app(db, tmp_path)
    try:
        day = app.pages["day"]
        pump(app)
        assert board_names(day) == {"uncompleted": ["Missed"], "pending": ["Open"], "completed": ["Kept"]}

        monkeypatch.setattr(controller_module, "local_date_of", lambda _now, _tz: WEDNESDAY + timedelta(days=1))
        history = app.productivity_controller.history(90)
        assert history.ok and history.value.entries
        statuses = {entry.name: entry.status for entry in history.value.entries}
        assert statuses["Kept"] == "completed" and statuses["Missed"] == "skipped"
    finally:
        close_app(app)


def test_the_columns_stack_on_a_narrow_window(tmp_path: Path, dialogs) -> None:
    app, day = scheduled_day(tmp_path / "narrow.db", tmp_path, ("Read",))
    try:
        board = day.status_board
        rows = {outcome: column.grid_info()["row"] for outcome, column in board.columns.items()}
        assert len(set(rows.values())) == 1  # wide: side by side
        day.set_layout(LayoutMode.NARROW)
        pump(app)
        rows = [column.grid_info()["row"] for column in board.columns.values()]
        assert rows == [0, 1, 2]  # narrow: stacked, same controls
        press(app, day, "Read", "right")
        assert board_names(day)["completed"] == ["Read"]
    finally:
        close_app(app)
