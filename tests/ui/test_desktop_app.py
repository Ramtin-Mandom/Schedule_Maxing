"""Widget-level tests for app/app.py's ScheduleOptimizerApp, driving the real
Tk callbacks (form submit button, Edit/Remove, Make Schedule, Execute tab,
Reset, close) against a temporary database.

Skipped automatically when no display is available (e.g. headless CI);
the same callback boundary is covered display-free by
tests/ui/test_schedule_page_controller.py and test_app_services.py.
Dialogs are replaced by recorders so nothing blocks.
"""

from __future__ import annotations

import time
import tkinter as tk
from datetime import date
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository
from app.ui import background
from app.ui.schedule_page_controller import ResetScope


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

    def install(self, monkeypatch) -> None:
        import app.app as app_module
        import app.ui.execution_panel as panel_module

        for module in (app_module, panel_module):
            monkeypatch.setattr(module.messagebox, "showerror", lambda title, message, **_: self.errors.append(message))
            monkeypatch.setattr(module.messagebox, "showinfo", lambda title, message, **_: self.infos.append(message))
            monkeypatch.setattr(module.messagebox, "askyesno", lambda title, message, **_: self.confirm)


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
        if until() and background.current_registry().active == 0:
            app.update()
            return
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the UI")
        time.sleep(0.01)


def close_app(app) -> None:
    app._on_close()


def fill_form(page, *, name: str, day: str = "1", fixed: bool = False, start="480", end="720", duration="60") -> None:
    form = page.form
    form.fixed_var.set("True" if fixed else "False")
    form._sync_fixed_fields()
    form.name_var.set(name)
    form.day_var.set(day)
    form.category_var.set("study")
    form.tag_var.set("tag")
    form.start_var.set(start)
    form.end_var.set(end)
    if not fixed:
        form.duration_var.set(duration)
        form.priority_var.set("5")


def tree_names(page) -> list[str]:
    tree = page.added_tasks_panel.tree
    return [tree.item(item_id, "values")[1] for item_id in tree.get_children()]


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
    fill_form(week, name="Study", day="2")  # a duplicate name on another day
    week.form.submit_button.invoke()
    fill_form(week, name="Lecture", day="1", fixed=True, start="480", end="540")
    week.form.submit_button.invoke()
    assert dialogs.errors == []
    assert sorted(tree_names(week)) == ["Lecture", "Study", "Study"]
    assert [task.name for task in stored_tasks(db_path)] == ["Study", "Study"]

    # Edit the Tuesday "Study" selected by its UUID row id.
    tuesday_task = next(t for t in stored_tasks(db_path) if t.preferred_dates == [date(2024, 6, 4)])
    week.added_tasks_panel.tree.selection_set(f"task:{tuesday_task.id}")
    week.edit_selected_task()
    assert week.form.submit_button.cget("text") == "Save Changes"
    week.form.name_var.set("Review")
    week.form.submit_button.invoke()
    assert sorted(tree_names(week)) == ["Lecture", "Review", "Study"]
    assert {t.id: t.name for t in stored_tasks(db_path)}[tuesday_task.id] == "Review"

    # Delete it again, by id.
    week.added_tasks_panel.tree.selection_set(f"task:{tuesday_task.id}")
    week.remove_selected_task()
    assert sorted(tree_names(week)) == ["Lecture", "Study"]

    # Make Schedule runs in the background, saves, then redraws from SQLite.
    week.make_schedule_button.invoke()
    pump(app, until=lambda: not week._busy)
    assert dialogs.errors == []
    assert "current" in week.status_label.cget("text")
    panel = week.execution_panel
    pump(app, until=lambda: "loading" not in panel.status_label.cget("text"))
    assert panel.status_label.cget("text").endswith("Not started")

    panel._action_buttons["start"].invoke()
    pump(app, until=lambda: "In progress" in panel.status_label.cget("text"))
    close_app(app)
    assert app.services.closed

    # Reopen: tasks, fixed blocks, placements and the running execution are restored.
    app = open_app(db_path, tmp_path)
    try:
        week = app.pages["week"]
        assert sorted(tree_names(week)) == ["Lecture", "Study"]
        assert "out of date" in week.status_label.cget("text")  # restored conservatively as stale
        panel = week.execution_panel
        pump(app, until=lambda: "In progress" in panel.status_label.cget("text"))

        week.confirm_reset(ResetScope.SCHEDULE)
        assert dialogs.errors == []
        assert "No saved schedule" in week.status_label.cget("text")
        assert sorted(tree_names(week)) == ["Lecture", "Study"]
    finally:
        close_app(app)

    connection = get_connection(db_path)
    try:
        [execution] = ExecutionService(ExecutionRepository(connection)).list_executions()
        assert execution.status.value == "in_progress" and execution.task_name == "Study"
    finally:
        connection.close()


def test_invalid_input_shows_an_error_and_saves_nothing(tmp_path: Path, dialogs: Dialogs) -> None:
    app = open_app(tmp_path / "desktop.db", tmp_path)
    try:
        day = app.pages["day"]
        fill_form(day, name="Bad", duration="45")
        day.form.submit_button.invoke()
        assert dialogs.errors and "multiple of 30" in dialogs.errors[-1]
        assert tree_names(day) == []
        assert day.form.name_var.get() == "Bad"  # the user's input is kept for correction
    finally:
        close_app(app)


def test_csv_upload_and_export_go_through_the_saved_data_boundary(tmp_path: Path, dialogs: Dialogs, monkeypatch) -> None:
    import app.app as app_module

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
    monkeypatch.setattr(app_module.filedialog, "askopenfilename", lambda **_: chosen["file"])
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **_: str(tmp_path / "export.csv"))
    monkeypatch.setattr(
        app_module, "ChoiceDialog", lambda parent, *, on_choose, **_: on_choose(chosen["mode"])
    )

    app = open_app(db_path, tmp_path)
    try:
        week = app.pages["week"]
        week.upload_button.invoke()
        assert dialogs.errors == [] and "Imported 2 task(s) and 1 fixed block(s)" in dialogs.infos[-1]
        assert sorted(tree_names(week)) == ["Lecture", "Read", "Summarise"]

        chosen["file"] = str(bad)
        week.upload_button.invoke()
        assert "Missing" in dialogs.errors[-1] and "nothing was saved" in dialogs.errors[-1]
        assert sorted(tree_names(week)) == ["Lecture", "Read", "Summarise"]  # unchanged, re-read

        chosen["file"], chosen["mode"] = str(good), "replace"
        week.upload_button.invoke()
        assert "replacing 2024-06-03 to 2024-06-04" in dialogs.infos[-1]
        assert sorted(tree_names(week)) == ["Lecture", "Read", "Summarise"]
        assert len(stored_tasks(db_path)) == 2  # replaced, not duplicated

        week.export_csv()
        assert "Exported 2 task(s), 1 fixed block(s)" in dialogs.infos[-1]
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
