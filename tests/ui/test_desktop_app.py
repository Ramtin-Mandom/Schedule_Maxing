"""Widget-level tests for app/app.py's ScheduleOptimizerApp, driving the real
Tk callbacks (form submit button, Edit/Remove, Make Schedule, Execute tab,
Reset, close) against a temporary database.

Skipped automatically when no display is available (e.g. headless CI);
the same callback boundary is covered display-free by
tests/ui/test_schedule_page_controller.py and test_app_services.py.
Dialogs are replaced by recorders so nothing blocks.
"""

from __future__ import annotations

import gc
import time
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository
from app.ui import background
from app.ui.schedule_page_controller import RowRef
from tests.tk_cleanup import cancel_stale_after_jobs


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


pytestmark = pytest.mark.skipif(not _display_available(), reason="no display available for Tk")

WEDNESDAY = date(2024, 6, 5)  # the week page therefore starts on Monday 2024-06-03


class Dialogs:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.confirm = True
        #: Messages of the design-system confirmations (app.ui.components.ask_confirm) the Day page asked.
        self.confirms: list[str] = []

    def install(self, monkeypatch) -> None:
        import app.app as app_module
        import app.ui.calendar_page as calendar_module
        import app.ui.day_page as day_module
        for module in (app_module,):
            monkeypatch.setattr(module.messagebox, "showerror", lambda title, message, **_: self.errors.append(message))
            monkeypatch.setattr(module.messagebox, "showinfo", lambda title, message, **_: self.infos.append(message))
            monkeypatch.setattr(module.messagebox, "askyesno", lambda title, message, **_: self.confirm)
        for module in (day_module, calendar_module):
            monkeypatch.setattr(module, "ask_confirm", lambda parent, *, message, **_: (
                self.confirms.append(message), self.confirm)[1])


@pytest.fixture
def dialogs(monkeypatch) -> Dialogs:
    recorder = Dialogs()
    recorder.install(monkeypatch)
    previous = background.current_registry()
    yield recorder
    background.install_registry(previous)


def open_app(db_path: Path, project_root: Path):
    from app.app import ScheduleOptimizerApp

    app = ScheduleOptimizerApp(db_path=str(db_path), timezone="UTC", project_root=str(project_root), today=WEDNESDAY)
    app.withdraw()
    pump(app)
    return app


