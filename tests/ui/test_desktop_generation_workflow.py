"""The shared scheduling workflow (app/planning/workflow.py) through the desktop's own
entry points -- open_app_services and PlanningController, no HTTP route and no web
package: first generation, the already_current no-op, incremental additions, explicit
full regeneration, history protection, fixed blocks, planning-timezone deadlines,
dependency order, stale inputs, previewed CSV imports, previewed resets and the
preference/engine views the next desktop prompts build on. The legacy Make Schedule
path (schedule_range) keeps its behavior alongside."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.planning import workflow
from app.planning.csv_import import ImportMode
from app.planning.errors import RegenerationRequiredError, StaleInputsError
from app.planning.models import FixedBlock, Task
from app.planning.preferences import DayWindowSpec, OptimizerMode, PreferenceOverrides
from app.planning.workflow import Freshness, GenerationMode
from app.ui import background
from app.ui.app_services import open_app_services

MON, TUE = date(2026, 9, 21), date(2026, 9, 22)
SEP_23 = date(2026, 9, 23)


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


@pytest.fixture
def services(tmp_path: Path):
    opened = open_app_services(tmp_path / "app.db", timezone="UTC", project_root=str(tmp_path))
    yield opened
    opened.close()


def ok(result):
    assert result.ok, result.error
    return result.value


def add(controller, name: str, *, duration: int = 60, priority: int = 5, **extra) -> Task:
    return ok(controller.add_or_update_task(Task(name=name, category="study", estimated_duration_minutes=duration,
                                                 priority=priority, **extra)))


def block(day: date, start_hour: int, hours: int, label: str = "Class", tz: str = "UTC") -> FixedBlock:
    start = datetime(day.year, day.month, day.day, start_hour, tzinfo=ZoneInfo(tz))
    return FixedBlock(label=label, category="class", planned_date=day, timezone=tz, planned_start=start,
                      planned_end=start + timedelta(hours=hours))


def table(connection, name: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY 1")]


def saved_state(connection) -> tuple:
    return tuple(table(connection, name) for name in ("scheduled_tasks", "schedule_generations", "sync_dirty"))


def placements(services, day: date = MON) -> dict:
    return {p.task_id: p for p in ok(services.planning_controller.get_placements(day))}


def test_first_generation_then_an_unchanged_run_writes_nothing(services) -> None:
    controller = services.planning_controller
    read = add(controller, "Read", preferred_dates=[MON])

    first = ok(controller.generate(MON, TUE))
    assert first.status == "generated" and read.id in placements(services)
    assert {d: s.status for d, s in ok(controller.day_freshness([MON, TUE])).items()} == {
        MON: Freshness.CURRENT, TUE: Freshness.CURRENT}

    before = saved_state(services.connection)
    again = ok(controller.generate(MON, TUE))
    assert again.status == "already_current" and again.reschedule is None
    assert saved_state(services.connection) == before  # no id, version, timestamp, provenance or sync change

    # The legacy Make Schedule path still regenerates the page's range, reusing identical placements' ids.
    kept_id = placements(services)[read.id].id
    ok(controller.schedule_range(MON, TUE))
    assert placements(services)[read.id].id == kept_id


def test_incremental_generation_adds_work_around_kept_placements(services) -> None:
    controller = services.planning_controller
    essay = add(controller, "Essay", preferred_dates=[MON])
    ok(controller.generate(MON, MON))
    kept = placements(services)[essay.id]

    urgent = add(controller, "Urgent", priority=10, preferred_dates=[MON])
    outcome = ok(controller.generate(MON, MON, mode=GenerationMode.INCREMENTAL))

    now = placements(services)
    assert outcome.status == "generated" and outcome.kept_ids[MON] == [kept.id]
    assert now[essay.id] == kept  # same id, interval and version
    assert urgent.id in now
    assert now[urgent.id].planned_end <= kept.planned_start or now[urgent.id].planned_start >= kept.planned_end


def test_a_kept_placement_that_no_longer_fits_needs_an_explicit_full_regeneration(services) -> None:
    controller = services.planning_controller
    essay = add(controller, "Essay", preferred_dates=[MON])
    ok(controller.generate(MON, MON))
    stored = ok(controller.get_task(essay.id))
    ok(controller.add_or_update_task(stored.model_copy(update={"estimated_duration_minutes": 90}),
                                     expected_version=stored.version))
    before = saved_state(services.connection)

    refused = controller.generate(MON, MON, mode=GenerationMode.INCREMENTAL)
    assert not refused.ok and isinstance(refused.cause, RegenerationRequiredError)
    assert [problem.reason for problem in refused.cause.problems] == ["duration_changed"]
    assert saved_state(services.connection) == before  # nothing written

    ok(controller.generate(MON, MON))  # the explicit full regeneration
    placement = placements(services)[essay.id]
    assert placement.planned_end - placement.planned_start == timedelta(minutes=90)


def test_full_regeneration_never_moves_work_whose_execution_started(services) -> None:
    controller = services.planning_controller
    essay = add(controller, "Essay", preferred_dates=[MON])
    ok(controller.generate(MON, MON))
    started = placements(services)[essay.id]
    execution = ok(services.execution_controller.get_or_create_canonical_execution(essay, started))
    ok(services.execution_controller.start(execution.id))

    other = add(controller, "Other", priority=10, preferred_dates=[MON])
    outcome = ok(controller.generate(MON, MON))  # full, protect_history (the default)
    now = placements(services)
    assert now[essay.id] == started and started.id in outcome.kept_ids[MON]
    assert other.id in now
    restored = ok(services.execution_controller.find_execution_for_placement(started.id))
    assert restored.id == execution.id  # the history still points at the kept placement


def test_fixed_blocks_and_dependencies_constrain_the_generated_day(services) -> None:
    controller = services.planning_controller
    ok(controller.save_fixed_block(block(MON, 0, 12, "Morning")))
    first = add(controller, "Draft", preferred_dates=[MON])
    second = add(controller, "Revise", preferred_dates=[MON], dependency_ids=[first.id], priority=9)

    ok(controller.generate(MON, MON))
    now = placements(services)
    noon = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    assert all(placement.planned_start >= noon for placement in now.values())
    assert now[second.id].planned_start >= now[first.id].planned_end


def test_deadlines_are_judged_in_the_planning_timezone(tmp_path: Path) -> None:
    """The Tokyo reproduction through the desktop's controller: due 08:00 +09:00, available 00:00-07:00 Tokyo."""
    services = open_app_services(tmp_path / "tokyo.db", timezone="Asia/Tokyo", project_root=str(tmp_path))
    try:
        controller = services.planning_controller
        deadline = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Asia/Tokyo"))
        task = add(controller, "Submit form", duration=30, deadline=deadline)
        ok(controller.set_date_overrides(SEP_23, PreferenceOverrides(
            day_window=DayWindowSpec(start_minute=0, end_minute=7 * 60))))

        preview = ok(controller.preview_allocation(SEP_23, SEP_23))
        assert preview.allocation.assignments == {task.id: SEP_23} and preview.allocation.unallocated == []
        ok(controller.generate(SEP_23, SEP_23, expected_fingerprint=preview.fingerprint))
        [placement] = ok(controller.get_placements(SEP_23))
        assert placement.planned_end <= deadline
    finally:
        services.close()


