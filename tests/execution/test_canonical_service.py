"""Tests for app/execution/service.py's canonical creation API
(create_canonical_execution / get_or_create_canonical_execution), the new
`cancel` transition, and canonical (exact-instant) start-delay computation --
added by Task 2's identity migration.

All timing is driven by an explicit FakeClock so tests never sleep and never
depend on wall-clock time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.execution.errors import InvalidTransitionError
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.models import LocalTimeWindow, ScheduledTask, Task
from app.planning.repository import PlanningRepository


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def __call__(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc))  # a Monday


@pytest.fixture
def service_with_clock(repository: ExecutionRepository, clock: FakeClock) -> ExecutionService:
    return ExecutionService(repository, clock=clock)


@pytest.fixture
def make_task(planning_repository: PlanningRepository):
    """Build a canonical Task and persist it: since schema v3 a new execution
    may only link to a persisted task (see app/execution/db.py)."""

    def _make(**overrides) -> Task:
        defaults = dict(
            name="Study Math",
            category="study",
            estimated_duration_minutes=60,
            priority=8,
            preferred_time_window=LocalTimeWindow(start_minute=540, end_minute=660),
        )
        defaults.update(overrides)
        task = Task(**defaults)
        planning_repository.upsert_task(task)
        return task

    return _make


@pytest.fixture
def make_placement(planning_repository: PlanningRepository):
    """Build a canonical placement for a persisted task and persist it too."""

    def _make(task: Task, *, start: datetime, end: datetime, tz: str = "UTC", **overrides) -> ScheduledTask:
        defaults = dict(
            task_id=task.id,
            planned_date=start.date(),
            timezone=tz,
            planned_start=start,
            planned_end=end,
            score=10.0,
        )
        defaults.update(overrides)
        placement = ScheduledTask(**defaults)
        planning_repository.upsert_placement(placement)
        return placement

    return _make


# ----------------------------------------------------------------------
# create_canonical_execution
# ----------------------------------------------------------------------


def test_create_canonical_execution_snapshots_task_fields(
    service_with_clock: ExecutionService, make_task, make_placement
) -> None:
    task = make_task(name="Study Math", category="study", tags=["math"], priority=9)
    placement = make_placement(
        task, start=datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc), end=datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    )

    execution = service_with_clock.create_canonical_execution(task, placement)

    assert execution.task_id == task.id
    assert execution.scheduled_task_id == placement.id
    assert execution.task_name == "Study Math"
    assert execution.category == "study"
    assert execution.tag == "math"
    assert execution.priority == 9
    assert execution.planned_duration == 60
    assert execution.status == ExecutionStatus.SCHEDULED
    assert execution.canonical_planned_start == datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)
    assert execution.canonical_timezone == "UTC"
    # Legacy abstract-day fields are left unset for a canonical execution.
    assert execution.planned_date is None
    assert execution.planned_start is None


def test_create_canonical_execution_without_placement_is_task_only(service_with_clock: ExecutionService, make_task) -> None:
    task = make_task()

    execution = service_with_clock.create_canonical_execution(task)

    assert execution.task_id == task.id
    assert execution.scheduled_task_id is None
    assert execution.canonical_planned_start is None
    assert execution.planned_duration == task.estimated_duration_minutes


def test_create_canonical_execution_never_deduplicates_task_only_attempts(
    service_with_clock: ExecutionService,
    make_task,
) -> None:
    """Task-only executions (no placement) are never deduplicated by task_id alone."""
    task = make_task()

    first = service_with_clock.create_canonical_execution(task)
    second = service_with_clock.create_canonical_execution(task)

    assert first.id != second.id


def test_create_canonical_execution_for_distinct_placements_creates_distinct_rows(
    service_with_clock: ExecutionService,
    make_task,
    make_placement,
) -> None:
    task = make_task()
    placement_a = make_placement(
        task, start=datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc), end=datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    )
    placement_b = make_placement(
        task, start=datetime(2024, 6, 4, 9, 0, tzinfo=timezone.utc), end=datetime(2024, 6, 4, 10, 0, tzinfo=timezone.utc)
    )

    first = service_with_clock.create_canonical_execution(task, placement_a)
    second = service_with_clock.create_canonical_execution(task, placement_b)

    assert first.id != second.id


def test_create_canonical_execution_twice_for_the_same_placement_raises(
    service_with_clock: ExecutionService,
    make_task,
    make_placement,
) -> None:
    """create_canonical_execution always inserts, and the database's own
    uniqueness policy for non-null scheduled_task_id (see
    app/execution/db.py's partial unique index) rejects a second row for the
    same placement -- callers that want "reuse the existing execution for
    this placement" semantics must use get_or_create_canonical_execution
    instead, which handles this atomically."""
    import sqlite3

    task = make_task()
    placement = make_placement(
        task, start=datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc), end=datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    )

    service_with_clock.create_canonical_execution(task, placement)
    with pytest.raises(sqlite3.IntegrityError):
        service_with_clock.create_canonical_execution(task, placement)


# ----------------------------------------------------------------------
# get_or_create_canonical_execution (identity-based deduplication)
# ----------------------------------------------------------------------


def test_get_or_create_canonical_reuses_execution_for_same_placement(
    service_with_clock: ExecutionService, make_task, make_placement
) -> None:
    task = make_task()
    placement = make_placement(
        task, start=datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc), end=datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    )

    first = service_with_clock.get_or_create_canonical_execution(task, placement)
    second = service_with_clock.get_or_create_canonical_execution(task, placement)

    assert first.id == second.id


def test_get_or_create_canonical_reuses_regardless_of_status(
    service_with_clock: ExecutionService, make_task, make_placement
) -> None:
    task = make_task()
    placement = make_placement(
        task, start=datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc), end=datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    )

    created = service_with_clock.get_or_create_canonical_execution(task, placement)
    service_with_clock.start(created.id)

    found = service_with_clock.get_or_create_canonical_execution(task, placement)

    assert found.id == created.id
    assert found.status == ExecutionStatus.IN_PROGRESS


def test_get_or_create_canonical_distinct_task_ids_stay_distinct_even_with_identical_labels_and_times(
    service_with_clock: ExecutionService,
    make_task,
    make_placement,
) -> None:
    """Two different tasks that happen to share the same name and the same
    scheduled interval must remain distinct executions."""
    task_a = make_task(name="Study Session")
    task_b = make_task(name="Study Session")
    start = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)
    end = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)

    placement_a = make_placement(task_a, start=start, end=end)
    placement_b = make_placement(task_b, start=start, end=end)

    execution_a = service_with_clock.get_or_create_canonical_execution(task_a, placement_a)
    execution_b = service_with_clock.get_or_create_canonical_execution(task_b, placement_b)

    assert execution_a.id != execution_b.id
    assert execution_a.task_id != execution_b.task_id


def test_get_or_create_canonical_moved_placement_does_not_overwrite_original_snapshot(
    service_with_clock: ExecutionService,
    make_task,
    make_placement,
) -> None:
    """A task re-placed at a new time (a new ScheduledTask.id) gets a new
    execution; the original execution's planned snapshot is untouched."""
    task = make_task(name="Study Math")
    original_placement = make_placement(
        task, start=datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc), end=datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    )
    original_execution = service_with_clock.get_or_create_canonical_execution(task, original_placement)

    moved_placement = make_placement(
        task, start=datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc), end=datetime(2024, 6, 3, 15, 0, tzinfo=timezone.utc)
    )
    moved_execution = service_with_clock.get_or_create_canonical_execution(task, moved_placement)

    assert moved_execution.id != original_execution.id
    reloaded_original = service_with_clock.get_execution(original_execution.id)
    assert reloaded_original.canonical_planned_start == datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)


