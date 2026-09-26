"""The reusable task form as real widgets (Milestone 4, Prompt 3), against a temporary
database: typed and stepped minute-precise times agree, tags are added with Enter without
submitting and removed again, a 13-minute task at 10:13 is saved exactly, a fixed-block
overlap is refused with the form kept and nothing written, an edit keeps recurrence and
hidden fields, removal asks first, and everything survives a reopen. Skipped without a
display (tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from app.planning.models import Project, RecurrenceFrequency, RecurrenceSpec, Task
from app.ui.schedule_page_controller import RowRef
from app.ui.task_editor import NO_PROJECT
from app.ui.task_form_model import TaskDraft
from tests.ui.test_desktop_app import WEDNESDAY, close_app, open_app, tree_names
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display
from tests.ui.test_desktop_shell import key, settle, shown


def stored(app) -> dict[str, Task]:
    return {task.name: task for task in app.services.planning_controller.list_tasks().value}


def test_reserved_project_name_can_be_assigned_and_cleared(tmp_path, dialogs):
    app = open_app(tmp_path / "projects.db", tmp_path)
    try:
        project = app.services.planning_service.create_project(Project(name=NO_PROJECT))
        day = app.pages["day"]
        day._reset_editor()
        form = day.form
        assert form._project_ids[NO_PROJECT] is None
        label = next(label for label, key in form._project_ids.items() if key == project.id)
        form.name_field.variable.set("Project task")
        form.duration_field.variable.set("13")
        form.project_select.variable.set(label)
        form.submit_button.invoke()
        task = stored(app)["Project task"]
        assert task.project_id == project.id
        day.edit_ref(RowRef("task", task.id, task.version))
        form.project_select.variable.set(NO_PROJECT)
        form.submit_button.invoke()
        assert stored(app)["Project task"].project_id is None
        assert dialogs.errors == []
    finally:
        close_app(app)


def test_times_tags_and_a_minute_precise_task(tmp_path: Path, dialogs) -> None:
    app = shown(open_app(tmp_path / "editor.db", tmp_path), tmp_path)
    try:
        day = app.pages["day"]
        form = day.form
        assert form.date_field.get() == WEDNESDAY.isoformat()  # a new task starts on the page's date
        form.toggle_more()

        start = form.window_start
        start.variable.set("10:12")
        key(start.entry, "Up")
        assert start.get() == "10:13 AM"  # typed then stepped: one minute
        key(start.entry, "Shift-Up")
        assert start.get() == "10:28 AM"
        start.down_button.invoke()
        key(start.entry, "Shift-Down")
        assert start.get() == "10:12 AM"
        start.up_button.invoke()
        assert start.value() == 613  # the buttons, keys and typing all land on the same minute
        form.window_end.variable.set("12:00 AM")
        form.window_end.normalize()
        assert form.window_end.get() == "12:00 AM (next day)"

        tags = form.tag_input
        for text in ("focus", "math", "focus"):
            tags.field.variable.set(text)
            key(tags.field.entry, "Return")
        assert tags.tags == ["focus", "math"] and tree_names(day) == []  # Enter added tags, never submitted
        tags.field.variable.set("")
        key(tags.field.entry, "BackSpace")
        assert tags.tags == ["focus"]
        tags.field.variable.set("reading")
        key(tags.field.entry, "Return")
        tags.chip_buttons["focus"].invoke()
        assert tags.tags == ["reading"]

        form.name_field.variable.set("Flashcards")
        form.duration_field.variable.set("13")
        form.submit_button.invoke()
        assert dialogs.errors == [] and "Flashcards" in tree_names(day)
        task = stored(app)["Flashcards"]
        assert task.estimated_duration_minutes == 13
        assert (task.preferred_time_window.start_minute, task.preferred_time_window.end_minute) == (613, 1440)
        assert task.tags == ["reading"] and task.preferred_dates == [WEDNESDAY]
        assert form.name_field.get() == "" and form.date_field.get() == WEDNESDAY.isoformat()  # ready for the next
    finally:
        close_app(app)


def test_overlap_refusal_keeps_the_form_and_writes_nothing(tmp_path: Path, dialogs) -> None:
    app = shown(open_app(tmp_path / "blocks.db", tmp_path), tmp_path)
    try:
        day = app.pages["day"]
        form = day.form
        form.set_kind("block")
        for label, start, end in (("Lecture", "9:00 AM", "10:30 AM"), ("Gym", "10:13 AM", "11:00 AM")):
            form.name_field.variable.set(label)
            form.start_field.variable.set(start)
            form.end_field.variable.set(end)
            form.submit_button.invoke()
        assert tree_names(day) == ["Lecture"]
        assert "overlaps the fixed block “Lecture” (9:00 AM – 10:30 AM" in form.notice.text
        assert (form.name_field.get(), form.start_field.get(), form.end_field.get()) == ("Gym", "10:13 AM", "11:00 AM")
        count = app.services.connection.execute("SELECT count(*) FROM fixed_blocks").fetchone()[0]
        assert count == 1

        form.start_field.variable.set("10:30 AM")  # corrected: adjacent is fine
        form.submit_button.invoke()
        assert sorted(tree_names(day)) == ["Gym", "Lecture"] and form.kind == "block"
    finally:
        close_app(app)


def test_edit_keeps_hidden_fields_remove_asks_and_everything_survives_a_reopen(tmp_path: Path, dialogs) -> None:
    db = tmp_path / "edit.db"
    app = open_app(db, tmp_path)
    try:
        service = app.services.planning_service
        gym = service.create_task(Task(
            name="Gym", category="Volunteering", estimated_duration_minutes=45, priority=4, tags=["a", "b"],
            preferred_dates=[WEDNESDAY, WEDNESDAY + timedelta(days=7)],
            recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.WEEKLY, weekdays=[2])))
        day = app.pages["day"]
        day.reload()
        day.added_tasks_panel.tree.selection_set(f"task:{gym.id}")
        day.edit_selected_task()
        form = day.form
        assert form.editing and form.category_select.get() == "Volunteering"  # an unknown category is kept
        assert form.kind_buttons["block"].cget("state") == "disabled"  # a task stays a task
        form.name_field.variable.set("Gym (evening)")
        form.duration_field.variable.set("1 h 13 min")
        form.submit_button.invoke()
        saved = stored(app)["Gym (evening)"]
        assert saved.id == gym.id and saved.estimated_duration_minutes == 73
        assert saved.recurrence == gym.recurrence and saved.tags == ["a", "b"] and saved.category == "Volunteering"
        assert saved.preferred_dates == [WEDNESDAY, WEDNESDAY + timedelta(days=7)]

        form.load(TaskDraft(name="Temp", duration="5", date=WEDNESDAY.isoformat()), editing=False)
        form.submit_button.invoke()
        temp = stored(app)["Temp"]
        day.added_tasks_panel.tree.selection_set(f"task:{temp.id}")
        dialogs.confirm = False
        day.remove_selected_task()
        assert "Temp" in stored(app)  # declined: nothing removed
        dialogs.confirm = True
        day.remove_selected_task()
        assert "Temp" not in stored(app)
    finally:
        close_app(app)

    app = open_app(db, tmp_path)
    try:
        assert set(stored(app)) == {"Gym (evening)"}
        assert stored(app)["Gym (evening)"].recurrence.weekdays == [2]
        assert tree_names(app.pages["day"]) == ["Gym (evening)"]
    finally:
        close_app(app)


def test_week_and_month_use_the_same_form_with_real_dates(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "week.db", tmp_path)
    try:
        week = app.pages["week"]
        assert type(week.form) is type(app.pages["day"].form) is type(app.pages["month"].form)
        friday = date(2024, 6, 7)
        week.form.load(TaskDraft(name="Report", duration="2 h", date=friday.isoformat(), pin_to_date=True),
                       editing=False)
        week.form.submit_button.invoke()
        assert stored(app)["Report"].required_date == friday and "Report" in tree_names(week)
        settle(app, 0.1)
    finally:
        close_app(app)
