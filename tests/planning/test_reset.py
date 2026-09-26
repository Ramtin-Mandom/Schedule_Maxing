"""The web workflow's explicit, confirmed range reset (PlanningService.reset_preview /
reset_range): what it tombstones and what it keeps, the disclosed cascade,
recurring templates, stale confirmations, and all-or-nothing rollback --
preference layers included."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.errors import EntityInUseError, VersionConflictError
from app.planning.models import FixedBlock, Project, RecurrenceSpec, ScheduledTask, Task
from app.planning.preferences import DayWindowSpec, OptimizerMode, PreferenceOverrides
from app.planning.provenance import GenerationRecord
from app.planning.repository import PlanningRepository

MON, TUE, WED, NEXT_MON = date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 28)


def task(name: str, **extra) -> Task:
    return Task(name=name, category="study", estimated_duration_minutes=60, priority=5, **extra)


def placement(t: Task, day: date, hour: int = 9) -> ScheduledTask:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return ScheduledTask(task_id=t.id, planned_date=day, timezone="UTC", planned_start=start,
                         planned_end=start + timedelta(hours=1), user_id=t.user_id)


def block(day: date, hour: int = 12) -> FixedBlock:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return FixedBlock(label="Lunch", planned_date=day, timezone="UTC", planned_start=start,
                      planned_end=start + timedelta(hours=1))


def dirty(connection) -> list[tuple]:
    return [tuple(r) for r in connection.execute("SELECT entity_type, entity_id, local_rev FROM sync_dirty ORDER BY 1, 2")]


def all_rows(connection) -> dict:
    tables = ("tasks", "fixed_blocks", "scheduled_tasks", "preference_overrides", "schedule_generations", "projects")
    return {table: [tuple(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY id")] for table in tables}


@pytest.fixture
def world(planning_service: PlanningService, planning_repository: PlanningRepository, execution_service: ExecutionService):
    """A week reset over MON..TUE, with records inside and outside it."""
    project = planning_service.create_project(Project(name="Thesis"))
    mon_task = planning_service.create_task(task("Mon task", required_date=MON, project_id=project.id))
    tue_task = planning_service.create_task(task("Tue task", preferred_dates=[TUE, NEXT_MON]))
    backlog = planning_service.create_task(task("Backlog"))
    wed_task = planning_service.create_task(task("Wed task", required_date=WED))
    recurring = planning_service.create_task(
        task("Standup", required_date=MON, recurrence=RecurrenceSpec(frequency="weekly", weekdays=[0]))
    )
    placements = {
        "mon": placement(mon_task, MON), "tue": placement(tue_task, TUE),
        "tue_next": placement(tue_task, NEXT_MON),  # outside the range: the disclosed cascade
        "backlog_in_range": placement(backlog, TUE, 14), "wed": placement(wed_task, WED),
        "standup_mon": placement(recurring, MON, 8), "standup_next": placement(recurring, NEXT_MON, 8),
    }
    planning_service.replace_placements(MON, NEXT_MON, placements.values())
    for day in (MON, TUE, WED):
        planning_service.create_fixed_block(block(day))
    user_layer = planning_service.save_user_preferences(PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY))
    planning_service.save_date_preferences(MON, PreferenceOverrides(day_window=DayWindowSpec(start_minute=480, end_minute=1200)))
    planning_service.save_date_preferences(WED, PreferenceOverrides(day_window=DayWindowSpec(start_minute=480, end_minute=1200)))
    for day in (MON, TUE, WED):
        planning_repository.insert_generation(GenerationRecord(
            planned_date=day, timezone="UTC", engine_mode="precise_greedy", range_start=MON, range_end=WED,
            range_scope="planned", allocation_id=project.id, fingerprint="f" * 64, placements_digest="d" * 64,
            placement_count=1, unscheduled_count=0, total_score=1.0, generated_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        ))
    stored = {p.task_id: p for p in planning_service.list_placements(MON, MON)}
    execution = execution_service.create_canonical_execution(mon_task, stored[mon_task.id])
    return dict(project=project, mon=mon_task, tue=tue_task, backlog=backlog, wed=wed_task, recurring=recurring,
                placements=placements, user_layer=user_layer, execution=execution)


def test_preview_lists_exactly_what_the_reset_touches(planning_service: PlanningService, world, connection) -> None:
    before = (all_rows(connection), dirty(connection))
    preview = planning_service.reset_preview(MON, TUE)

    assert (all_rows(connection), dirty(connection)) == before  # a preview writes nothing
    p = world["placements"]
    assert set(preview.placement_ids) == {p["mon"].id, p["tue"].id, p["backlog_in_range"].id, p["standup_mon"].id}
    assert preview.cascade_placement_ids == [p["tue_next"].id] and preview.has_cascade
    assert set(preview.task_ids) == {world["mon"].id, world["tue"].id}
    assert preview.protected_recurring_task_ids == [world["recurring"].id]
    assert preview.counts == {"placements": 4, "cascade_placements": 1, "schedule_generations": 2,
                              "fixed_blocks": 2, "tasks": 2, "date_preferences": 1}
    assert preview.placements_with_history_ids == [p["mon"].id] and preview.tasks_with_history_ids == [world["mon"].id]
    assert not preview.blocked


def test_reset_applies_the_preview_and_restores_inherited_date_preferences(
    planning_service: PlanningService, execution_service: ExecutionService, world, connection
) -> None:
    before = planning_service.resolve_preferences([MON, TUE], "UTC")
    assert before[MON].day_window.start_minute == 480

    preview = planning_service.reset_preview(MON, TUE)
    result = planning_service.reset_range(MON, TUE, confirmation=preview.token)

    assert result.deleted == preview.counts
    after = planning_service.resolve_preferences([MON, TUE], "UTC")
    assert after[MON].day_window.start_minute == 0  # inherits again
    assert after[MON].optimizer_mode == OptimizerMode.ADHD_FRIENDLY  # the user layer is kept
    assert planning_service.user_preferences() == world["user_layer"]
    assert planning_service.date_preferences(WED) is not None

    remaining = {t.name for t in planning_service.list_tasks()}
    assert remaining == {"Backlog", "Wed task", "Standup"}
    assert planning_service.get_project(world["project"].id) is not None
    assert planning_service.list_placements(MON, TUE) == []
    assert [x.id for x in planning_service.list_placements(NEXT_MON, NEXT_MON)] == [world["placements"]["standup_next"].id]
    assert [x.planned_date for x in planning_service.list_fixed_blocks()] == [WED]
    assert set(planning_service.generation_records(MON, WED)) == {WED}
    assert execution_service.get_execution(world["execution"].id) == world["execution"]  # history untouched


def test_reset_writes_versioned_tombstones_that_sync_will_see(planning_service: PlanningService, world, connection) -> None:
    preview = planning_service.reset_preview(MON, TUE)
    planning_service.reset_range(MON, TUE, confirmation=preview.token)

    mon = planning_service.get_tasks_including_deleted([world["mon"].id])[world["mon"].id]
    assert mon.deleted_at is not None and mon.version == world["mon"].version + 1
    marked = {(kind, entity) for kind, entity, _ in dirty(connection)}
    for record_id in [*preview.placement_ids, *preview.cascade_placement_ids]:
        assert ("placement", str(record_id)) in marked
    for record_id in preview.date_preference_ids:
        assert ("preference", str(record_id)) in marked


def test_a_stale_confirmation_deletes_nothing(planning_service: PlanningService, world, connection) -> None:
    preview = planning_service.reset_preview(MON, TUE)
    planning_service.create_fixed_block(block(TUE, 16))  # the range changed after the preview
    before = (all_rows(connection), dirty(connection))

    with pytest.raises(VersionConflictError, match="changed since the reset was previewed"):
        planning_service.reset_range(MON, TUE, confirmation=preview.token)
    assert (all_rows(connection), dirty(connection)) == before


def test_a_dependency_refusal_rolls_back_the_whole_reset(planning_service: PlanningService, world, connection) -> None:
    preview = planning_service.reset_preview(MON, TUE)
    # After the preview (the previewed records are unchanged), a task outside the range starts depending on one inside.
    planning_service.create_task(task("Follow-up", required_date=WED, dependency_ids=[world["mon"].id]))
    assert planning_service.reset_preview(MON, TUE).token == preview.token
    before = (all_rows(connection), dirty(connection))

    with pytest.raises(EntityInUseError):
        planning_service.reset_range(MON, TUE, confirmation=preview.token)

    assert (all_rows(connection), dirty(connection)) == before  # preference layers and placements included
    assert planning_service.date_preferences(MON) is not None
    assert planning_service.reset_preview(MON, TUE).blocked


def test_reset_of_an_empty_range_is_a_confirmed_no_op(planning_service: PlanningService, connection) -> None:
    preview = planning_service.reset_preview(MON, TUE)
    assert preview.counts == dict.fromkeys(preview.counts, 0)
    result = planning_service.reset_range(MON, TUE, confirmation=preview.token)
    assert result.deleted == preview.counts and dirty(connection) == []


def test_the_desktop_clear_range_keeps_its_older_scope(planning_service: PlanningService, world) -> None:
    cleared = planning_service.clear_range(MON, TUE, include_planning_data=False)
    assert cleared.deleted_tasks == 0 and cleared.deleted_fixed_blocks == 0
    assert planning_service.date_preferences(MON) is not None  # never touched by the older scopes