def test_get_or_create_canonical_requires_a_placement(service_with_clock: ExecutionService) -> None:
    """get_or_create_canonical_execution's signature requires a ScheduledTask;
    a task-only attempt must go through create_canonical_execution instead."""
    import inspect

    signature = inspect.signature(service_with_clock.get_or_create_canonical_execution)
    assert signature.parameters["scheduled_task"].default is inspect._empty


# ----------------------------------------------------------------------
# cancel
# ----------------------------------------------------------------------


@pytest.mark.parametrize("source_status_setup", ["scheduled", "in_progress", "paused"])
def test_cancel_from_each_valid_source(service_with_clock: ExecutionService, source_status_setup: str, make_task) -> None:
    task = make_task()
    execution = service_with_clock.create_canonical_execution(task)

    if source_status_setup in ("in_progress", "paused"):
        service_with_clock.start(execution.id)
    if source_status_setup == "paused":
        service_with_clock.pause(execution.id)

    cancelled = service_with_clock.cancel(execution.id)

    assert cancelled.status == ExecutionStatus.CANCELLED
    assert cancelled.actual_active_duration_minutes is None
    assert cancelled.actual_final_end_at is not None


def test_cancel_closes_open_session(service_with_clock: ExecutionService, repository: ExecutionRepository, make_task) -> None:
    task = make_task()
    execution = service_with_clock.create_canonical_execution(task)
    service_with_clock.start(execution.id)

    service_with_clock.cancel(execution.id)

    sessions = repository.list_sessions(execution.id)
    assert len(sessions) == 1
    assert sessions[0].ended_at is not None


@pytest.mark.parametrize("action", ["start", "pause", "resume", "complete", "skip", "cancel"])
def test_no_transition_out_of_cancelled(service_with_clock: ExecutionService, action: str, make_task) -> None:
    task = make_task()
    execution = service_with_clock.create_canonical_execution(task)
    service_with_clock.cancel(execution.id)

    with pytest.raises(InvalidTransitionError):
        getattr(service_with_clock, action)(execution.id)