def pump(app, until=lambda: True, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        app.update()
        if until() and background.current_registry().outstanding == 0:
            app.update()
            return
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the UI")
        time.sleep(0.01)


def finish_closing(app, timeout: float = 15.0) -> None:
    """Closing never blocks the Tk thread: serve the event loop until the workers are done and the window is gone."""
    deadline = time.monotonic() + timeout
    while not app._closed:
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the window to close")
        app.update()
        time.sleep(0.005)


def close_app(app) -> None:
    app._on_close()
    finish_closing(app)
    cancel_stale_after_jobs(app)  # its leftover timers must not fire during later tests (tests/tk_cleanup.py)
    # Collect the closed window's Tk objects (fonts, images) now, on the Tk thread. Left for later, a
    # garbage collection on another thread (e.g. a TestClient's event loop) would call into Tcl from the
    # wrong thread and block.
    gc.collect()


def fill_form(page, *, name: str, day: str = "1", fixed: bool = False, start="480", end="720", duration="60") -> None:
    """
    Fill the task editor like a user: select day N of the page (the form has no date field; a Week/Month
    task gets the selected day, a Day task the page's date), then enter times as hour, minute and AM/PM.
    """
    from app.ui.time_fields import minutes_to_clock

    target = page.page_controller.anchor_date + timedelta(days=int(day) - 1)
    if hasattr(page, "select_date"):
        page.select_date(target)
    else:
        assert target == page.page_controller.anchor_date, "the Day page adds to the date it shows"
    form = page.form
    form.set_kind("block" if fixed else "task")
    form.name_field.variable.set(name)
    form.category_select.variable.set("study")

    def enter(field, minutes: int) -> None:
        hour, minute, meridiem = minutes_to_clock(minutes)
        field.hour_var.set(str(hour))
        field.minute_var.set(f"{minute:02d}")
        if field.meridiem != meridiem:
            field.meridiem_button.invoke()  # AM/PM is a toggle, as a user clicks it

    if fixed:
        enter(form.start_field, int(start))
        enter(form.end_field, int(end))
    else:
        form.duration_field.variable.set(duration)
        form.priority_select.variable.set("5")
        enter(form.window_start, int(start))
        enter(form.window_end, int(end))
        form.tag_input.set_tags(["tag"])


def tree_names(page) -> list[str]:
    """The names of the saved tasks and fixed blocks of the page's dates, as the page last read them."""
    return [row.name for row in page.snapshot.rows]


def board_names(page) -> dict[str, list[str]]:
    """The Day page's Uncompleted | Tasks | Completed columns, by name."""
    from app.execution.lifecycle import TaskOutcome

    board = page.status_board.board
    return {outcome.value: sorted(card.name for card in board.column(outcome)) for outcome in TaskOutcome}


def press(app, page, name: str, side: str) -> None:
    """Click a card's left (x) or right (arrow) control on the Day board and wait for the saved state."""
    card = next(card for card in page.status_board.board.cards if card.name == name)
    page.status_board.card_widgets[card.key][side].invoke()
    pump(app)


def stored_tasks(db_path: Path):
    connection = get_connection(db_path)
    try:
        return PlanningService(PlanningRepository(connection)).list_tasks()
    finally:
        connection.close()


def test_create_edit_delete_schedule_execute_close_reopen_reset(tmp_path: Path, dialogs: Dialogs) -> None:
    db_path = tmp_path / "desktop.db"
    app = open_app(db_path, tmp_path)
    week = app.pages["week"]
    assert week.page_controller.anchor_date == date(2024, 6, 3)
    assert tree_names(week) == []  # nothing is imported or sampled at startup

    # Create through the real submit button: committed before the table refresh.
    fill_form(week, name="Study", day="1")
    week.form.submit_button.invoke()
    pump(app)
    fill_form(week, name="Study", day="2")  # a duplicate name on another day
    week.form.submit_button.invoke()
    pump(app)
    fill_form(week, name="Lecture", day="1", fixed=True, start="480", end="540")
    week.form.submit_button.invoke()
    pump(app)
    assert dialogs.errors == []
    assert sorted(tree_names(week)) == ["Lecture", "Study", "Study"]
    assert [task.name for task in stored_tasks(db_path)] == ["Study", "Study"]

    # Edit the Tuesday "Study" by its id (as its Day page's Available-task button does).
    tuesday_task = next(t for t in stored_tasks(db_path) if t.preferred_dates == [date(2024, 6, 4)])
    week.edit_ref(RowRef("task", tuesday_task.id, tuesday_task.version))
    pump(app)
    assert week.form.submit_button.cget("text") == "Save changes"
    week.form.name_field.variable.set("Review")
    week.form.submit_button.invoke()
    pump(app)
    assert sorted(tree_names(week)) == ["Lecture", "Review", "Study"]
    assert {t.id: t.name for t in stored_tasks(db_path)}[tuesday_task.id] == "Review"

    # Delete it again, by id.
    stored = next(t for t in stored_tasks(db_path) if t.id == tuesday_task.id)
    week.remove_ref(RowRef("task", stored.id, stored.version))
    pump(app)
    assert sorted(tree_names(week)) == ["Lecture", "Study"]

    # Scheduling happens on Day: Open Day, Make Schedule (in the background), then mark it done there.
    week.select_date(date(2024, 6, 3))
    pump(app)
    week.open_day_button.invoke()
    pump(app)
    day = app.pages["day"]
    assert app.shell.current == "day" and day.page_controller.anchor_date == date(2024, 6, 3)
    day.make_schedule_button.invoke()
    pump(app, until=lambda: not day._busy)
    assert dialogs.errors == []
    assert day.freshness_badge.cget("text") == "Current"
    pump(app)
    assert board_names(day) == {"pending": ["Study"], "completed": [], "uncompleted": []}
    press(app, day, "Study", "right")
    assert board_names(day) == {"pending": [], "completed": ["Study"], "uncompleted": []}
    day.back_button.invoke()
    pump(app)
    assert app.shell.current == "week"
    assert week.snapshot.day(date(2024, 6, 3)).freshness_label == "Current"
    close_app(app)
    assert app.services.closed

    # Reopen: tasks, fixed blocks, placements and the completed status are restored.
    app = open_app(db_path, tmp_path)
    try:
        week = app.pages["week"]
        assert sorted(tree_names(week)) == ["Lecture", "Study"]
        # Restored with its persisted provenance: nothing it depends on changed, so it is still current.
        assert week.snapshot.day(date(2024, 6, 3)).freshness_label == "Current"
        day = app.pages["day"]
        day.open_date(date(2024, 6, 3))
        pump(app)
        assert board_names(day)["completed"] == ["Study"]

        # Reset Week: previewed and confirmed; execution history is kept.
        week.reset_button.invoke()
        pump(app)
        assert "Reset the week of Mon Jun 3" in dialogs.confirms[-1]
        assert dialogs.errors == [] and tree_names(week) == []
        assert week.snapshot.day(date(2024, 6, 3)).items == []
    finally:
        close_app(app)

    connection = get_connection(db_path)
    try:
        [execution] = ExecutionService(ExecutionRepository(connection)).list_executions()
        assert execution.status.value == "completed" and execution.task_name == "Study"
    finally:
        connection.close()


def test_invalid_input_shows_an_error_and_saves_nothing(tmp_path: Path, dialogs: Dialogs) -> None:
    app = open_app(tmp_path / "desktop.db", tmp_path)
    try:
        day = app.pages["day"]
        fill_form(day, name="Bad", duration="0")
        day.form.window_start.hour_var.set("13")  # hours are 1-12 beside the AM/PM toggle
        day.form.submit_button.invoke()
        pump(app)
        assert day.form.duration_field.error == "A task takes at least 1 minute."  # shown next to its field
        assert "hour goes from 1 to 12" in day.form.window_start.error
        assert day.form.notice.text.startswith("Error:")
        assert tree_names(day) == []
        assert day.form.name_field.get() == "Bad"  # the user's input is kept for correction
    finally:
        close_app(app)


def test_csv_upload_and_export_go_through_the_saved_data_boundary(tmp_path: Path, dialogs: Dialogs, monkeypatch) -> None:
    import app.ui.day_page as day_module

    db_path = tmp_path / "desktop.db"
    good = tmp_path / "good.csv"
    good.write_text(
        "date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
        "1,Read,study,t,false,540,720,60,5,\n2,Summarise,study,t,false,540,720,60,5,Read\n"
        "1,Lecture,event,t,true,480,540,0,0,\n",
        encoding="utf-8",
    )
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
        "1,Okay,study,t,false,540,720,60,5,\n1,Broken,study,t,false,540,720,60,5,Missing\n",
        encoding="utf-8",
    )
    chosen = {"file": str(good), "mode": "append"}
    monkeypatch.setattr(day_module.filedialog, "askopenfilename", lambda **_: chosen["file"])
    monkeypatch.setattr(day_module.filedialog, "asksaveasfilename", lambda **_: str(tmp_path / "export.csv"))
    monkeypatch.setattr(day_module, "ChoiceDialog", lambda parent, *, on_choose, **_: on_choose(chosen["mode"]))

    app = open_app(db_path, tmp_path)
    try:
        day = app.pages["day"]
        week = app.pages["week"]
        day.import_button.invoke()  # a legacy file: imported only after the explicit choice and confirmation
        pump(app)
        assert dialogs.errors == [] and "Imported 2 task(s) and 1 fixed block(s)" in day.notice.text
        assert sorted(tree_names(day)) == ["Lecture", "Read"]
        week.reload()
        pump(app)
        assert sorted(tree_names(week)) == ["Lecture", "Read", "Summarise"]  # the other dates see it too

        chosen["file"] = str(bad)
        day.import_button.invoke()
        pump(app)
        assert "Missing" in day.notice.text and "nothing was saved" in day.notice.text
        week.reload()
        pump(app)
        assert sorted(tree_names(week)) == ["Lecture", "Read", "Summarise"]  # unchanged, re-read

        chosen["file"], chosen["mode"] = str(good), "replace"
        day.import_button.invoke()
        pump(app)
        assert "replacing 2024-06-05 to 2024-06-06" in day.notice.text
        assert len(stored_tasks(db_path)) == 2  # replaced, not duplicated

        day.export_button.invoke()
        pump(app)
        assert "Exported 1 task(s), 1 fixed block(s)" in day.notice.text
        assert (tmp_path / "export.csv").read_text(encoding="utf-8").startswith("record_type,id,task_id,date")
    finally:
        close_app(app)


def test_startup_failure_shows_an_error_instead_of_the_scheduler(tmp_path: Path, dialogs: Dialogs) -> None:
    unusable = tmp_path / "is_a_directory.db"
    unusable.mkdir()
    from app.app import ScheduleOptimizerApp

    app = ScheduleOptimizerApp(db_path=str(unusable), project_root=str(tmp_path), today=WEDNESDAY)
    app.withdraw()
    try:
        assert app.services is None
        assert app.pages == {}  # no page that could accept unsaved edits
        assert str(unusable) in dialogs.errors[-1]
    finally:
        app._on_close()
