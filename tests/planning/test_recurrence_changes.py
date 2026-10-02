"""Scoped changes of recurring series (app/planning/series.py, docs/recurrence.md): this occurrence, this and
every later occurrence (an atomic split with explicit lineage, no overlapping segments, no duplicate slots),
the entire series (in place, or a split at the start for a rule or time-zone change); started/finished and
individually edited occurrences are preserved and named; execution history is never touched; stale versions
change nothing; and an occurrence moved to another date keeps its identity -- the date it left never
recreates its work (replacing the old "recurring moves only within one date" rule)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning import series as series_ops
from app.planning import workflow
from app.planning.application import PlanningService, RangeScope
from app.planning.errors import (
    InvalidEntityError,
    RescheduleRejectedError,
    SeriesConfigurationError,
    VersionConflictError,
)
from app.planning.models import OccurrenceState, RecurrenceSpec, ScheduledTask, Task
from app.planning.recurrence import occurrence_task_id
from app.planning.repository import PlanningRepository
from app.planning.series import EditScope

MON = date(2026, 3, 2)


def day(offset: int) -> date:
    return MON + timedelta(days=offset)


def at(hour: int, when: date) -> datetime:
    return datetime(when.year, when.month, when.day, hour, tzinfo=timezone.utc)


class World:
    def __init__(self, path: Path) -> None:
        self.connection = get_connection(path)
        self.planning = PlanningService(PlanningRepository(self.connection))
        self.executions = ExecutionService(ExecutionRepository(self.connection))

    def series(self, **rule) -> Task:
        spec = RecurrenceSpec(**{"frequency": "daily", "start_date": MON, "timezone": "UTC", **rule})
        return self.planning.create_task(Task(name="Walk", category="exercise", estimated_duration_minutes=30,
                                              priority=5, recurrence=spec))

    def expand(self, first: date, last: date):
        return series_ops.expand_occurrences(self.planning, first, last)

    def occurrence(self, series: Task, slot: date) -> Task | None:
        return self.planning.get_tasks_including_deleted([occurrence_task_id(series.id, slot)]).get(
            occurrence_task_id(series.id, slot))

    def schedule(self, first: date, last: date | None = None):
        return workflow.generate(self.planning, range_start=first, range_end=last or first, timezone_name="UTC",
                                 scope=RangeScope.PLANNED)

    def placement_of(self, task: Task) -> ScheduledTask:
        [placement] = self.planning.active_placements_for_tasks([task.id])[task.id]
        return placement

    def complete(self, task: Task) -> None:
        execution = self.executions.get_or_create_canonical_execution(task, self.placement_of(task))
        self.executions.start(execution.id)
        self.executions.complete(execution.id)


@pytest.fixture
def world(tmp_path: Path):
    session = World(tmp_path / "app.db")
    yield session
    session.connection.close()


def test_this_occurrence_marks_it_modified_and_series_wide_edits_leave_it_and_history_alone(world: World) -> None:
    walk = world.series()
    world.expand(day(0), day(4))
    world.schedule(day(0))
    first = world.occurrence(walk, day(0))
    world.complete(first)  # history
    edited = world.occurrence(walk, day(1))
    change = series_ops.edit_occurrence(world.planning, edited.model_copy(update={"name": "Long walk",
                                                                                   "estimated_duration_minutes": 60}),
                                        expected_version=edited.version)
    assert change.occurrence.occurrence_state == OccurrenceState.MODIFIED

    stored = world.planning.get_task(walk.id)
    change = series_ops.edit_series(world.planning, stored.model_copy(update={"name": "Evening walk", "priority": 8}),
                                    expected_version=stored.version, scope=EditScope.SERIES)
    assert change.series.name == "Evening walk" and change.successor is None  # same rule: in place
    assert {task.occurrence_slot for task in change.updated} == {day(2), day(3), day(4)}
    assert all(task.name == "Evening walk" and task.series_version == change.series.version and task.occurrence_state
               is None for task in change.updated)
    assert {(item.task.occurrence_slot, item.reason) for item in change.preserved} == {(day(0), "history"),
                                                                                       (day(1), "modified")}
    assert world.occurrence(walk, day(0)).name == "Walk" and world.occurrence(walk, day(1)).name == "Long walk"
    assert "kept as history" in change.explanation() and "individually edited" in change.explanation()


def test_an_ordinary_update_of_an_occurrence_marks_it_modified_and_its_identity_never_changes(world: World) -> None:
    walk = world.series()
    [occurrence] = world.expand(day(0), day(0)).created
    saved = world.planning.update_task(occurrence.model_copy(update={"priority": 9}), expected_version=1)
    assert saved.occurrence_state == OccurrenceState.MODIFIED
    with pytest.raises((InvalidEntityError, ValueError)):
        world.planning.update_task(saved.model_copy(update={"occurrence_slot": day(1)}), expected_version=saved.version)
    with pytest.raises(InvalidEntityError):
        world.planning.update_task(world.planning.get_task(walk.id).model_copy(update={"recurrence": None}),
                                   expected_version=walk.version)  # a series with occurrences stays a series


def test_this_and_later_splits_atomically_with_lineage_and_no_duplicate_slots(world: World) -> None:
    walk = world.series(count=10)
    world.expand(day(0), day(6))
    world.schedule(day(4))
    world.complete(world.occurrence(walk, day(4)))  # a later occurrence already done
    modified = world.occurrence(walk, day(5))
    series_ops.edit_occurrence(world.planning, modified.model_copy(update={"priority": 2}),
                               expected_version=modified.version)
    stored = world.planning.get_task(walk.id)

    change = series_ops.edit_series(world.planning, stored.model_copy(update={"name": "Run"}),
                                    expected_version=stored.version, scope=EditScope.FUTURE, cutoff=day(3))

    ended, successor = change.series, change.successor
    assert ended.recurrence.end_date == day(2) and ended.recurrence.count is None  # equivalent bound, no overlap
    assert successor.series_predecessor_id == walk.id and successor.recurrence.start_date == day(3)
    assert successor.recurrence.count == 7  # 10 counted from the original start; 3 slots before the cutoff
    assert {task.occurrence_slot for task in change.superseded} == {day(3), day(6)}
    assert all(task.occurrence_state == OccurrenceState.SUPERSEDED and task.deleted_at for task in change.superseded)
    assert {(item.task.occurrence_slot, item.reason) for item in change.preserved} == {(day(4), "history"),
                                                                                       (day(5), "modified")}
    assert world.occurrence(walk, day(1)).deleted_at is None  # earlier occurrences untouched

    expanded = world.expand(day(0), day(9))
    successor_slots = sorted(task.occurrence_slot for task in expanded.created if task.series_id == successor.id)
    assert successor_slots == [day(3), day(6), day(7), day(8), day(9)]  # not 4 and 5: preserved work covers them
    assert all(task.name == "Run" for task in expanded.created)
    live = [task for task in world.planning.list_tasks() if task.occurrence_slot is not None]
    assert len({task.occurrence_slot for task in live}) == len(live)  # one live occurrence per date


def test_an_entire_series_rule_or_time_zone_change_is_a_versioned_split_that_never_rekeys_history(world: World) -> None:
    walk = world.series()
    world.expand(day(0), day(2))
    world.schedule(day(0))
    done = world.occurrence(walk, day(0))
    world.complete(done)
    stored = world.planning.get_task(walk.id)
    moved_zone = stored.model_copy(update={"recurrence": stored.recurrence.model_copy(update={
        "timezone": "Europe/Berlin"})})
    change = series_ops.edit_series(world.planning, moved_zone, expected_version=stored.version, scope=EditScope.SERIES)
    assert change.series.recurrence.end_date == MON - timedelta(days=1)  # retired: it has no slot any more
    assert change.successor.recurrence.timezone == "Europe/Berlin"
    assert world.occurrence(walk, day(0)).id == done.id and world.occurrence(walk, day(0)).deleted_at is None
    assert {task.occurrence_slot for task in change.superseded} == {day(1), day(2)}
    created = world.expand(day(0), day(2)).created
    assert sorted(task.occurrence_slot for task in created) == [day(1), day(2)]  # day 0 is the completed one
    assert all(task.series_id == change.successor.id for task in created)


def test_deleting_later_or_all_occurrences_preserves_history_and_reports_it(world: World) -> None:
    walk = world.series()
    world.expand(day(0), day(5))
    world.schedule(day(3))
    world.complete(world.occurrence(walk, day(3)))
    stored = world.planning.get_task(walk.id)

    later = series_ops.delete_series(world.planning, walk.id, expected_version=stored.version, scope=EditScope.FUTURE,
                                     cutoff=day(2))
    assert later.series.recurrence.end_date == day(1) and later.series.deleted_at is None
    assert {task.occurrence_slot for task in later.superseded} == {day(2), day(4), day(5)}
    assert [(item.task.occurrence_slot, item.reason) for item in later.preserved] == [(day(3), "history")]
    assert world.expand(day(0), day(9)).created == []  # the series ends: nothing comes back

    everything = series_ops.delete_series(world.planning, walk.id, expected_version=later.series.version,
                                          scope=EditScope.SERIES)
    assert everything.series.deleted_at is not None
    assert {task.occurrence_slot for task in everything.superseded} == {day(0), day(1)}
    assert world.occurrence(walk, day(3)).deleted_at is None  # history stays
    execution_rows = world.connection.execute("SELECT COUNT(*) FROM executions WHERE deleted_at IS NULL").fetchone()[0]
    assert execution_rows == 1


def test_stale_scoped_changes_change_nothing(world: World) -> None:
    walk = world.series()
    world.expand(day(0), day(2))
    stored = world.planning.get_task(walk.id)
    world.planning.update_task(stored.model_copy(update={"priority": 7}), expected_version=stored.version)
    before = [tuple(row) for row in world.connection.execute("SELECT * FROM tasks ORDER BY id")]
    with pytest.raises(VersionConflictError):
        series_ops.edit_series(world.planning, stored.model_copy(update={"name": "X"}), expected_version=stored.version,
                               scope=EditScope.FUTURE, cutoff=day(1))
    with pytest.raises(VersionConflictError):
        series_ops.delete_series(world.planning, walk.id, expected_version=stored.version, scope=EditScope.SERIES)
    with pytest.raises(SeriesConfigurationError):
        series_ops.edit_occurrence(world.planning, stored, expected_version=world.planning.get_task(walk.id).version)
    assert [tuple(row) for row in world.connection.execute("SELECT * FROM tasks ORDER BY id")] == before


def test_moving_an_occurrence_to_another_date_keeps_its_identity_and_the_old_date_never_recreates_it(
    world: World,
) -> None:
    walk = world.series()
    world.expand(day(0), day(2))
    world.schedule(day(0))
    occurrence = world.occurrence(walk, day(0))
    placement = world.placement_of(occurrence)

    moved = workflow.reschedule_placement(
        world.planning, placement.id, expected_version=placement.version, planned_date=day(1), timezone_name="UTC",
        planned_start=at(18, day(1)), planned_end=at(18, day(1)) + timedelta(minutes=30))
    assert moved.replacement.task_id == occurrence.id and moved.replacement.planned_date == day(1)
    assert moved.updated_task.required_date == day(1) and moved.updated_task.occurrence_slot == day(0)
    assert moved.updated_task.occurrence_state == OccurrenceState.MODIFIED

    world.schedule(day(0))  # the date it left: nothing of it comes back
    assert not [p for p in world.planning.placements_for_date(day(0)) if p.task_id == occurrence.id]
    assert world.expand(day(0), day(0)).created == []
    world.schedule(day(1))  # its new date: it is planned there once, next to that date's own occurrence
    on_tuesday = [p for p in world.planning.placements_for_date(day(1)) if p.task_id == occurrence.id]
    assert len(on_tuesday) == 1
    own = world.occurrence(walk, day(1))
    assert len([p for p in world.planning.placements_for_date(day(1)) if p.task_id == own.id]) == 1


def test_a_moved_occurrence_cannot_take_a_second_placement(world: World) -> None:
    walk = world.series()
    world.expand(day(0), day(0))
    world.schedule(day(0))
    occurrence = world.occurrence(walk, day(0))
    first = world.placement_of(occurrence)
    # A second live placement of the same occurrence (e.g. written by an old import) makes a move ambiguous.
    stray = ScheduledTask(task_id=occurrence.id, planned_date=day(2), timezone="UTC", planned_start=at(9, day(2)),
                          planned_end=at(9, day(2)) + timedelta(minutes=30))
    world.planning.replace_placements(day(2), day(2), [stray])
    with pytest.raises(RescheduleRejectedError) as rejected:
        workflow.reschedule_placement(
            world.planning, first.id, expected_version=first.version, planned_date=day(1), timezone_name="UTC",
            planned_start=at(10, day(1)), planned_end=at(10, day(1)) + timedelta(minutes=30))
    assert "occurrence_taken" in [problem.reason for problem in rejected.value.problems]
    assert world.occurrence(walk, day(0)).required_date == day(0)  # the refused move changed nothing


def test_a_legacy_template_placement_still_moves_only_within_its_own_date(world: World) -> None:
    legacy = world.planning.create_task(Task(name="Standup", category="work", estimated_duration_minutes=15,
                                             priority=5, recurrence=RecurrenceSpec(frequency="daily")))
    placement = ScheduledTask(task_id=legacy.id, planned_date=day(0), timezone="UTC", planned_start=at(9, day(0)),
                              planned_end=at(9, day(0)) + timedelta(minutes=15))
    [stored] = world.planning.replace_placements(day(0), day(0), [placement]).placements
    with pytest.raises(RescheduleRejectedError) as rejected:
        workflow.reschedule_placement(
            world.planning, stored.id, expected_version=stored.version, planned_date=day(1), timezone_name="UTC",
            planned_start=at(9, day(1)), planned_end=at(9, day(1)) + timedelta(minutes=15))
    assert [problem.reason for problem in rejected.value.problems] == ["recurring_occurrence_date"]
    moved = workflow.reschedule_placement(
        world.planning, stored.id, expected_version=stored.version, planned_date=day(0), timezone_name="UTC",
        planned_start=at(11, day(0)), planned_end=at(11, day(0)) + timedelta(minutes=15))
    assert moved.replacement.planned_date == day(0) and moved.updated_task is None