def test_cancel_from_completed_is_invalid(service_with_clock: ExecutionService, make_task) -> None:
    task = make_task()
    execution = service_with_clock.create_canonical_execution(task)
    service_with_clock.start(execution.id)
    service_with_clock.complete(execution.id)

    with pytest.raises(InvalidTransitionError):
        service_with_clock.cancel(execution.id)


def test_skip_records_actual_final_end_at(service_with_clock: ExecutionService, make_task) -> None:
    task = make_task()
    execution = service_with_clock.create_canonical_execution(task)

    skipped = service_with_clock.skip(execution.id)

    assert skipped.actual_final_end_at is not None


# ----------------------------------------------------------------------
# Canonical (exact-instant) start delay, including midnight crossing
# ----------------------------------------------------------------------


def test_canonical_start_delay_is_exact_instant_difference(
    service_with_clock: ExecutionService, clock: FakeClock
, make_task, make_placement) -> None:
    task = make_task()
    planned_start = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)
    placement = make_placement(task, start=planned_start, end=planned_start + timedelta(hours=1))
    execution = service_with_clock.create_canonical_execution(task, placement)

    clock.advance(timedelta(minutes=12))  # clock starts at planned_start already; advance to simulate lateness
    service_with_clock.start(execution.id)
    clock.advance(timedelta(minutes=30))
    completed = service_with_clock.complete(execution.id)

    assert completed.start_delay_minutes == pytest.approx(12.0)


def test_canonical_start_delay_handles_midnight_crossing_correctly(
    service_with_clock: ExecutionService, clock: FakeClock
, make_task, make_placement) -> None:
    """planned_start just before midnight UTC, actual start just after --
    the exact-instant subtraction must not wrap around like a naive
    time-of-day comparison would."""
    task = make_task()
    planned_start = datetime(2024, 6, 3, 23, 50, tzinfo=timezone.utc)
    placement = make_placement(task, start=planned_start, end=planned_start + timedelta(hours=1))
    execution = service_with_clock.create_canonical_execution(task, placement)

    clock.advance(timedelta(hours=15))  # move clock to 2024-06-04 00:00 UTC (planned_start + 10 min)
    service_with_clock.start(execution.id)
    clock.advance(timedelta(minutes=20))
    completed = service_with_clock.complete(execution.id)

    # Actual start is 2024-06-04T00:00:00Z; planned_start is 2024-06-03T23:50:00Z.
    # A correct instant-based delay is +10 minutes, not a large negative
    # number from naively comparing minute-of-day (0 - 1430).
    assert completed.start_delay_minutes == pytest.approx(10.0)


def test_canonical_start_delay_negative_for_early_start(
    service_with_clock: ExecutionService, clock: FakeClock
, make_task, make_placement) -> None:
    task = make_task()
    planned_start = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)
    placement = make_placement(task, start=planned_start, end=planned_start + timedelta(hours=1))
    execution = service_with_clock.create_canonical_execution(task, placement)

    clock.advance(timedelta(minutes=-15))
    service_with_clock.start(execution.id)
    clock.advance(timedelta(minutes=30))
    completed = service_with_clock.complete(execution.id)

    assert completed.start_delay_minutes == pytest.approx(-15.0)


def test_actual_first_start_at_is_not_reset_by_resume(service_with_clock: ExecutionService, clock: FakeClock, make_task) -> None:
    task = make_task()
    execution = service_with_clock.create_canonical_execution(task)

    started = service_with_clock.start(execution.id)
    first_start = started.actual_first_start_at
    clock.advance(timedelta(minutes=10))
    service_with_clock.pause(execution.id)
    clock.advance(timedelta(minutes=5))
    resumed = service_with_clock.resume(execution.id)

    assert resumed.actual_first_start_at == first_start


def test_task_only_execution_has_no_start_delay_when_no_planned_start_exists(
    service_with_clock: ExecutionService, clock: FakeClock
, make_task) -> None:
    """A canonical, task-only execution (no placement) has neither a
    canonical_planned_start nor a legacy planned_start, so start_delay_minutes
    stays None rather than being computed against a fabricated "midnight"
    planned time."""
    task = make_task()
    execution = service_with_clock.create_canonical_execution(task)

    service_with_clock.start(execution.id)
    clock.advance(timedelta(minutes=10))
    completed = service_with_clock.complete(execution.id)

    assert completed.start_delay_minutes is None


# ----------------------------------------------------------------------
# Mixed legacy + canonical listing
# ----------------------------------------------------------------------


def test_list_executions_returns_both_legacy_and_canonical_rows(service_with_clock: ExecutionService, make_task) -> None:
    legacy = service_with_clock.create_execution(
        task_name="Legacy Task", category="study", tag="math",
        planned_date=1, planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )
    task = make_task(name="Canonical Task")
    canonical = service_with_clock.create_canonical_execution(task)

    all_ids = {execution.id for execution in service_with_clock.list_executions()}
    assert {legacy.id, canonical.id} <= all_ids
