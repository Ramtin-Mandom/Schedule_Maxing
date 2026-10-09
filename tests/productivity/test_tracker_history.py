"""The tracker over real planning and execution history (app/productivity/tracker.py; SQLite services):
statuses and both completion formulas, local-date attribution across a DST change, moves and regeneration
counted once, recurring occurrences under one type, renamed and deleted tasks, exact medians across reading
windows, the lineage-limit diagnostic, and owner isolation."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning import workflow
from app.planning.application import PlanningService
from app.planning.history import MAX_LINEAGE_DEPTH, collect_schedule_history
from app.planning.models import PlacementRemovalReason, RecurrenceSpec, ScheduledTask, Task
from app.planning.recurrence import occurrence_task_id
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope
from app.productivity.day_summary import DayStatusClass
from app.productivity.reporting import ProductivityService
from app.productivity.tracker import TrackerData, build_tracker_report

UTC = timezone.utc
VAN = "America/Vancouver"
MON = date(2026, 3, 2)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Stack:
    def __init__(self, connection, owner: uuid.UUID | None = None, timezone_name: str = "UTC") -> None:
        self.clock = Clock()
        self.owner = owner
        scope = OwnerScope.account(owner) if owner else OwnerScope.ownerless()
        self.planning = PlanningService(PlanningRepository(connection), self.clock).scoped(scope)
        repository = ExecutionRepository(connection)
        self.executions = ExecutionService(repository, self.clock).scoped(scope)
        self.productivity = ProductivityService(repository.scoped(scope), clock=self.clock, history=self.planning,
                                                timezone_name=timezone_name)

    def place(self, task: Task, start: datetime, minutes: int = 60) -> ScheduledTask:
        day = start.astimezone(UTC).date()
        placement = ScheduledTask(task_id=task.id, user_id=self.owner, planned_date=day, timezone="UTC",
                                  planned_start=start, planned_end=start + timedelta(minutes=minutes),
                                  created_at=min(self.clock.now, start), updated_at=min(self.clock.now, start))
        existing = self.planning.placements_for_date(day)
        self.planning.replace_placements(day, day, [*existing, placement],
                                         expected_versions={p.id: p.version for p in existing})
        return self.planning.get_placement(placement.id)

    def plan(self, name: str, start: datetime, **fields):
        task = self.planning.create_task(Task(user_id=self.owner, name=name, category="study",
                                              estimated_duration_minutes=60, priority=5, **fields))
        return task, self.place(task, start)

    def attempt(self, task, placement) -> str:
        return self.executions.get_or_create_canonical_execution(task, placement).id

    def complete(self, task, placement, at: datetime, minutes: int = 30) -> str:
        execution_id = self.attempt(task, placement)
        self.clock.now = at - timedelta(minutes=minutes)
        self.executions.start(execution_id)
        self.clock.now = at
        self.executions.complete(execution_id)
        return execution_id

    def report(self, **kwargs):
        return self.productivity.build_tracker_report(**kwargs)


@pytest.fixture
def connection(tmp_path: Path):
    conn = get_connection(tmp_path / "app.db")
    yield conn
    conn.close()


@pytest.fixture
def stack(connection) -> Stack:
    return Stack(connection)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def test_every_status_lands_in_the_right_count_and_both_completion_formulas_stay_apart(stack) -> None:
    plans = {name: stack.plan(name, at(MON, hour)) for name, hour in (
        ("Done", 8), ("Skipped", 9), ("Cancelled", 10), ("Working", 11), ("Paused", 12), ("Untouched", 13))}
    stack.complete(*plans["Done"], at(MON, 9))
    stack.clock.now = at(MON, 9, 30)
    stack.executions.skip(stack.attempt(*plans["Skipped"]))
    stack.executions.cancel(stack.attempt(*plans["Cancelled"]))
    stack.clock.now = at(MON, 11, 5)
    stack.executions.start(stack.attempt(*plans["Working"]))  # an open session
    paused = stack.attempt(*plans["Paused"])
    stack.clock.now = at(MON, 12, 5)
    stack.executions.start(paused)
    stack.clock.now = at(MON, 12, 20)
    stack.executions.pause(paused)
    stack.clock.now = at(MON + timedelta(days=1), 9)

    built = stack.report()
    counts = built.general.counts
    assert (counts.planned, counts.completed, counts.skipped, counts.cancelled) == (6, 1, 1, 1)
    assert (counts.in_progress, counts.paused, counts.overdue_not_started, counts.unresolved) == (1, 1, 1, 3)
    # Due formula: the cancellation is excluded from the denominator ...
    assert (counts.due_denominator, counts.due_completion.numerator, counts.due_completion.value) == (5, 1, 0.2)
    assert (counts.due_skip.numerator, counts.due_skip.value) == (1, 0.2)
    # ... while the execution-based formula keeps it: completed / (completed + skipped + cancelled).
    stats = stack.productivity.build_dashboard().global_stats
    assert (stats.completed_count, stats.skipped_count, stats.cancelled_count, stats.terminal_count) == (1, 1, 1, 3)
    assert stats.completion_rate == round(1 / 3, 4)
    # The calendar classification counts the cancelled attempt as uncompleted: 1 of 6 is not a green day.
    day = next(view for view in built.time.days if view.local_date == MON)
    assert day.status_class == DayStatusClass.MOSTLY_PENDING and built.general.longest_green_streak.available is False
    assert built.general.activity.timed_completions == 1 and built.general.activity.productive_minutes == 30.0


def test_completions_are_dated_in_the_reporting_timezone_across_a_dst_change(connection) -> None:
    stack = Stack(connection, timezone_name=VAN)
    saturday, sunday = date(2026, 3, 7), date(2026, 3, 8)  # Vancouver springs forward on Sunday 8 March 2026
    late, placed = stack.plan("Late", at(sunday, 5))       # 21:00 Saturday in Vancouver (UTC-8)
    stack.complete(late, placed, at(sunday, 7, 30))        # 23:30 Saturday local
    after, placed = stack.plan("After", at(sunday, 20))    # 13:00 Sunday local (UTC-7 after the change)
    stack.complete(after, placed, at(sunday, 21))
    stack.clock.now = at(sunday + timedelta(days=2), 12)

    built = stack.report()
    days = {view.local_date: view for view in built.time.days}
    assert (days[saturday].counts.planned, days[saturday].activity.completions) == (1, 1)
    assert (days[sunday].counts.planned, days[sunday].activity.completions) == (1, 1)
    assert {view.bucket: view.counts.planned for view in built.time.buckets} == {
        "morning": 0, "afternoon": 0, "evening": 1, "night": 1}  # buckets use the plan's own timezone (UTC here)
    assert built.general.averages.elapsed_days == 3  # Saturday, the 23-hour Sunday and Monday: calendar days
    assert built.general.longest_green_streak.value == 2.0


def test_a_move_and_a_regeneration_keep_one_occurrence_on_its_current_date(stack) -> None:
    tuesday = MON + timedelta(days=1)
    task, placement = stack.plan("Essay", at(MON, 9))
    stack.clock.now = at(MON, 7)
    moved = workflow.reschedule_placement(stack.planning, placement.id, expected_version=placement.version,
                                          planned_date=tuesday, timezone_name="UTC", planned_start=at(tuesday, 9),
                                          planned_end=at(tuesday, 10)).replacement
    # A regeneration of Tuesday re-places the same task: the move's placement is superseded by the new one.
    replacement = ScheduledTask(task_id=task.id, planned_date=tuesday, timezone="UTC", planned_start=at(tuesday, 14),
                                planned_end=at(tuesday, 15), created_at=stack.clock.now, updated_at=stack.clock.now)
    stack.planning.replace_placements(tuesday, tuesday, [replacement], expected_versions={moved.id: moved.version})
    assert stack.planning.get_placement(moved.id, include_deleted=True).removal_reason == \
        PlacementRemovalReason.REGENERATED
    stack.complete(task, stack.planning.get_placement(replacement.id), at(tuesday, 15))
    stack.clock.now = at(tuesday + timedelta(days=1), 9)

    built = stack.report()
    assert (built.general.counts.planned, built.general.counts.completed) == (1, 1)  # one logical occurrence
    days = {view.local_date: view for view in built.time.days}
    assert (days[MON].counts.planned, days[MON].moved_out) == (0, 1)  # the original date says where it went
    assert (days[tuesday].counts.planned, days[tuesday].activity.completions) == (1, 1)
    assert built.completeness.moved_out == 1 and built.completeness.removed_from_plan == 0
    assert len(built.types) == 1 and built.types[0].periods["all_time"].counts.planned == 1


def test_recurring_occurrences_share_a_type_and_renamed_or_deleted_tasks_keep_their_history(stack) -> None:
    rule = RecurrenceSpec(frequency="daily", start_date=MON, timezone="UTC")
    series = stack.planning.create_task(Task(name="Standup", category="work", estimated_duration_minutes=15,
                                             priority=5, recurrence=rule))
    days = [MON, MON + timedelta(days=1)]
    occurrences = stack.planning.materialize_occurrences([
        Task(id=occurrence_task_id(series.id, day), name="Standup", category="work", estimated_duration_minutes=15,
             priority=5, series_id=series.id, occurrence_slot=day, required_date=day, series_version=series.version)
        for day in days])
    for occurrence, day in zip(occurrences, days):
        stack.complete(occurrence, stack.place(occurrence, at(day, 9), 15), at(day, 10), minutes=15)
    solo, placement = stack.plan("Reading", at(MON, 13), points=4)
    stack.complete(solo, placement, at(MON, 14))
    stack.planning.update_task(stack.planning.get_task(solo.id).model_copy(update={"name": "Skimming"}),
                               expected_version=stack.planning.get_task(solo.id).version)
    stack.planning.delete_task(solo.id, expected_version=stack.planning.get_task(solo.id).version)
    stack.clock.now = at(MON + timedelta(days=3), 9)

    built = stack.report()
    by_label = {view.label: view for view in built.types}
    assert "Standup" in by_label
    standup = by_label["Standup"].periods["all_time"]
    assert (standup.counts.planned, standup.counts.completed, standup.activity.completions) == (2, 2, 2)
    assert built.general.most_completed_type.winners[0].label == "Standup"
    # A deleted task's completion and points are removed with it: nothing of it is counted any more.
    reading = by_label.get("Reading")
    assert reading is None or reading.periods["all_time"].activity.completions == 0
    names = {record.name for record in built.records.values() if record.kind == "completion"}
    assert names == {"Standup"}
    assert built.general.counts.planned == 2  # the deleted task's placement left the plan (reported, not counted)
    assert built.completeness.removed_from_plan == 1


def test_medians_are_exact_across_reading_windows(stack) -> None:
    old = MON - timedelta(days=450)
    for index, (day, minutes) in enumerate([(old, 10), (old + timedelta(days=1), 20), (old + timedelta(days=2), 100),
                                            (MON - timedelta(days=2), 30), (MON - timedelta(days=1), 40)]):
        task, placement = stack.plan(f"Task {index}", at(day, 9))
        stack.complete(task, placement, at(day, 12), minutes=minutes)
    stack.clock.now = at(MON, 12)

    built = stack.report()
    assert built.completeness.windows_read == 2
    # Windows hold [10, 20, 100] and [30, 40]: the median of all five is 30, the median of the two medians 27.5.
    assert built.general.durations.pairs == 5 and built.general.durations.median_actual_minutes == 30.0
    assert built.general.activity.productive_minutes == 200.0


def test_a_lineage_longer_than_the_limit_is_reported_not_trusted() -> None:
    start = datetime(2026, 3, 2, 9, tzinfo=UTC)
    chain = [ScheduledTask(id=uuid.uuid4(), task_id=uuid.uuid4(), planned_date=MON, timezone="UTC",
                           planned_start=start, planned_end=start + timedelta(hours=1), created_at=start,
                           updated_at=start) for _ in range(MAX_LINEAGE_DEPTH + 3)]
    predecessors = {later.id: [earlier] for earlier, later in zip(chain, chain[1:])}
    history = collect_schedule_history(
        start, start + timedelta(days=1), in_range=lambda _start, _end: [chain[-1]],
        superseded_by=lambda ids: {key: predecessors[key] for key in ids if key in predecessors},
        executions_for=lambda _ids: [])
    assert history.lineage_truncated and len(history.placements) == MAX_LINEAGE_DEPTH + 1

    built = build_tracker_report(TrackerData(lineage_truncated=True), timezone_name="UTC", as_of=start)
    assert not built.completeness.complete and "lineage limit" in " ".join(built.completeness.notes)
    assert built.general.longest_green_streak.qualified


def test_each_owner_sees_only_their_own_tracker(connection) -> None:
    alice, bob = Stack(connection, uuid.uuid4()), Stack(connection, uuid.uuid4())
    task, placement = alice.plan("Private", at(MON, 9), points=7)
    alice.complete(task, placement, at(MON, 10))
    for stack in (alice, bob):
        stack.clock.now = at(MON + timedelta(days=1), 9)

    mine, theirs = alice.report(), bob.report()
    assert (mine.general.activity.completions, mine.general.activity.known_points) == (1, 7)
    assert theirs.general.activity.completions == 0 and theirs.types == [] and theirs.records == {}
    assert theirs.general.counts.planned == 0 and not theirs.general.highest_point_day.available
