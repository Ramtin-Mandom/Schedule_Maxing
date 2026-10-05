"""Task types and placement planning snapshots (docs/productivity-redesign-plan.md, contract A) through the
shared PlanningService on the local SQLite repository: type identity and ownership, recurring-series
inheritance, and snapshots that are captured when a placement is saved and never rewritten -- by edits of the
task or its type, a regeneration, a move, or a restart."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.execution.db import get_connection
from app.planning import workflow
from app.planning.application import PlanningService
from app.planning.errors import InvalidReferenceError
from app.planning.history import SOURCE_CURRENT_TASK, SOURCE_PLACEMENT, SOURCE_UNKNOWN, historical_plan
from app.planning.models import (
    PlacementRemovalReason,
    RecurrenceSpec,
    ScheduledTask,
    Task,
    TaskType,
    derived_task_type_id,
)
from app.planning.recurrence import occurrence_task_id
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope

MON, TUE = date(2026, 3, 2), date(2026, 3, 3)
ALICE, BOB = uuid.uuid4(), uuid.uuid4()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 3, 1, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "app.db"


@pytest.fixture
def connection(db_path):
    conn = get_connection(db_path)
    yield conn
    conn.close()


@pytest.fixture
def service(connection) -> PlanningService:
    return PlanningService(PlanningRepository(connection), Clock())


def at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def task(name: str = "Reading", **fields) -> Task:
    return Task(**{"name": name, "category": "study", "tags": ["deep", "book"], "estimated_duration_minutes": 45,
                   "priority": 5, "points": 3, **fields})


def place(service: PlanningService, item: Task, day: date = MON, hour: int = 9) -> ScheduledTask:
    placement = ScheduledTask(task_id=item.id, planned_date=day, timezone="UTC", planned_start=at(day, hour),
                              planned_end=at(day, hour) + timedelta(minutes=45))
    existing = service.placements_for_date(day)
    service.replace_placements(day, day, [*existing, placement],
                               expected_versions={p.id: p.version for p in existing})
    return service.get_placement(placement.id)


def test_new_tasks_get_distinct_types_and_never_group_by_name(service) -> None:
    first, second = service.create_task(task()), service.create_task(task())
    assert first.task_type_id == derived_task_type_id(first.id) != second.task_type_id
    assert {t.id: t.label for t in service.list_task_types()} == {first.task_type_id: "Reading",
                                                                  second.task_type_id: "Reading"}

    # Reusing an existing type is explicit; category and tags stay independent of it.
    third = service.create_task(task("Reading (evening)", category="leisure", tags=[], task_type_id=first.task_type_id))
    assert third.task_type_id == first.task_type_id and len(service.list_task_types()) == 2

    # An edit that does not mention the type keeps it; an unknown type is refused and nothing is written.
    renamed = service.update_task(first.model_copy(update={"name": "Reading!", "task_type_id": None}),
                                  expected_version=first.version)
    assert renamed.task_type_id == first.task_type_id
    with pytest.raises(InvalidReferenceError):
        service.create_task(task("Stray", task_type_id=uuid.uuid4()))
    assert [t.name for t in service.list_tasks()] == ["Reading!", "Reading", "Reading (evening)"]


def test_a_task_never_references_another_accounts_type(connection) -> None:
    repository = PlanningRepository(connection)
    alice = PlanningService(repository.scoped(OwnerScope.account(ALICE)), Clock())
    bob = PlanningService(repository.scoped(OwnerScope.account(BOB)), Clock())
    shared = alice.create_task_type(TaskType(label="Deep work"))
    assert shared.user_id == ALICE and bob.list_task_types() == []

    with pytest.raises(InvalidReferenceError):
        bob.create_task(task(user_id=BOB, task_type_id=shared.id))
    assert bob.list_tasks() == []
    mine = alice.create_task(task(user_id=ALICE, task_type_id=shared.id))
    assert mine.task_type_id == shared.id and alice.get_task_types([shared.id])[shared.id].label == "Deep work"


def test_occurrences_and_continued_segments_share_the_series_type_but_stay_distinct(service) -> None:
    rule = RecurrenceSpec(frequency="daily", start_date=MON, timezone="UTC")
    series = service.create_task(task("Standup", recurrence=rule))
    occurrences = service.materialize_occurrences([
        task("Standup", id=occurrence_task_id(series.id, day), series_id=series.id, occurrence_slot=day,
             required_date=day, series_version=series.version, task_type_id=uuid.uuid4())  # a stray type is overruled
        for day in (MON, TUE)
    ])
    segment = service.create_task(task("Standup (later)", series_predecessor_id=series.id,
                                       recurrence=rule.model_copy(update={"start_date": TUE})))

    assert {item.task_type_id for item in (*occurrences, segment)} == {series.task_type_id}
    assert series.task_type_id == derived_task_type_id(series.id)
    assert len({item.id for item in occurrences}) == 2  # one type, two occurrences
    assert [t.label for t in service.list_task_types()] == ["Standup"]


def test_a_saved_placement_keeps_its_snapshot_through_edits_regeneration_and_restart(service, db_path) -> None:
    item = service.create_task(task())
    placed = place(service, item)
    expected = {"task_name": "Reading", "task_category": "study", "task_tags": ["deep", "book"], "task_points": 3,
                "task_estimate_minutes": 45, "task_type_id": item.task_type_id, "task_type_label": "Reading"}
    assert {name: getattr(placed, name) for name in expected} == expected

    service.update_task(item.model_copy(update={"name": "Skimming", "category": "leisure", "tags": [], "points": 0,
                                                "estimated_duration_minutes": 10}), expected_version=item.version)
    label = service.list_task_types()[0]
    service.update_task_type(label.model_copy(update={"label": "Books"}), expected_version=label.version)
    # A regeneration that keeps the placement (same id) keeps its original snapshot, whatever it is handed.
    service.replace_placements(MON, MON, [placed.model_copy(update={"task_name": None, "task_points": None})],
                               expected_versions={placed.id: placed.version})

    reopened = get_connection(db_path)
    try:
        stored = PlanningService(PlanningRepository(reopened)).get_placement(placed.id)
        assert {name: getattr(stored, name) for name in expected} == expected and stored.version == placed.version
        # Never-started work has history: the snapshot answers without an execution or the current task.
        plan = historical_plan(stored)
        assert (plan.name, plan.category, plan.tags, plan.points, plan.estimate_minutes, plan.type_label) == (
            "Reading", "study", ("deep", "book"), 3, 45, "Reading")
        assert set(plan.sources.values()) == {SOURCE_PLACEMENT}
    finally:
        reopened.close()


def test_a_move_keeps_the_original_snapshot_and_the_replacement_takes_its_own(service) -> None:
    item = service.create_task(task())
    original = place(service, item)
    edited = service.update_task(item.model_copy(update={"name": "Skimming", "points": 7}), expected_version=item.version)

    moved = workflow.reschedule_placement(
        service, original.id, expected_version=original.version, planned_date=MON, timezone_name="UTC",
        planned_start=at(MON, 14), planned_end=at(MON, 14) + timedelta(minutes=45))

    assert (moved.previous.task_name, moved.previous.task_points) == ("Reading", 3)
    assert moved.previous.removal_reason == PlacementRemovalReason.RESCHEDULED
    assert moved.previous.superseded_by_id == moved.replacement.id
    assert (moved.replacement.task_name, moved.replacement.task_points) == ("Skimming", 7)
    assert moved.replacement.task_type_id == edited.task_type_id == original.task_type_id
    live = service.active_placements_for_tasks([item.id])[item.id]
    assert [placement.id for placement in live] == [moved.replacement.id]  # one logical occurrence, not two


def test_history_from_before_snapshots_stays_unknown(service, connection) -> None:
    item = service.create_task(task())
    placed = place(service, item)
    connection.execute(
        "UPDATE scheduled_tasks SET task_name = NULL, task_tags = NULL, task_points = NULL, "
        "task_estimate_minutes = NULL, task_type_id = NULL, task_type_label = NULL, task_category = NULL")
    legacy = service.get_placement(placed.id)

    plan = historical_plan(legacy, task=service.get_task(item.id))
    assert (plan.name, plan.category, plan.tags, plan.points, plan.estimate_minutes, plan.type_label) == (None,) * 6
    assert plan.type_id == item.task_type_id and plan.sources["type_id"] == SOURCE_CURRENT_TASK
    assert {plan.sources[name] for name in ("name", "category", "tags", "points")} == {SOURCE_UNKNOWN}
    # Keeping it in a regeneration does not invent the missing snapshot.
    service.replace_placements(MON, MON, [legacy], expected_versions={legacy.id: legacy.version})
    assert service.get_placement(placed.id).task_name is None