def test_changed_inputs_refuse_the_save_and_keep_the_previous_schedule(services, monkeypatch) -> None:
    controller = services.planning_controller
    add(controller, "Read", preferred_dates=[MON])
    preview = ok(controller.preview_allocation(MON, MON))
    add(controller, "Arrived after the preview", preferred_dates=[MON])

    stale = controller.generate(MON, MON, expected_fingerprint=preview.fingerprint)
    assert not stale.ok and isinstance(stale.cause, StaleInputsError)
    assert ok(controller.get_placements(MON)) == []

    ok(controller.generate(MON, MON))
    before = saved_state(services.connection)
    real = workflow.generate_selected_day

    def racing(*args, **kwargs):  # another writer (a sync pull, another window) changes a block meanwhile
        services.planning_service.create_fixed_block(block(MON, 20, 1, "Surprise"))
        return real(*args, **kwargs)

    add(controller, "Also new", preferred_dates=[MON])
    monkeypatch.setattr(workflow, "generate_selected_day", racing)
    raced = controller.generate(MON, MON)
    assert not raced.ok and isinstance(raced.cause, StaleInputsError)
    assert saved_state(services.connection)[:2] == before[:2]  # the saved schedule is exactly as it was


def test_reset_is_previewed_then_confirmed_and_keeps_defaults_and_history(services) -> None:
    controller = services.planning_controller
    essay = add(controller, "Essay", required_date=MON)
    undated = add(controller, "Someday")
    ok(controller.set_user_overrides(PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY)))
    ok(controller.set_date_overrides(MON, PreferenceOverrides(optimizer_mode=OptimizerMode.PRECISE_GREEDY)))
    ok(controller.generate(MON, MON))
    placement = placements(services)[essay.id]
    execution = ok(services.execution_controller.get_or_create_canonical_execution(essay, placement))
    ok(services.execution_controller.start(execution.id))

    preview = ok(controller.reset_preview(MON, MON))
    assert essay.id in preview.task_ids and undated.id not in preview.task_ids
    assert placement.id in preview.placements_with_history_ids and not preview.blocked
    assert ok(controller.get_task(essay.id)) is not None  # previewing wrote nothing

    refused = controller.reset_range(MON, MON, confirmation="0" * 64)
    assert not refused.ok and ok(controller.get_task(essay.id)) is not None

    ok(controller.reset_range(MON, MON, confirmation=preview.token))
    assert ok(controller.get_task(essay.id)) is None and ok(controller.get_task(undated.id)) is not None
    assert ok(controller.date_preferences(MON)) is None
    assert ok(controller.user_preferences()).overrides.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
    # The reset date's executions go with its plan.
    assert ok(services.execution_controller.find_execution_for_placement(placement.id)) is None
    assert execution.id not in {item.id for item in ok(services.execution_controller.list_executions())}


