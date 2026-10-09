"""The Day Schedule as real widgets (Milestone 4, Prompt 4) against a temporary database: the
page opens on the date, fixed blocks appear at once in their category's color, Make Schedule
(a background run) draws each task at its exact minutes, an unchanged run writes nothing, the
Engine choice beside Make Schedule saves only this date and is disabled while saving,
Day Preferences edits the date layer natively, everything survives a reopen, Reset Day asks
first and can be cancelled, CSV v2 export/import goes through native dialogs with a preview,
Week -> Day keeps the way back, and the scheduled tasks reach the status board. Skipped without a display
(see tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.planning.preferences import OptimizerMode
from app.ui import theme
from app.ui.day_timeline import TimelineGeometry
from app.execution.lifecycle import TaskOutcome
from tests.ui.test_desktop_app import WEDNESDAY, close_app, fill_form, open_app, pump, tree_names
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display


def run_make_schedule(app, day) -> None:
    day.make_schedule_button.invoke()
    pump(app, until=lambda: not day._busy)


def item_named(day, name: str):
    return next(item for item in day.snapshot.timeline if item.name == name)


def rows_of(app, table: str) -> list[tuple]:
    return [tuple(row) for row in app.services.connection.execute(f"SELECT * FROM {table} ORDER BY 1")]


def saved_state(app) -> tuple:
    return tuple(rows_of(app, name) for name in ("tasks", "fixed_blocks", "scheduled_tasks", "schedule_generations",
                                                  "preference_overrides", "sync_dirty"))


def test_the_persisted_day_flow(tmp_path: Path, dialogs) -> None:
    db_path = tmp_path / "day.db"
    app = open_app(db_path, tmp_path)
    try:
        day = app.pages["day"]
        assert app.shell.current == "day" and day.page_controller.anchor_date == WEDNESDAY
        assert day.freshness_badge.cget("text") == "Not scheduled yet"
        assert day.engine_select.get() == "Normal"
        assert day.engine_select.values == ["Normal", "ADHD friendly", "Early finish", "Night owl", "Catch-up"]
        assert rows_of(app, "preference_overrides") == []  # nothing is written at startup

        # A fixed block shows at once, in its own category's color.
        fill_form(day, name="Gym", fixed=True, start="540", end="630")
        day.form.category_select.variable.set("exercise")
        day.form.submit_button.invoke()
        pump(app)
        fill_form(day, name="Read", duration="13", preferred="Mid")
        day.form.submit_button.invoke()
        pump(app)
        assert dialogs.errors == [] and [item.name for item in day.snapshot.timeline] == ["Gym"]
        assert [chip.task.name for chip in day.chips] == ["Read"]  # available, not yet scheduled
        pump(app)
        gym = day.schedule_canvas.item_bounds(item_named(day, "Gym").key)
        box = day.schedule_canvas.canvas.find_withtag(f"box:{item_named(day, 'Gym').key}")[0]
        assert day.schedule_canvas.canvas.itemcget(box, "fill") == theme.resolve(theme.category_style("exercise").fill)
        geometry = day.schedule_canvas.geometry  # fitted to the canvas: the whole day, no horizontal scrolling
        assert (gym[0], gym[2]) == (geometry.x(540), geometry.x(630))
        assert geometry == TimelineGeometry().fitted(geometry.width()) and not hasattr(day.schedule_canvas, "h_scroll")

        run_make_schedule(app, day)
        pump(app)
        assert day.notice.text.startswith("Done: Scheduled 1 task(s)") and day.freshness_badge.cget("text") == "Current"
        read = item_named(day, "Read")
        # Mid: the middle third of the whole-day window starts at 8:00 AM.
        assert (read.start_minute, read.end_minute, read.time_text) == (480, 493, "8:00 AM – 8:13 AM")
        pump(app)
        bounds = day.schedule_canvas.item_bounds(read.key)
        geometry = day.schedule_canvas.geometry
        assert abs((bounds[2] - bounds[0]) - max(2, 13 * geometry.px_per_minute)) < 1e-6  # exactly 13 minutes wide
        assert day.chips == [] and any(gap.start_minute == 493 for gap in day.snapshot.free_gaps)

        saved = saved_state(app)
        run_make_schedule(app, day)
        pump(app)
        assert "already current" in day.notice.text and saved_state(app) == saved

        # The keyboard way to the same actions: the timeline selects and describes an item.
        day.schedule_canvas.select(read.key)
        pump(app)
        assert "Read: 8:00 AM – 8:13 AM (13 min), scheduled" in day.schedule_canvas.details.cget("text")

        # Engine: saved for this date only (in the background, with the controls disabled meanwhile).
        day.engine_select.choose("ADHD friendly")
        assert day._busy and str(day.make_schedule_button.cget("state")) == "disabled"
        pump(app, until=lambda: not day._busy)
        assert "now ADHD friendly" in day.notice.text and day.freshness_badge.cget("text") == "Out of date"
        assert day.engine_select.get() == "ADHD friendly" and "quarter hour" in day.engine_note.cget("text")
        controller = app.services.planning_controller
        assert controller.date_preferences(WEDNESDAY).value.overrides.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
        assert controller.user_preferences().value is None
        day.shift_date(1)
        pump(app)
        assert day.engine_select.get() == "Normal"  # another date is unaffected
        day.shift_date(-1)
        pump(app)

        # A 13-minute task still fits the quarter-hour rule, so nothing is regenerated: it is kept.
        run_make_schedule(app, day)
        pump(app)
        assert day.notice.text.startswith("Done: Kept 1 scheduled task(s) in place")
        assert item_named(day, "Read").placement_id == read.placement_id
        fill_form(day, name="Essay", duration="45", start="600", end="900")
        day.form.submit_button.invoke()
        pump(app)
        run_make_schedule(app, day)
        pump(app)
        assert item_named(day, "Essay").start_minute % 15 == 0
        pump(app)
        assert len(day.snapshot.executables) == 2  # both scheduled tasks wait in the board's Tasks column
        assert sorted(card.name for card in day.status_board.board.column(TaskOutcome.PENDING)) == [
            "Essay", "Gym", "Read"]  # the fixed block is completed on the board too

        # Day Preferences: a native editor of the date layer, inherited values shown.
        day.open_preferences()
        pump(app)
        dialog = day.preferences_dialog
        spacing = "reward.min_gap_between_tasks_minutes"
        assert "Inherited app default" in dialog.editor.rows[spacing].source
        assert "reward.short_gap_bonus_weight" in dialog.editor.rows  # ADHD friendly's own control
        dialog.editor.set_input(spacing, "45")
        pump(app)
        dialog.editor.buttons[spacing]["save"].invoke()
        pump(app)
        assert dialog.editor.rows[spacing].state == "set" and "saved" in dialog.notice.text
        dialog.editor.set_input("reward.weight_importance", "lots")
        pump(app)
        dialog.editor.buttons["reward.weight_importance"]["save"].invoke()
        pump(app)
        assert dialog.editor.error_labels["reward.weight_importance"].cget("text").startswith("Error:")
        dialog.primary_button.invoke()
        pump(app)
        assert day.freshness_badge.cget("text") == "Out of date"  # the page re-read itself
        run_make_schedule(app, day)
        pump(app)
        assert day.freshness_badge.cget("text") == "Current"
        timeline = [(item.name, item.start_minute, item.end_minute) for item in day.snapshot.timeline]
    finally:
        close_app(app)

    # Reopen: tasks, blocks, placements, the engine and the freshness are restored from SQLite.
    app = open_app(db_path, tmp_path)
    try:
        day = app.pages["day"]
        assert [(item.name, item.start_minute, item.end_minute) for item in day.snapshot.timeline] == timeline
        assert day.freshness_badge.cget("text") == "Current" and day.engine_select.get() == "ADHD friendly"

        dialogs.confirm = False
        before = saved_state(app)
        day.reset_button.invoke()
        pump(app)
        assert "Reset Wed Jun 5?" in dialogs.confirms[-1] and "points" in dialogs.confirms[-1]
        assert "nothing was deleted" in day.notice.text and saved_state(app) == before

        dialogs.confirm = True
        day.reset_button.invoke()
        pump(app)
        assert day.snapshot.timeline == [] and tree_names(day) == []
        assert day.engine_select.get() == "Normal" and day.freshness_badge.cget("text") == "Not scheduled yet"
        assert day.form.name_field.get() == "" and "was reset" in day.notice.text
        assert dialogs.errors == []
    finally:
        close_app(app)


def test_csv_v2_through_native_dialogs_and_the_way_back_to_week(tmp_path: Path, dialogs, monkeypatch) -> None:
    import app.ui.day_page as day_module

    exported = tmp_path / "day.csv"
    chosen = {"open": str(exported)}
    monkeypatch.setattr(day_module.filedialog, "asksaveasfilename", lambda **_: str(exported))
    monkeypatch.setattr(day_module.filedialog, "askopenfilename", lambda **_: chosen["open"])
    monkeypatch.setattr(day_module, "ChoiceDialog", lambda parent, *, on_choose, **_: on_choose("append"))

    app = open_app(tmp_path / "csv.db", tmp_path)
    try:
        day = app.pages["day"]
        fill_form(day, name="Plan", duration="30")
        day.form.submit_button.invoke()
        pump(app)
        day.export_button.invoke()
        pump(app)
        assert "CSV format v2" in day.notice.text
        text = exported.read_text(encoding="utf-8")
        assert text.startswith("record_type,id,task_id,date")
        exported.write_text(text.replace(",Plan,", ",Plan (renamed),"), encoding="utf-8")

        day.import_button.invoke()
        pump(app)
        assert "updated 1 task(s)" in dialogs.confirms[-1]  # previewed before anything is written
        assert "Done:" in day.notice.text and tree_names(day) == ["Plan (renamed)"]

        broken = tmp_path / "broken.csv"
        broken.write_text(text.replace(",2,", ",9,"), encoding="utf-8")  # an unsupported format version
        chosen["open"] = str(broken)
        day.import_button.invoke()
        pump(app)
        assert day.notice.text.startswith("Error:") and tree_names(day) == ["Plan (renamed)"]

        legacy = tmp_path / "legacy.csv"
        legacy.write_text("date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
                          "1,Old style,study,t,false,540,720,60,5,\n", encoding="utf-8")
        chosen["open"] = str(legacy)
        day.import_button.invoke()  # legacy only after an explicit Append choice and confirmation
        pump(app)
        assert "Old style" in tree_names(day)

        # Week -> Day keeps the way back.
        week = app.pages["week"]
        app.show_page("week")
        pump(app)
        app.open_day(WEDNESDAY + timedelta(days=1), return_to="week")
        pump(app)
        assert app.shell.current == "day" and day.page_controller.anchor_date == WEDNESDAY + timedelta(days=1)
        assert day.back_button.winfo_manager() == "grid" and "Back to Week" in day.back_button.cget("text")
        day.back_button.invoke()
        pump(app)
        assert app.shell.current == "week" and week.page_controller.anchor_date == WEDNESDAY - timedelta(days=2)
        assert dialogs.errors == []
    finally:
        close_app(app)


def test_task_types_todo_cards_remove_controls_and_bounded_lists(tmp_path: Path, dialogs) -> None:
    """Flexible / Fixed / To Do through the real form and page: points, Early / Late, the To Do cards, the remove
    controls (form, available-task X, To Do X), completion of a fixed block and a To Do, and a restart."""
    from app.ui.day_page import TODO_CARD_SIZE, TODO_NAME_CHARS
    from tests.ui.test_desktop_app import board_names, press

    path = tmp_path / "kinds.db"
    long_name = "Buy " + "oat milk " * 30
    app = open_app(path, tmp_path)
    try:
        day, form = app.pages["day"], app.pages["day"].form

        def add(**fields) -> None:
            fill_form(day, **fields)
            form.submit_button.invoke()
            pump(app)

        add(name="Read", duration="30", preferred="Early", points="40")
        add(name="Write", duration="30", preferred="Late")
        add(name="Class", fixed=True, start="540", end="600", points="30")
        form.set_kind("todo")
        form.name_field.variable.set(long_name)
        form.points_field.variable.set("15")
        form.submit_button.invoke()
        pump(app, until=lambda: len(day.todo_widgets) == 1)
        assert dialogs.errors == [] and form.kind == "todo"  # the next one is a To Do again

        # The To Do is a bounded sticky note, not a schedule row; available tasks keep a fixed width too.
        card = day.todo_widgets[0]
        assert card["task"].points == 15 and card["points"].cget("text") == "15 pts" and not card["completed"]
        assert card["frame"].cget("height") == day.todo_card_size <= TODO_CARD_SIZE  # bounded, whatever the name
        assert len(card["name"].cget("text")) <= TODO_NAME_CHARS < len(long_name)
        assert day.todo_area._parent_frame.winfo_manager() == "grid" and sorted(tree_names(day)) == ["Class", "Read", "Write"]
        assert sorted(chip.task.name for chip in day.chips) == ["Read", "Write"]
        assert all(chip.remove_button.winfo_exists() for chip in day.chips)

        run_make_schedule(app, day)
        pump(app)
        assert sorted(item.name for item in day.snapshot.timeline) == ["Class", "Read", "Write"]  # never the To Do
        assert item_named(day, "Read").end_minute <= 480 and item_named(day, "Write").start_minute >= 960

        # Selecting a scheduled task opens it in the form for reconfiguration, with Remove.
        assert form.remove_button.winfo_manager() == ""  # adding: no Remove
        day.edit_ref(item_named(day, "Read").ref)
        pump(app, until=lambda: form.editing)
        assert (form.kind, form.points_field.get(), form.preferred_select.get()) == ("task", "40", "Early")
        assert form.remove_button.winfo_manager() == "grid" and form.remove_button.cget("text") == "Remove task"
        form.remove_button.invoke()
        pump(app, until=lambda: "Read" not in tree_names(day))
        assert not form.editing and form.remove_button.winfo_manager() == ""

        # The X of an available task removes it at once.
        add(name="Extra", duration="20")
        pump(app, until=lambda: [chip.task.name for chip in day.chips] == ["Extra"])
        day.chips[0].remove_button.invoke()
        pump(app, until=lambda: day.chips == [])
        assert "Extra" not in tree_names(day)

        # Completion: the fixed block on the board, the To Do with its Done box.
        pump(app, until=lambda: "Class" in board_names(day)["pending"])
        press(app, day, "Class", "right")
        pump(app, until=lambda: "Class" in board_names(day)["completed"])
        pump(app, until=lambda: day.todo_widgets and day.todo_widgets[0]["done"].cget("state") == "normal")
        day.todo_widgets[0]["done"].toggle()
        pump(app, until=lambda: day.todo_widgets and day.todo_widgets[0]["completed"])
        executions = {e.task_name: e for e in app.services.execution_controller.list_executions().value}
        assert (executions["Class"].points, executions["Class"].status.value) == (30, "completed")
        todo_execution = executions[long_name.strip()]
        assert (todo_execution.points, todo_execution.status.value, todo_execution.canonical_planned_start) == (
            15, "completed", None)
    finally:
        close_app(app)

    app = open_app(path, tmp_path)  # after a restart everything is as it was saved
    try:
        day = app.pages["day"]
        pump(app, until=lambda: day.todo_widgets and day.todo_widgets[0]["completed"])
        assert sorted(item.name for item in day.snapshot.timeline) == ["Class", "Write"]
        pump(app, until=lambda: "Class" in board_names(day)["completed"])
        day.todo_widgets[0]["remove"].invoke()  # the To Do's own X
        pump(app, until=lambda: day.todo_widgets == [])
        assert day.todo_area._parent_frame.winfo_manager() == "" and day.snapshot.todos == []
    finally:
        close_app(app)


def test_the_workspace_fits_the_whole_day_and_shows_todos_eight_per_row(tmp_path: Path, dialogs) -> None:
    """One workspace: the 24-hour timeline fitted to its width (no horizontal scrolling, vertical labels on
    narrow blocks) above the To Do grid -- eight notes per row, two rows (16) visible, more scroll down."""
    from app.planning.models import TODO_PLACEHOLDER_MINUTES, Task, TaskKind
    from app.ui.day_page import TODO_CARD_SIZE, TODO_COLUMNS, TODO_GAP

    app = open_app(tmp_path / "workspace.db", tmp_path)
    try:
        app.deiconify()
        pump(app)
        day = app.pages["day"]
        planning = app.services.planning_controller
        timeline = day.schedule_canvas
        assert timeline.master is day.workspace and day.todo_area._parent_frame.master is day.workspace

        fill_form(day, name="Lecture", fixed=True, start="540", end="630")
        day.form.submit_button.invoke()
        pump(app)
        fill_form(day, name="Read", duration="30", preferred="Late")
        day.form.submit_button.invoke()
        pump(app)
        run_make_schedule(app, day)
        pump(app, until=lambda: timeline.item_bounds(item_named(day, "Read").key) is not None)
        canvas, geometry = timeline.canvas, timeline.geometry
        pump(app, until=lambda: abs(timeline.geometry.width() - canvas.winfo_width()) <= 2)
        geometry = timeline.geometry
        assert geometry.x(0) >= 0 and geometry.x(1440) <= canvas.winfo_width()  # 12 AM to 12 AM, all visible
        assert not canvas.cget("xscrollcommand") and len(canvas.find_withtag("hour")) >= 8
        read = item_named(day, "Read")
        assert canvas.find_withtag(f"vertical:{read.key}")  # a narrow block is named vertically
        timeline.select(read.key)
        pump(app)
        assert "Read:" in timeline.details.cget("text")

        def todo_rows() -> list[tuple[int, int]]:
            return [(int(w["frame"].grid_info()["row"]), int(w["frame"].grid_info()["column"]))
                    for w in day.todo_widgets]

        def add_todos(count: int) -> None:
            for index in range(count):
                assert planning.add_or_update_task(Task(
                    name=f"Note {len(day.todo_widgets) + index + 1}", category="errand", kind=TaskKind.TODO,
                    points=index, estimated_duration_minutes=TODO_PLACEHOLDER_MINUTES,
                    preferred_dates=[day.page_controller.anchor_date])).ok
            wanted = len(day.todo_widgets) + count
            day.reload()
            pump(app, until=lambda: len(day.todo_widgets) == wanted)

        add_todos(16)
        assert int(timeline.grid_info()["row"]) < int(day.todo_area._parent_frame.grid_info()["row"])  # below it
        assert TODO_COLUMNS == 8 and todo_rows() == [(index // 8, index % 8) for index in range(16)]
        size = day.todo_card_size
        two_rows = day.todo_area._desired_height
        assert two_rows == 2 * (size + TODO_GAP) and size <= TODO_CARD_SIZE == 150  # a quarter lower than before
        assert day.todo_area._scrollbar.winfo_manager() == ""  # 16 fit in the two rows: no scrollbar
        assert not day.todo_area._parent_canvas.cget("xscrollcommand")  # and never a sideways one
        widths = {w["frame"].winfo_width() for w in day.todo_widgets}
        assert max(widths) - min(widths) <= 2 and abs(max(widths) - size) <= 40, (widths, size)  # equal, near-square
        window = (app.winfo_width(), app.winfo_height())

        add_todos(2)  # a third row: the grid keeps its two rows and scrolls; the window does not grow
        assert todo_rows()[-1] == (2, 1) and day.todo_area._desired_height == two_rows
        assert day.todo_area._scrollbar.winfo_manager() == "grid"
        assert (app.winfo_width(), app.winfo_height()) == window
    finally:
        close_app(app)
