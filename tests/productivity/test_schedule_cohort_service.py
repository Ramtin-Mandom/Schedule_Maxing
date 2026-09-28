"""The schedule-cohort report through ProductivityService over the real SQLite
planning and execution services: the lifecycle's own durations, historical
snapshots after edits, legacy history kept out of calendar reports, lineage
after regeneration, owner scopes, the compatible terminal-outcome report,
and reads that never write."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning import workflow
from app.planning.application import PlanningService, RangeScope
from app.planning.models import ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope
from app.productivity.reporting import ProductivityService
from app.ui.planning_controller import PlanningController

VAN = "America/Vancouver"
MON = date(2026, 3, 2)


class Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)  # Mon 07:00 in Vancouver

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class Stack:
    def __init__(self, tmp_path: Path, owner: uuid.UUID | None = None, now: datetime | None = None) -> None:
        self.clock = Clock(now)
        self.connection = get_connection(tmp_path / "app.db")
        planning = PlanningService(PlanningRepository(self.connection), self.clock)
        executions = ExecutionRepository(self.connection)
        scope = OwnerScope.account(owner) if owner else OwnerScope.ownerless()
        self.planning = planning.scoped(scope)
        self.executions = ExecutionService(executions, self.clock).scoped(scope)
        self.productivity = ProductivityService(executions.scoped(scope), clock=self.clock, history=self.planning,
                                                timezone_name=VAN)
        self.owner = owner

    def planned(self, name: str, local_hour: int, minutes: int = 60, day: date = MON, **fields):
        task = self.planning.create_task(Task(user_id=self.owner, name=name, category=fields.pop("category", "study"),
                                              estimated_duration_minutes=minutes, priority=5, **fields))
        start = datetime(day.year, day.month, day.day, local_hour, tzinfo=timezone.utc) + timedelta(hours=8)
        placement = ScheduledTask(task_id=task.id, user_id=self.owner, planned_date=day, timezone=VAN,
                                  planned_start=start, planned_end=start + timedelta(minutes=minutes),
                                  created_at=self.clock.now, updated_at=self.clock.now)
        existing = self.planning.placements_for_date(day)
        self.planning.replace_placements(day, day, [*existing, placement],
                                         expected_versions={p.id: p.version for p in existing})
        return task, self.planning.get_placement(placement.id)

    def report(self, **kwargs):
        return self.productivity.build_schedule_cohort_report(start_date=kwargs.pop("start", MON),
                                                              end_date=kwargs.pop("end", MON), **kwargs)


@pytest.fixture
def stack(tmp_path: Path):
    s = Stack(tmp_path)
    yield s
    s.connection.close()


def test_sixty_planned_two_twenty_minute_sessions_and_a_pause_are_forty_active_minutes(stack: Stack) -> None:
    task, placement = stack.planned("Write", 9)
    execution = stack.executions.get_or_create_canonical_execution(task, placement)
    stack.clock.now = placement.planned_start
    stack.executions.start(execution.id)
    stack.clock.advance(minutes=20)
    stack.executions.pause(execution.id)
    stack.clock.advance(minutes=10)
    stack.executions.resume(execution.id)
    stack.clock.advance(minutes=20)
    stack.executions.complete(execution.id)
    stack.clock.advance(hours=12)

    report = stack.report()
    [occurrence] = report.occurrences
    assert (occurrence.estimate_minutes, occurrence.actual_active_minutes) == (60.0, 40.0)
    assert report.duration.median_signed_error_minutes == -20.0 and report.duration.median_ratio == 0.667
    assert report.due_completion.value == 1.0 and report.timezone == VAN


def test_a_reclassified_and_renamed_task_keeps_its_historical_category(stack: Stack) -> None:
    task, placement = stack.planned("Read", 9, category="study")
    stack.planning.update_task(task.model_copy(update={"name": "Skim", "category": "leisure"}),
                               expected_version=task.version)
    stack.clock.advance(days=1)
    report = stack.report()
    assert set(report.by_category) == {"study"} and report.occurrences[0].category_source == "placement_snapshot"


def test_legacy_history_stays_out_of_calendar_reports_but_in_the_compatible_one(stack: Stack) -> None:
    legacy = stack.executions.create_execution(task_name="Old", category="study", tag="", planned_date=1,
                                               planned_start=540, planned_end=600, planned_duration=60, priority=5)
    stack.executions.skip(legacy.id)
    stack.clock.advance(days=1)
    assert stack.report().occurrence_count == 0  # no calendar anchor: never given a fabricated date
    compatible = stack.productivity.generate_report()
    assert compatible.global_stats.terminal_count == 1 and compatible.global_stats.skip_rate == 1.0


def test_reports_never_write_and_never_create_executions(stack: Stack) -> None:
    stack.planned("Untouched", 9)
    stack.clock.advance(days=1)
    before = [tuple(row) for table in ("executions", "scheduled_tasks", "sync_dirty")
              for row in stack.connection.execute(f"SELECT * FROM {table}")]
    report = stack.report()
    assert report.due_outcomes.overdue_unattempted == 1 and report.occurrences[0].execution_id is None
    assert [tuple(row) for table in ("executions", "scheduled_tasks", "sync_dirty")
            for row in stack.connection.execute(f"SELECT * FROM {table}")] == before


def test_moves_and_an_unchanged_regeneration_count_one_occurrence(tmp_path: Path) -> None:
    # The engine stamps new placements with the real clock, so this stack runs on it too.
    stack = Stack(tmp_path, now=datetime.now(timezone.utc))
    controller = PlanningController(service=stack.planning, timezone=VAN, project_root=str(tmp_path))
    task = controller.add_or_update_task(Task(name="Plan", category="study", estimated_duration_minutes=60,
                                              priority=5, required_date=MON)).value
    assert controller.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE).ok
    [first] = stack.planning.placements_for_date(MON)
    for hour in (14, 16):  # two explicit moves (local 14:00, 16:00)
        current = stack.planning.placements_for_date(MON)[0]
        start = datetime(2026, 3, 2, tzinfo=timezone.utc) + timedelta(hours=hour + 8)
        workflow.reschedule_placement(stack.planning, current.id, expected_version=current.version, planned_date=MON,
                                      timezone_name=VAN, planned_start=start, planned_end=start + timedelta(hours=1))
    controller.add_or_update_task(stack.planning.get_task(task.id).model_copy(update={"priority": 6}),
                                  expected_version=stack.planning.get_task(task.id).version)
    stack.clock.advance(days=1)

    report = stack.report()
    assert report.occurrence_count == 1
    assert (report.reschedules.reschedule_events, report.reschedules.reschedule_rate.value) == (2, 1.0)
    assert report.reschedules.regeneration_events == 0
    assert report.occurrences[0].original_placement_id == first.id
    stack.connection.close()


def test_each_owner_sees_only_its_own_history(tmp_path: Path) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    a = Stack(tmp_path, alice)
    task, placement = a.planned("Mine", 9)
    a.executions.skip(a.executions.get_or_create_canonical_execution(task, placement).id)
    a.clock.advance(days=1)
    a.connection.close()
    b = Stack(tmp_path, bob)
    b.planned("Theirs", 10)
    b.clock.advance(days=1)
    try:
        report = b.report()
        assert report.occurrence_count == 1 and report.due_outcomes.skipped == 0
        assert report.due_outcomes.overdue_unattempted == 1
    finally:
        b.connection.close()


def test_the_reporting_timezone_is_explicit(tmp_path: Path) -> None:
    connection = get_connection(tmp_path / "app.db")
    try:
        service = ProductivityService(ExecutionRepository(connection),
                                      history=PlanningService(PlanningRepository(connection)))
        with pytest.raises(ValueError, match="reporting timezone"):
            service.build_schedule_cohort_report(start_date=MON, end_date=MON)
        assert service.build_schedule_cohort_report(start_date=MON, end_date=MON, timezone_name="UTC").timezone == "UTC"
    finally:
        connection.close()
