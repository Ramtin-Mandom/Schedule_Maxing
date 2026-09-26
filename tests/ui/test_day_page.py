"""The Day Schedule as real widgets (Milestone 4, Prompt 4) against a temporary database: the
page opens on the date, fixed blocks appear at once in their category's color, Make Schedule
(a background run) draws each task at its exact minutes, an unchanged run writes nothing, the
Engine choice beside Make Schedule saves only this date and is disabled while saving,
Day Preferences edits the date layer natively, everything survives a reopen, Reset Day asks
first and can be cancelled, CSV v2 export/import goes through native dialogs with a preview,
Week -> Day keeps the way back, and Execute stays reachable. Skipped without a display
(see tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.planning.preferences import OptimizerMode
from app.ui import theme
from app.ui.day_timeline import TimelineGeometry
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
        assert day.engine_select.get() == "Normal" and day.engine_select.values == ["Normal", "ADHD friendly"]
        assert rows_of(app, "preference_overrides") == []  # nothing is written at startup

        # A fixed block shows at once, in its own category's color.
        fill_form(day, name="Gym", fixed=True, start="540", end="630")
        day.form.category_select.variable.set("exercise")
        day.form.submit_button.invoke()
        fill_form(day, name="Read", duration="13", start="613", end="720")
        day.form.submit_button.invoke()
        assert dialogs.errors == [] and [item.name for item in day.snapshot.timeline] == ["Gym"]
        assert [chip.task.name for chip in day.chips] == ["Read"]  # available, not yet scheduled
        pump(app)
        gym = day.schedule_canvas.item_bounds(item_named(day, "Gym").key)
        box = day.schedule_canvas.canvas.find_withtag(f"box:{item_named(day, 'Gym').key}")[0]
        assert day.schedule_canvas.canvas.itemcget(box, "fill") == theme.resolve(theme.category_style("exercise").fill)
        geometry = TimelineGeometry()
        assert (gym[0], gym[2]) == (geometry.x(540), geometry.x(630))

        run_make_schedule(app, day)
        assert day.notice.text.startswith("Done: Scheduled 1 task(s)") and day.freshness_badge.cget("text") == "Current"
        read = item_named(day, "Read")
        assert (read.start_minute, read.end_minute, read.time_text) == (630, 643, "10:30 AM – 10:43 AM")
        pump(app)
        bounds = day.schedule_canvas.item_bounds(read.key)
        assert bounds[2] - bounds[0] == 13  # drawn exactly 13 minutes wide
        assert day.chips == [] and any(gap.start_minute == 643 for gap in day.snapshot.free_gaps)

        saved = saved_state(app)
        run_make_schedule(app, day)
        assert "already current" in day.notice.text and saved_state(app) == saved

        # The keyboard way to the same actions: the timeline selects and describes an item.
        day.schedule_canvas.select(read.key)
        assert "Read: 10:30 AM – 10:43 AM (13 min), scheduled" in day.schedule_canvas.details.cget("text")

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
        assert day.engine_select.get() == "Normal"  # another date is unaffected
        day.shift_date(-1)

        # A 13-minute task still fits the quarter-hour rule, so nothing is regenerated: it is kept.
        run_make_schedule(app, day)
        assert day.notice.text.startswith("Done: Kept 1 scheduled task(s) in place")
        assert item_named(day, "Read").placement_id == read.placement_id
        fill_form(day, name="Essay", duration="45", start="600", end="900")
        day.form.submit_button.invoke()
        run_make_schedule(app, day)
        assert item_named(day, "Essay").start_minute % 15 == 0
        assert day.execution_panel is not None and len(day.snapshot.executables) == 2  # Execute stays reachable

        # Day Preferences: a native editor of the date layer, inherited values shown.
        day.open_preferences()
        dialog = day.preferences_dialog
        spacing = "reward.min_gap_between_tasks_minutes"
        assert "Inherited app default" in dialog.editor.rows[spacing].source
        assert "reward.short_gap_bonus_weight" in dialog.editor.rows  # ADHD friendly's own control
        dialog.editor.set_input(spacing, "45")
        dialog.editor.buttons[spacing]["save"].invoke()
        assert dialog.editor.rows[spacing].state == "set" and "saved" in dialog.notice.text
        dialog.editor.set_input("reward.weight_importance", "lots")
        dialog.editor.buttons["reward.weight_importance"]["save"].invoke()
        assert dialog.editor.error_labels["reward.weight_importance"].cget("text").startswith("Error:")
        dialog.primary_button.invoke()
        assert day.freshness_badge.cget("text") == "Out of date"  # the page re-read itself
        run_make_schedule(app, day)
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
        assert "Reset Wed Jun 5?" in dialogs.confirms[-1] and "execution history" in dialogs.confirms[-1]
        assert "nothing was deleted" in day.notice.text and saved_state(app) == before

        dialogs.confirm = True
        day.reset_button.invoke()
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
        day.export_button.invoke()
        assert "CSV format v2" in day.notice.text
        text = exported.read_text(encoding="utf-8")
        assert text.startswith("record_type,id,task_id,date")
        exported.write_text(text.replace(",Plan,", ",Plan (renamed),"), encoding="utf-8")

        day.import_button.invoke()
        assert "updated 1 task(s)" in dialogs.confirms[-1]  # previewed before anything is written
        assert "Done:" in day.notice.text and tree_names(day) == ["Plan (renamed)"]

        broken = tmp_path / "broken.csv"
        broken.write_text(text.replace(",2,", ",9,"), encoding="utf-8")  # an unsupported format version
        chosen["open"] = str(broken)
        day.import_button.invoke()
        assert day.notice.text.startswith("Error:") and tree_names(day) == ["Plan (renamed)"]

        legacy = tmp_path / "legacy.csv"
        legacy.write_text("date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
                          "1,Old style,study,t,false,540,720,60,5,\n", encoding="utf-8")
        chosen["open"] = str(legacy)
        day.import_button.invoke()  # legacy only after an explicit Append choice and confirmation
        assert "Old style" in tree_names(day)

        # Week -> Day keeps the way back.
        week = app.pages["week"]
        app.show_page("week")
        app.open_day(WEDNESDAY + timedelta(days=1), return_to="week")
        assert app.shell.current == "day" and day.page_controller.anchor_date == WEDNESDAY + timedelta(days=1)
        assert day.back_button.winfo_manager() == "grid" and "Back to Week" in day.back_button.cget("text")
        day.back_button.invoke()
        assert app.shell.current == "week" and week.page_controller.anchor_date == WEDNESDAY - timedelta(days=2)
        assert dialogs.errors == []
    finally:
        close_app(app)
