"""Recurrence expansion on the local store (app/planning/series.py, docs/recurrence.md): concrete occurrences
with derived identity and a reserved slot per (series, slot); series definitions are never scheduled;
repeated, overlapping, restarted and retried expansion are no-ops; budgets bound the work and leave nothing
half-written; legacy templates need configuration while their saved placements map to occurrences (ambiguous
collisions are reported, not discarded); recurring dependencies resolve the same slot (prerequisites are
materialized, mismatches and cycles refused, unresolved slots reported); DST days are reported; uniqueness
holds under concurrency; owners are isolated."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.planning import series as series_ops
from app.planning import workflow
from app.planning.application import PlanningService, RangeScope
from app.planning.errors import (
    EntityInUseError,
    InvalidEntityError,
    InvalidReferenceError,
    RecurrenceLimitError,
    ScopeError,
)
from app.planning.external_dependencies import ExternalDependencyState
from app.planning.models import LocalTimeWindow, OccurrenceState, RecurrenceSpec, ScheduledTask, Task
from app.planning.recurrence import occurrence_task_id
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope

MON = date(2026, 3, 2)
SUN = MON + timedelta(days=6)


def make_series(name: str = "Gym", frequency: str = "weekly", start: date = MON, tz: str = "UTC", **fields) -> Task:
    rule = {key: fields.pop(key) for key in ("interval", "weekdays", "day_of_month", "end_date", "count")
            if key in fields}
    return Task(name=name, category="exercise", estimated_duration_minutes=45, priority=6,
                recurrence=RecurrenceSpec(frequency=frequency, start_date=start, timezone=tz, **rule), **fields)


def open_service(path: Path) -> tuple[sqlite3.Connection, PlanningService]:
    connection = get_connection(path)
    return connection, PlanningService(PlanningRepository(connection))


@pytest.fixture
def service(tmp_path: Path):
    connection, planning = open_service(tmp_path / "app.db")
    yield planning
    connection.close()


def occurrences(service: PlanningService, series: Task, *, include_deleted: bool = True) -> list[Task]:
    return service.occurrences_of_series([series.id], include_deleted=include_deleted).get(series.id, [])


def test_expansion_materializes_concrete_occurrences_with_derived_identity(service: PlanningService) -> None:
    gym = service.create_task(make_series(weekdays=[0, 3], preferred_time_window=LocalTimeWindow(
        start_minute=17 * 60, end_minute=20 * 60)))
    result = series_ops.expand_occurrences(service, MON, MON + timedelta(days=13))

    assert [task.occurrence_slot for task in result.created] == [MON, MON + timedelta(days=3), MON + timedelta(days=7),
                                                                 MON + timedelta(days=10)]
    for occurrence in result.created:
        assert occurrence.id == occurrence_task_id(gym.id, occurrence.occurrence_slot)
        assert (occurrence.series_id, occurrence.series_version, occurrence.recurrence) == (gym.id, 1, None)
        assert occurrence.required_date == occurrence.occurrence_slot and occurrence.preferred_dates == []
        assert (occurrence.name, occurrence.estimated_duration_minutes, occurrence.preferred_time_window) == (
            "Gym", 45, gym.preferred_time_window)

    # The series definition itself is never scheduled; its occurrences are ordinary planned tasks.
    loaded = service.load_range(MON, SUN, scope=RangeScope.PLANNED)
    assert gym.id not in loaded.task_ids
    assert set(loaded.task_ids) == {task.id for task in result.created[:2]}
    eligible = service.load_range(MON, SUN, scope=RangeScope.ELIGIBLE)
    assert gym.id not in eligible.task_ids


def test_repeated_overlapping_restarted_and_retried_expansion_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    connection, service = open_service(path)
    gym = service.create_task(make_series(frequency="daily"))
    first = series_ops.expand_occurrences(service, MON, MON + timedelta(days=9))
    assert len(first.created) == 10
    rows_before = connection.execute("SELECT id, version, updated_at FROM tasks ORDER BY id").fetchall()
    dirty_before = connection.execute("SELECT * FROM sync_dirty ORDER BY 1, 2").fetchall()

    assert series_ops.expand_occurrences(service, MON, MON + timedelta(days=9)).created == []  # repeated
    overlap = series_ops.expand_occurrences(service, MON + timedelta(days=5), MON + timedelta(days=11))
    assert [task.occurrence_slot for task in overlap.created] == [MON + timedelta(days=10), MON + timedelta(days=11)]
    assert overlap.existing_count == 5
    connection.close()

    connection, service = open_service(path)  # a restart
    assert series_ops.expand_occurrences(service, MON, MON + timedelta(days=11)).created == []
    after = {row[0]: tuple(row) for row in connection.execute("SELECT id, version, updated_at FROM tasks")}
    assert all(after[row[0]] == tuple(row) for row in rows_before)  # nothing rewritten, no version bump
    assert len(occurrences(service, gym)) == 12
    dirty = connection.execute("SELECT * FROM sync_dirty ORDER BY 1, 2").fetchall()
    assert len(dirty) == len(dirty_before) + 2  # only the two new occurrences were captured
    connection.close()


def test_the_budgets_bound_the_work_and_a_refused_expansion_writes_nothing(service: PlanningService) -> None:
    for index in range(3):
        service.create_task(make_series(name=f"Daily {index}", frequency="daily"))
    with pytest.raises(RecurrenceLimitError):
        series_ops.expand_occurrences(service, MON, MON + timedelta(days=9), budget=25)  # 30 would be needed
    assert all(not group for group in service.occurrences_of_series(
        [task.id for task in service.list_series()]).values())
    with pytest.raises(ScopeError):
        series_ops.expand_occurrences(service, MON, MON + timedelta(days=series_ops.MAX_EXPANSION_DAYS))
    assert series_ops.MAX_EXPANSION_DAYS == workflow.MAX_RANGE_DAYS
    retried = series_ops.expand_occurrences(service, MON, MON + timedelta(days=9))  # the retry succeeds
    assert len(retried.created) == 30


def test_skipped_deleted_and_superseded_slots_are_never_minted_again(service: PlanningService) -> None:
    gym = service.create_task(make_series(frequency="daily"))
    created = series_ops.expand_occurrences(service, MON, MON + timedelta(days=2)).created
    series_ops.delete_occurrence(service, created[0].id, expected_version=1, skip=True)
    series_ops.delete_occurrence(service, created[1].id, expected_version=1)
    again = series_ops.expand_occurrences(service, MON, MON + timedelta(days=2))
    assert again.created == [] and again.existing_count == 3
    states = {task.occurrence_slot: (task.deleted_at is not None, task.occurrence_state)
              for task in occurrences(service, gym)}
    assert states == {MON: (True, OccurrenceState.SKIPPED), MON + timedelta(days=1): (True, OccurrenceState.DELETED),
                      MON + timedelta(days=2): (False, None)}
    assert {task.id for task in service.load_range(MON, MON + timedelta(days=2)).tasks.tasks.values()} == {created[2].id}


def test_the_database_keeps_one_record_per_series_and_slot(service: PlanningService, tmp_path: Path) -> None:
    gym = service.create_task(make_series(frequency="daily"))
    [occurrence] = series_ops.expand_occurrences(service, MON, MON).created
    raw = sqlite3.connect(str(tmp_path / "app.db"))
    with pytest.raises(sqlite3.IntegrityError):  # even a writer that bypasses the derived id
        raw.execute("INSERT INTO tasks (id, name, category, estimated_duration_minutes, priority, required, created_at, "
                    "updated_at, version, series_id, occurrence_slot) VALUES (?, 'Dup', 'x', 5, 5, 0, 'n', 'n', 1, ?, ?)",
                    (str(uuid.uuid4()), str(gym.id), MON.isoformat()))
    raw.close()
    assert [task.id for task in occurrences(service, gym)] == [occurrence.id]


def test_two_connections_expanding_the_same_slots_concurrently_converge(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    setup, service = open_service(path)
    gym = service.create_task(make_series(frequency="daily"))
    setup.close()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def expand() -> None:
        connection, planning = open_service(path)
        try:
            barrier.wait()
            for _ in range(5):
                series_ops.expand_occurrences(planning, MON, MON + timedelta(days=20))
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=expand) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    check, service = open_service(path)
    slots = [task.occurrence_slot for task in occurrences(service, gym)]
    assert slots == [MON + timedelta(days=offset) for offset in range(21)]  # once each
    check.close()


def test_legacy_templates_need_configuration_and_their_placements_map_to_occurrences(service: PlanningService) -> None:
    legacy = service.create_task(Task(name="Standup", category="work", estimated_duration_minutes=15, priority=5,
                                      recurrence=RecurrenceSpec(frequency="weekly", weekdays=[0])))
    other = service.create_task(Task(name="Other", category="work", estimated_duration_minutes=15, priority=5))
    assert legacy.needs_configuration

    def legacy_placement(day: date, hour: int) -> ScheduledTask:
        start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
        return ScheduledTask(task_id=legacy.id, planned_date=day, timezone="UTC", planned_start=start,
                             planned_end=start + timedelta(minutes=15))

    tue = MON + timedelta(days=1)
    service.replace_placements(MON, tue, [legacy_placement(MON, 9), legacy_placement(tue, 9),
                                          legacy_placement(tue, 11)])
    result = series_ops.expand_occurrences(service, MON, SUN)
    assert result.needs_configuration == [legacy.id]
    assert [task.occurrence_slot for task in result.created] == [MON]  # the unambiguous legacy slot is mapped
    [collision] = result.legacy_collisions
    assert (collision.series_id, collision.slot, len(collision.placement_ids)) == (legacy.id, tue, 2)
    assert any(problem.code == "legacy_collision" for problem in result.problems)
    assert len(service.placements_for_date(tue)) == 2  # reported for repair, nothing discarded
    assert other.id not in {task.series_id for task in result.created}

    # Generating Monday places the mapped occurrence; the legacy placement is superseded by identity, not duplicated.
    workflow.generate(service, range_start=MON, range_end=MON, timezone_name="UTC", scope=RangeScope.PLANNED)
    [placed] = [p for p in service.placements_for_date(MON) if p.task_id in {t.id for t in result.created}]
    legacy_tombstones = service.placements_superseded_by([placed.id])[placed.id]
    assert [tombstone.task_id for tombstone in legacy_tombstones] == [legacy.id]
    assert not [p for p in service.placements_for_date(MON) if p.task_id == legacy.id]


def test_recurring_dependencies_resolve_the_same_slot_and_materialize_prerequisites(service: PlanningService) -> None:
    warm = service.create_task(make_series(name="Warm up", frequency="daily", start=MON - timedelta(days=7)))
    gym = service.create_task(make_series(weekdays=[0, 2], dependency_ids=[warm.id]))
    result = series_ops.expand_occurrences(service, MON, MON + timedelta(days=2), series_ids=[gym.id])
    by_slot = {(task.series_id, task.occurrence_slot): task for task in result.created}
    for day in (MON, MON + timedelta(days=2)):
        assert by_slot[(gym.id, day)].dependency_ids == [occurrence_task_id(warm.id, day)]
        assert (warm.id, day) in by_slot  # the prerequisite of that slot was materialized with it
    assert (warm.id, MON + timedelta(days=1)) not in by_slot  # only what the requested series needed

    # The occurrences plan as concrete tasks: same date, prerequisite first.
    outcome = workflow.generate(service, range_start=MON, range_end=MON, timezone_name="UTC")
    placed = {p.task_id: p for p in outcome.outputs[MON].placements}
    assert placed[occurrence_task_id(warm.id, MON)].planned_end <= placed[occurrence_task_id(gym.id, MON)].planned_start


def test_unsupported_recurring_dependencies_are_refused_with_useful_errors(service: PlanningService) -> None:
    daily_berlin = service.create_task(make_series(name="Berlin", frequency="daily", tz="Europe/Berlin"))
    monthly = service.create_task(make_series(name="Monthly", frequency="monthly"))
    with pytest.raises(InvalidReferenceError, match="time zone"):
        service.create_task(make_series(name="Gym", dependency_ids=[daily_berlin.id]))
    with pytest.raises(InvalidReferenceError, match="different cadences"):
        service.create_task(make_series(name="Gym", dependency_ids=[monthly.id]))
    with pytest.raises(InvalidReferenceError, match="concrete occurrence"):
        service.create_task(Task(name="One-off", category="work", estimated_duration_minutes=30, priority=5,
                                 dependency_ids=[monthly.id]))
    a = service.create_task(make_series(name="A", frequency="daily"))
    b = service.create_task(make_series(name="B", frequency="daily", dependency_ids=[a.id]))
    with pytest.raises(InvalidEntityError, match="cycle"):
        service.update_task(a.model_copy(update={"dependency_ids": [b.id]}), expected_version=a.version)

    # A one-off task depends on one concrete occurrence instead -- that is allowed.
    [first] = series_ops.expand_occurrences(service, MON, MON, series_ids=[monthly.id]).created
    report = service.create_task(Task(name="Report", category="work", estimated_duration_minutes=30, priority=5,
                                      dependency_ids=[first.id]))
    assert report.dependency_ids == [first.id]


def test_a_prerequisite_without_that_slot_leaves_the_slot_unmaterialized_and_reported(service: PlanningService) -> None:
    warm = service.create_task(make_series(name="Warm up", frequency="daily", end_date=MON + timedelta(days=1)))
    gym = service.create_task(make_series(frequency="daily", dependency_ids=[warm.id]))
    result = series_ops.expand_occurrences(service, MON, MON + timedelta(days=3), series_ids=[gym.id])
    assert sorted(task.occurrence_slot for task in result.created if task.series_id == gym.id) == [
        MON, MON + timedelta(days=1)]
    unresolved = [problem for problem in result.problems if problem.code == "dependency_unresolved"]
    assert [problem.slot for problem in unresolved] == [MON + timedelta(days=2), MON + timedelta(days=3)]
    assert "has no occurrence" in unresolved[0].message


def test_skipping_a_prerequisite_occurrence_blocks_its_dependent_explicitly(service: PlanningService) -> None:
    warm = service.create_task(make_series(name="Warm up", frequency="daily"))
    gym = service.create_task(make_series(frequency="daily", dependency_ids=[warm.id]))
    series_ops.expand_occurrences(service, MON, MON)
    prerequisite = service.get_task(occurrence_task_id(warm.id, MON))
    series_ops.delete_occurrence(service, prerequisite.id, expected_version=prerequisite.version, skip=True)  # allowed

    dependent = service.get_task(occurrence_task_id(gym.id, MON))
    assert dependent.dependency_ids == [prerequisite.id]
    external = service.external_dependencies([dependent], MON, MON, "UTC")
    assert external[prerequisite.id].state == ExternalDependencyState.MISSING
    allocation = workflow.preview_allocation(service, MON, MON, scope=RangeScope.PLANNED, timezone_name="UTC").allocation
    assert dependent.id in {entry.task_id for entry in allocation.unallocated}
    # An ordinary dependency still blocks deleting its target.
    errand = service.create_task(Task(name="Errand", category="work", estimated_duration_minutes=30, priority=5))
    service.create_task(Task(name="After errand", category="work", estimated_duration_minutes=30, priority=5,
                             dependency_ids=[errand.id]))
    with pytest.raises(EntityInUseError):
        service.delete_task(errand.id, expected_version=errand.version)


def test_a_legacy_dependency_on_a_series_definition_reports_it_instead_of_scheduling_it(
    service: PlanningService,
) -> None:
    target = service.create_task(Task(name="Template", category="work", estimated_duration_minutes=30, priority=5))
    stored = service.create_task(Task(name="Dependent", category="work", estimated_duration_minutes=30, priority=5,
                                      required_date=MON, dependency_ids=[target.id]))
    # The edge predates the target becoming a (legacy) recurring template: it is kept, and explained when it blocks.
    service.update_task(target.model_copy(update={"recurrence": RecurrenceSpec(frequency="daily")}),
                        expected_version=target.version)
    external = service.external_dependencies([service.get_task(stored.id)], MON, MON, "UTC")
    assert external[target.id].state == ExternalDependencyState.SERIES
    assert "occurrences" in external[target.id].explanation()


def test_daylight_saving_days_and_impossible_local_times_are_reported_never_shifted(service: PlanningService) -> None:
    service.create_task(make_series(
        name="Night", frequency="daily", start=date(2026, 3, 7), tz="America/New_York",
        preferred_time_window=LocalTimeWindow(start_minute=2 * 60 + 30, end_minute=4 * 60)))
    result = series_ops.expand_occurrences(service, date(2026, 3, 7), date(2026, 3, 9))
    assert len(result.created) == 3  # every local date has its occurrence
    codes = {(warning.slot, warning.code) for warning in result.warnings}
    assert (date(2026, 3, 8), "offset_transition") in codes
    assert (date(2026, 3, 8), "ambiguous_local_time") in codes  # 02:30 does not exist that night
    assert not any(slot != date(2026, 3, 8) for slot, _ in codes)
    for task in result.created:
        assert task.preferred_time_window.start_minute == 150  # stored as asked, never moved


def test_owners_see_and_expand_only_their_own_series(service: PlanningService) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    as_alice = service.scoped(OwnerScope.account(alice))
    as_bob = service.scoped(OwnerScope.account(bob))
    gym = as_alice.create_task(make_series(user_id=alice, frequency="daily"))
    as_bob.create_task(make_series(name="Bob's", user_id=bob, frequency="daily"))
    created = series_ops.expand_occurrences(as_alice, MON, MON + timedelta(days=1)).created
    assert {task.user_id for task in created} == {alice} and {task.series_id for task in created} == {gym.id}
    assert as_bob.get_tasks_including_deleted([created[0].id]) == {}
    stray = series_ops.occurrence_for(gym, MON + timedelta(days=5), [], datetime.now(timezone.utc))
    with pytest.raises((InvalidReferenceError, InvalidEntityError, ScopeError)):
        as_bob.materialize_occurrences([stray.model_copy(update={"user_id": bob})])
