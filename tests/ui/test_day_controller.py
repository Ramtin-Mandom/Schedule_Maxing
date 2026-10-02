"""Headless tests of the desktop Day Schedule presenter (app/ui/day_controller.py, Milestone 4
Prompt 4) through the desktop's own services (open_app_services -> PlanningController -> the
shared workflow; no HTTP, no display): exact-minute timeline items with the fixed block's own
category, display-only free gaps inside the effective window, the engine choice (labels,
Normal fallback, nothing written at startup, per-date isolation, reset to the default),
preference inheritance with the absent/value/null layer semantics, first generation, the
unchanged no-op, incremental stability, engine changes that do or do not need a regeneration,
failures and empty runs that keep the previous schedule, the previewed Reset Day and the
canonical CSV v2 round trip with its preview and refusals. Every database is temporary."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.planning.csv_import import ImportMode
from app.planning.models import FixedBlock, LocalTimeWindow, RecurrenceFrequency, RecurrenceSpec, Task
from app.planning.preferences import (
    ENGINE_DESCRIPTIONS,
    ENGINE_LABELS,
    DayWindowSpec,
    OptimizerMode,
    PreferenceOverrides,
    RewardPreferencesOverride,
)
from app.planning.workflow import Freshness
from app.ui import background
from app.ui import preferences_model as prefs
from app.ui.app_services import open_app_services
from app.ui.day_controller import ENGINE_EXPLANATIONS, DayScheduleController, assign_lanes, free_gaps
from app.ui.day_timeline import TimelineGeometry, hour_label

DAY = date(2026, 9, 24)
NEXT = DAY + timedelta(days=1)
TZ = "America/Toronto"


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "day.db"


@pytest.fixture
def services(db_path: Path, tmp_path: Path):
    opened = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    yield opened
    opened.close()


def day_page(services, day: date = DAY) -> DayScheduleController:
    return DayScheduleController(services.planning_controller, anchor_date=day, timezone=TZ)


def ok(result):
    assert result.ok, result.error
    return result.value


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(TZ))


def block(label: str, start: tuple[int, int], end: tuple[int, int], category: str = "class", day: date = DAY) -> FixedBlock:
    return FixedBlock(label=label, category=category, planned_date=day, timezone=TZ, planned_start=local(day, *start),
                      planned_end=local(day, *end))


def add_task(services, name: str, *, duration: int = 60, window: tuple[int, int] | None = None, day: date | None = DAY,
             **extra) -> Task:
    fields = dict(name=name, category="study", estimated_duration_minutes=duration, priority=5, **extra)
    if window is not None:
        fields["preferred_time_window"] = LocalTimeWindow(start_minute=window[0], end_minute=window[1])
    if day is not None and "required_date" not in extra:
        fields["preferred_dates"] = [day]
    return ok(services.planning_controller.add_or_update_task(Task(**fields)))


def rows(connection, table: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]


def state(connection) -> tuple:
    return tuple(rows(connection, name) for name in ("tasks", "fixed_blocks", "scheduled_tasks", "schedule_generations",
                                                     "preference_overrides", "task_tags", "sync_dirty"))


def placements(services, day: date = DAY) -> dict:
    return {p.task_id: p for p in ok(services.planning_controller.get_placements(day))}


# -----------------------------------------------------------------------------
# Timeline, free time, available tasks
# -----------------------------------------------------------------------------


def test_pure_layout_helpers() -> None:
    geometry = TimelineGeometry()
    assert geometry.x(613) - geometry.x(600) == 13  # one pixel per minute: a 13-minute task is 13 px wide
    assert geometry.x(1440) - geometry.x(0) == 1440
    assert [hour_label(hour) for hour in (0, 9, 12, 13, 24)] == ["12 AM", "9 AM", "12 PM", "1 PM", "12 AM"]
    assert assign_lanes([(0, 60), (30, 90), (60, 120), (100, 110)]) == [0, 1, 0, 1]
    gaps = free_gaps((480, 1080), [(420, 540), (600, 613), (613, 700), (1000, 1200)])
    assert [(gap.start_minute, gap.end_minute) for gap in gaps] == [(540, 600), (700, 1000)]
    assert gaps[0].text == "Free 9:00 AM – 10:00 AM (1 h)"


def test_exact_minutes_fixed_category_colors_and_free_gaps_are_display_only(services) -> None:
    controller = services.planning_controller
    ok(controller.save_fixed_block(block("Lecture", (9, 0), (10, 30))))
    ok(controller.save_fixed_block(block("Lab", (13, 0), (14, 0), category="chem_lab")))  # an imported, unknown category
    ok(controller.set_date_overrides(DAY, PreferenceOverrides(day_window=DayWindowSpec(start_minute=480,
                                                                                         end_minute=1080))))
    flash = add_task(services, "Flashcards", duration=13, window=(613, 700))
    page = day_page(services)

    before = ok(page.load())
    assert before.freshness == Freshness.NONE and before.freshness_label == "Not scheduled yet"
    assert [(item.name, item.kind, item.category) for item in before.timeline] == [
        ("Lecture", "fixed", "class"), ("Lab", "fixed", "chem_lab")]  # fixed blocks appear before any generation
    assert [task.name for task in before.unplaced] == ["Flashcards"]
    assert before.window == (480, 1080)

    run = ok(page.make_schedule())
    assert run.status == "generated"
    snapshot = run.snapshot
    item = next(item for item in snapshot.timeline if item.name == "Flashcards")
    assert (item.start_minute, item.end_minute) == (630, 643)  # right after the lecture, exactly 13 minutes
    assert item.time_text == "10:30 AM – 10:43 AM" and item.kind == "scheduled" and item.ref.id == flash.id
    lecture = next(item for item in snapshot.timeline if item.name == "Lecture")
    assert lecture.time_text == "9:00 AM – 10:30 AM" and lecture.category == "class"
    assert [(gap.start_minute, gap.end_minute) for gap in snapshot.free_gaps] == [
        (480, 540), (643, 780), (840, 1080)]  # only inside the 8 AM - 6 PM window
    assert snapshot.unplaced == [] and snapshot.freshness_label == "Current"

    connection = services.connection
    saved = state(connection)
    ok(page.load())
    assert state(connection) == saved  # free time is never stored
    assert len(rows(connection, "fixed_blocks")) == 2 and len(rows(connection, "tasks")) == 1


def test_today_is_the_planning_timezone_date(services) -> None:
    page = day_page(services)
    # 02:30 UTC on Sep 25 is still Sep 24 in Toronto.
    assert page.today(datetime(2026, 9, 25, 2, 30, tzinfo=ZoneInfo("UTC"))) == DAY
    fixed = DayScheduleController(services.planning_controller, anchor_date=DAY, timezone=TZ, today=lambda: NEXT)
    assert fixed.today() == NEXT


# -----------------------------------------------------------------------------
# Engine choice
# -----------------------------------------------------------------------------


def test_engine_options_come_from_the_enum_and_catalog_with_central_labels(services) -> None:
    assert set(ENGINE_LABELS) == set(OptimizerMode) == set(ENGINE_EXPLANATIONS) == set(ENGINE_DESCRIPTIONS)
    options = day_page(services).engine_options()
    assert [(o.mode, o.label) for o in options] == [
        (OptimizerMode.PRECISE_GREEDY, "Normal"), (OptimizerMode.ADHD_FRIENDLY, "ADHD friendly"),
        (OptimizerMode.EARLY_FINISH, "Early finish"), (OptimizerMode.NIGHT_OWL, "Night owl"),
        (OptimizerMode.CATCH_UP, "Catch-up")]
    assert "quarter hour" in options[1].explanation and "Durations never change" in options[1].explanation
    assert "any minute" in options[0].explanation


def test_normal_is_the_fallback_nothing_is_written_at_startup_and_defaults_are_kept(services) -> None:
    connection = services.connection
    page = day_page(services)
    before = state(connection)
    snapshot = ok(page.load())
    assert snapshot.engine.effective == OptimizerMode.PRECISE_GREEDY and not snapshot.engine.overridden
    assert state(connection) == before  # opening the date writes no preference

    ok(services.planning_controller.set_engine_mode(OptimizerMode.ADHD_FRIENDLY))  # an existing user default
    reopened = day_page(services)
    assert ok(reopened.load()).engine.effective == OptimizerMode.ADHD_FRIENDLY  # respected, not overwritten
    assert ok(services.planning_controller.user_preferences()).overrides.optimizer_mode == OptimizerMode.ADHD_FRIENDLY


def test_the_engine_choice_is_saved_for_this_date_only_and_can_be_reset(services) -> None:
    controller = services.planning_controller
    ok(controller.set_user_overrides(PreferenceOverrides(reward=RewardPreferencesOverride(weight_importance=7))))
    ok(controller.set_date_overrides(DAY, PreferenceOverrides(day_window=DayWindowSpec(start_minute=420,
                                                                                         end_minute=1320))))
    page = day_page(services)
    snapshot = ok(page.load())
    user_before = ok(controller.user_preferences())

    changed = ok(page.set_engine(OptimizerMode.ADHD_FRIENDLY, expected_version=snapshot.preference_version))
    assert changed.engine.effective == OptimizerMode.ADHD_FRIENDLY and changed.engine.overridden
    layer = ok(controller.date_preferences(DAY)).overrides
    assert layer.optimizer_mode == OptimizerMode.ADHD_FRIENDLY and layer.day_window.start_minute == 420  # kept
    assert ok(controller.user_preferences()) == user_before  # user defaults untouched
    assert ok(day_page(services, NEXT).load()).engine.effective == OptimizerMode.PRECISE_GREEDY  # isolated

    # Persisted: a new session shows it; a stale precondition changes nothing.
    assert ok(day_page(services).load()).engine.effective == OptimizerMode.ADHD_FRIENDLY
    refused = page.set_engine(OptimizerMode.PRECISE_GREEDY, expected_version=snapshot.preference_version)
    assert not refused.ok and "changed elsewhere" in refused.error
    assert refused.value.engine.effective == OptimizerMode.ADHD_FRIENDLY
    assert not page.set_engine("annealing", expected_version=changed.preference_version).ok

    reset = ok(page.set_engine(None, expected_version=changed.preference_version))
    assert reset.engine.effective == OptimizerMode.PRECISE_GREEDY and not reset.engine.overridden
    layer = ok(controller.date_preferences(DAY)).overrides
    assert layer.optimizer_mode is None and layer.day_window.start_minute == 420

    # With nothing else in the layer, removing the engine removes the layer.
    other = day_page(services, NEXT)
    set_next = ok(other.set_engine(OptimizerMode.ADHD_FRIENDLY, expected_version=None))
    ok(other.set_engine(None, expected_version=set_next.preference_version))
    assert ok(controller.date_preferences(NEXT)) is None


# -----------------------------------------------------------------------------
# Day Preferences
# -----------------------------------------------------------------------------


def row(view, key: str) -> prefs.PreferenceRow:
    return next(row for row in view.rows if row.spec.key == key)


def test_preferences_show_inheritance_and_keep_absent_value_null_semantics(services) -> None:
    controller = services.planning_controller
    ok(controller.set_user_overrides(PreferenceOverrides(
        category_multipliers={"study": 2.0}, reward=RewardPreferencesOverride(min_gap_between_tasks_minutes=15))))
    page = day_page(services)
    view = ok(page.preferences())
    keys = [r.spec.key for r in view.rows]
    assert "reward.weight_category_bonus" not in keys  # never read by the engines: not offered
    assert not any("short_gap" in key for key in keys)  # ADHD-only fields hidden for Normal
    study = row(view, "category_multipliers:study")
    assert (study.effective_text, study.state, study.source) == ("2", "inherited", "Inherited from your defaults")
    assert row(view, "reward.min_gap_between_tasks_minutes").effective_text == "15 min"
    assert "soft preference" in row(view, "reward.min_gap_between_tasks_minutes").spec.help

    view = ok(page.save_preference("category_multipliers:study", "3", expected_version=view.layer_version))
    study = row(view, "category_multipliers:study")
    assert (study.effective_text, study.inherited_text, study.state) == ("3", "2", "set")
    view = ok(page.clear_preference("category_multipliers:study", expected_version=view.layer_version))
    assert row(view, "category_multipliers:study").state == "cleared"
    assert row(view, "category_multipliers:study").effective_text == "1 (neutral)"
    assert ok(controller.date_preferences(DAY)).overrides.category_multipliers == {"study": None}  # stored null
    view = ok(page.inherit_preference("category_multipliers:study", expected_version=view.layer_version))
    assert row(view, "category_multipliers:study").effective_text == "2"
    assert ok(controller.date_preferences(DAY)) is None  # absent again: an empty layer is removed

    view = ok(page.save_preference("reward.min_gap_between_tasks_minutes", "45", expected_version=None))
    view = ok(page.save_preference("day_window", ("8:00 AM", "6:00 PM"), expected_version=view.layer_version))
    assert row(view, "day_window").effective_text == "8:00 AM – 6:00 PM"
    assert ok(controller.resolve_preferences(DAY)).reward.min_gap_between_tasks_minutes == 45
    assert ok(controller.resolve_preferences(NEXT)).reward.min_gap_between_tasks_minutes == 15  # other dates inherit

    stored = state(services.connection)
    for key, value in (("reward.weight_importance", "lots"), ("reward.max_time_distance_minutes", "0"),
                       ("day_window", ("6:00 PM", "8:00 AM"))):
        refused = page.save_preference(key, value, expected_version=view.layer_version)
        assert not refused.ok and refused.error
    assert state(services.connection) == stored  # a refused value saves nothing
    assert not page.save_preference("reward.weight_importance", "4", expected_version=None).ok  # stale version

    ok(page.set_engine(OptimizerMode.ADHD_FRIENDLY, expected_version=view.layer_version))
    view = ok(page.preferences())
    assert any(r.spec.key == "reward.short_gap_bonus_weight" for r in view.rows)  # the active engine's controls

    view = ok(page.reset_date_preferences(expected_version=view.layer_version))
    assert view.layer_version is None and ok(controller.date_preferences(DAY)) is None
    user = ok(controller.user_preferences()).overrides
    assert user.category_multipliers == {"study": 2.0} and user.reward.min_gap_between_tasks_minutes == 15


# -----------------------------------------------------------------------------
# Make Schedule
# -----------------------------------------------------------------------------


def test_first_run_no_op_incremental_additions_and_reopen(services, db_path, tmp_path) -> None:
    read = add_task(services, "Read", window=(540, 720))
    write = add_task(services, "Write", window=(540, 720))
    page = day_page(services)
    first = ok(page.make_schedule())
    assert first.status == "generated" and first.snapshot.freshness == Freshness.CURRENT
    kept = {task_id: (p.id, p.planned_start, p.planned_end) for task_id, p in placements(services).items()}
    assert set(kept) == {read.id, write.id}

    connection = services.connection
    saved = state(connection)
    again = ok(page.make_schedule())
    assert again.status == "already_current" and "nothing was saved" in again.message
    assert state(connection) == saved  # no record, version, timestamp or sync change

    extra = add_task(services, "Review", duration=30)
    assert ok(page.load()).freshness_label == "Out of date"
    added = ok(page.make_schedule())
    assert added.status == "generated" and added.message.startswith("Kept 2 scheduled task(s) in place and added 1")
    now = placements(services)
    assert {task_id: (p.id, p.planned_start, p.planned_end) for task_id, p in now.items() if task_id in kept} == kept
    assert extra.id in now

    services.close()
    reopened = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    try:
        snapshot = ok(day_page(reopened).load())
        assert snapshot.freshness == Freshness.CURRENT and len(snapshot.timeline) == 3
    finally:
        reopened.close()


def test_an_engine_change_needs_a_regeneration_only_when_kept_work_no_longer_fits(services) -> None:
    task = add_task(services, "Essay", duration=45, window=(547, 700))  # Normal starts it at 9:07 AM
    page = day_page(services)
    ok(page.make_schedule())
    assert placements(services)[task.id].planned_start == local(DAY, 9, 7)

    snapshot = ok(page.load())
    snapshot = ok(page.set_engine(OptimizerMode.ADHD_FRIENDLY, expected_version=snapshot.preference_version))
    assert snapshot.freshness == Freshness.STALE and "engine changed from Normal to ADHD friendly" in snapshot.freshness_detail
    saved = state(services.connection)
    run = ok(page.make_schedule())
    assert run.status == "needs_regeneration" and "quarter hours" in run.problems[0] and run.problems[0].startswith("Essay")
    assert state(services.connection) == saved  # explained, nothing changed

    regenerated = ok(page.regenerate_for(DAY))
    assert regenerated.status == "generated" and regenerated.snapshot.freshness == Freshness.CURRENT
    start = placements(services)[task.id].planned_start
    assert start.astimezone(ZoneInfo(TZ)).minute % 15 == 0

    # Back to Normal: the quarter-hour placement still fits, so it is kept rather than regenerated.
    before = placements(services)[task.id]
    snapshot = ok(page.set_engine(OptimizerMode.PRECISE_GREEDY, expected_version=regenerated.snapshot.preference_version))
    kept = ok(page.make_schedule())
    assert kept.status == "generated" and placements(services)[task.id].id == before.id
    assert placements(services)[task.id].planned_start == before.planned_start


def test_failures_and_empty_runs_keep_the_previous_schedule(services) -> None:
    controller = services.planning_controller
    task = add_task(services, "Read", window=(540, 720))
    page = day_page(services)
    ok(page.make_schedule())
    previous = placements(services)[task.id]

    # No capacity: the whole day becomes a fixed block, so nothing fits.
    ok(controller.save_fixed_block(block("Trip", (0, 0), (23, 59))))
    run = ok(page.make_schedule())
    assert run.status == "needs_regeneration"  # the kept placement overlaps the new block
    saved = state(services.connection)
    empty = ok(page.regenerate_for(DAY))
    assert empty.status == "nothing_placed" and "previous schedule was kept" in empty.message
    assert empty.reasons and empty.reasons[0].startswith("Read — Not allocated to this date: no candidate date")
    assert state(services.connection) == saved and placements(services)[task.id] == previous

    # A required task that allocation cannot fit is reported as required; nothing is saved.
    add_task(services, "Exam prep", duration=120, required=True)
    required_run = ok(page.regenerate_for(DAY))
    assert required_run.status == "nothing_placed" and required_run.reasons[0].startswith("Required: Exam prep")
    assert placements(services)[task.id] == previous

    # A required task the day engine cannot place (the free time is split in two): the run fails, the saved
    # schedule of that date stays exactly as it was, and the failure names the task.
    walk = add_task(services, "Walk", duration=30, window=(480, 600), day=NEXT)
    other = day_page(services, NEXT)
    ok(other.make_schedule())
    walk_before = placements(services, NEXT)[walk.id]
    for label, start, end in (("Night", (0, 0), (8, 0)), ("Class", (10, 0), (11, 0)), ("Work", (13, 0), (23, 59))):
        ok(controller.save_fixed_block(block(label, start, end, day=NEXT)))
    add_task(services, "Thesis", duration=150, required=True, day=NEXT)
    saved = state(services.connection)
    failed = other.regenerate_for(NEXT)
    assert not failed.ok and failed.value.status == "failed"
    assert failed.value.reasons[0].startswith("Required: Thesis")
    assert "previous schedule is unchanged" in failed.error
    assert state(services.connection) == saved and placements(services, NEXT)[walk.id] == walk_before


# -----------------------------------------------------------------------------
# Reset Day
# -----------------------------------------------------------------------------


def test_reset_day_previews_then_deletes_only_this_dates_planning(services) -> None:
    controller = services.planning_controller
    ok(controller.set_user_overrides(PreferenceOverrides(reward=RewardPreferencesOverride(weight_importance=9))))
    today = add_task(services, "Today", window=(540, 720))
    undated = add_task(services, "Someday", day=None)
    tomorrow = add_task(services, "Tomorrow", day=NEXT)
    ok(controller.save_fixed_block(block("Class", (13, 0), (14, 0))))
    page = day_page(services)
    ok(page.set_engine(OptimizerMode.ADHD_FRIENDLY, expected_version=None))
    ok(page.make_schedule())

    plan = ok(page.reset_plan())
    assert not plan.blocked
    assert "1 task(s) planned for this date, 1 fixed block(s)" in plan.message
    assert "date preferences and engines set for this date" in plan.message and "undated tasks" in plan.message
    saved = state(services.connection)
    assert state(services.connection) == saved  # previewing (or cancelling) deletes nothing

    reset = ok(page.reset_day(plan))
    assert reset.timeline == [] and reset.freshness == Freshness.NONE
    assert reset.engine.effective == OptimizerMode.PRECISE_GREEDY  # the date override went; defaults apply
    names = {task.name for task in ok(controller.list_tasks())}
    assert names == {"Someday", "Tomorrow"} and today.id not in {t.id for t in ok(controller.list_tasks())}
    assert ok(controller.resolve_preferences(DAY)).reward.weight_importance == 9  # weights not zeroed
    assert [task.name for task in reset.unplaced] == ["Someday"]  # the undated task is still available here
    assert undated.id and tomorrow.id

    # A preview is only good for what it showed.
    add_task(services, "Late addition")
    stale_plan = ok(page.reset_plan())
    add_task(services, "Even later")
    refused = page.reset_day(stale_plan)
    assert not refused.ok and "changed since the reset was previewed" in refused.error
    assert {"Late addition", "Even later"} <= {task.name for task in ok(controller.list_tasks())}


# -----------------------------------------------------------------------------
# CSV v2
# -----------------------------------------------------------------------------


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames), list(reader)


def write_rows(path: Path, header: list[str], data: list[dict]) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(data)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def test_canonical_csv_round_trip_preview_updates_and_refusals(services, tmp_path) -> None:
    controller = services.planning_controller
    weekly = add_task(services, "Weekly review", window=(540, 720), day=None,
                      recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.WEEKLY, start_date=DAY, timezone=TZ))
    ok(controller.save_fixed_block(block("Class", (13, 0), (14, 0))))
    page = day_page(services)
    ok(page.make_schedule())
    exported = tmp_path / "day.csv"
    result = ok(page.export_csv(str(exported)))
    assert (result.tasks, result.fixed_blocks, result.placements) == (2, 1, 1)  # the series and its occurrence

    saved = state(services.connection)
    plan = ok(page.csv_plan(str(exported)))
    assert plan.kind == "canonical" and not plan.has_updates and "already up to date" in plan.summary
    imported = ok(page.apply_csv(plan))
    assert "created nothing" in imported.summary
    assert state(services.connection) == saved  # identical records: a no-op

    header, data = read_rows(exported)
    for line in data:
        if line["record_type"] == "task" and line["recurrence"]:  # the series definition
            line["name"] = "Weekly review (renamed)"
    edited = tmp_path / "edited.csv"
    write_rows(edited, header, data)
    plan = ok(page.csv_plan(str(edited)))
    assert plan.has_updates and "updated 1 task(s)" in plan.summary
    assert state(services.connection) == saved  # the preview wrote nothing
    ok(page.apply_csv(plan))
    stored = ok(controller.get_task(weekly.id))
    assert stored.name == "Weekly review (renamed)" and stored.recurrence == weekly.recurrence  # identity and rule kept

    # Content already stored is a no-op; a different change made from the outdated export is refused (its
    # version no longer matches), in the preview and nothing is written.
    assert "already up to date: 2 task(s)" in ok(page.apply_csv(plan)).summary
    after_update = state(services.connection)
    conflicting = tmp_path / "conflicting.csv"
    write_rows(conflicting, header, [{**line, "name": "Something else"} if line["record_type"] == "task" else line
                                     for line in data])
    refused = page.csv_plan(str(conflicting))
    assert not refused.ok and "nothing was changed" in refused.error and "version" in refused.error.lower()
    assert state(services.connection) == after_update

    duplicate = tmp_path / "duplicate.csv"
    task_row = next(line for line in data if line["record_type"] == "task")
    write_rows(duplicate, header, [*data, dict(task_row)])
    assert "already appears on line" in page.csv_plan(str(duplicate)).error
    bad = tmp_path / "bad.csv"
    write_rows(bad, header, [{**line, "format_version": "3"} for line in data])
    assert "format_version must be 2" in page.csv_plan(str(bad)).error
    bad_date = tmp_path / "bad_date.csv"
    write_rows(bad_date, header, [{**line, "date": "2026-13-40"} if line["record_type"] == "fixed_block" else line
                                  for line in data])
    assert not page.csv_plan(str(bad_date)).ok
    missing_ref = tmp_path / "missing_ref.csv"
    write_rows(missing_ref, header, [{**line, "task_id": "00000000-0000-0000-0000-000000000001"}
                                     if line["record_type"] == "placement" else line for line in data])
    assert not page.csv_plan(str(missing_ref)).ok
    assert state(services.connection) == after_update  # none of the refused files changed anything

    legacy = tmp_path / "legacy.csv"
    legacy.write_text("date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
                      "1,Old style,study,t,false,540,720,60,5,\n", encoding="utf-8")
    plan = ok(page.csv_plan(str(legacy)))
    assert plan.kind == "legacy" and "not the canonical format version 2" in plan.summary
    assert not page.apply_csv(plan).ok  # never imported without an explicit choice
    ok(page.apply_csv(plan, legacy_mode=ImportMode.APPEND))
    assert "Old style" in {task.name for task in ok(controller.list_tasks())}