def test_a_canonical_csv_is_previewed_without_writing_then_imported(services, tmp_path: Path) -> None:
    controller = services.planning_controller
    essay = add(controller, "Essay", preferred_dates=[MON])
    exported = tmp_path / "planning.csv"
    ok(controller.export_planning_csv(str(exported)))

    before = saved_state(services.connection)
    unchanged = ok(controller.preview_csv_file(str(exported)))
    assert unchanged.unchanged.get("task") == 1 and not unchanged.created.get("task")

    edited = tmp_path / "edited.csv"
    edited.write_text(exported.read_text(encoding="utf-8").replace("Essay", "Essay (final)"), encoding="utf-8")
    refused = controller.preview_csv_file(str(edited))
    assert not refused.ok  # a differing record needs allow_updates
    assert ok(controller.preview_csv_file(str(edited), allow_updates=True)).updated.get("task") == 1
    assert saved_state(services.connection) == before and ok(controller.get_task(essay.id)).name == "Essay"

    legacy = tmp_path / "legacy.csv"
    legacy.write_text("task_name,category\nx,study\n", encoding="utf-8")
    assert not controller.preview_csv_file(str(legacy)).ok

    ok(controller.import_csv_file(str(edited), anchor_date=MON, mode=ImportMode.APPEND, allow_updates=True))
    assert ok(controller.get_task(essay.id)).name == "Essay (final)"


def test_preference_views_and_engine_catalog_for_the_day_and_settings_screens(services) -> None:
    controller = services.planning_controller
    assert set(controller.engine_descriptions()) == set(OptimizerMode)  # all five modes (docs/scheduling-modes.md)
    ok(controller.set_user_overrides(PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY)))
    ok(controller.set_date_overrides(TUE, PreferenceOverrides(optimizer_mode=OptimizerMode.PRECISE_GREEDY)))

    views = ok(controller.preference_views(MON, TUE))
    assert views.user_layer is not None and views.user_layer.version == 1
    monday, tuesday = views.days[MON], views.days[TUE]
    assert monday.date_layer is None and monday.effective.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
    assert tuesday.date_layer is not None and tuesday.effective.optimizer_mode == OptimizerMode.PRECISE_GREEDY
    assert tuesday.inherited.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
