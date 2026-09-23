"""Tests for app/productivity/data_prep.py's handling of canonical
(Task 2 / Schedule Maxing v2) TaskExecution rows alongside legacy ones:
planned weekday/time-bucket basis, cancelled's terminal-but-not-completed-or-
skipped treatment, and mixed legacy/canonical reports and exports.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.models import LocalTimeWindow, ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.productivity.data_prep import build_observations
from app.productivity.exporters import export_report_to_csv, export_report_to_json
from app.productivity.reporting import ProductivityService
from app.productivity.stats import ProductivityThresholds


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def __call__(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


@pytest.fixture
def make_task(planning_repository: PlanningRepository):
    """Build and persist a canonical Task (a new execution may only link to a
    persisted task since schema v3 -- see app/execution/db.py)."""

    def _make(**overrides) -> Task:
        defaults = dict(
            name="Study Math",
            category="study",
            tags=["math"],
            estimated_duration_minutes=60,
            priority=8,
            preferred_time_window=LocalTimeWindow(start_minute=540, end_minute=660),
        )
        defaults.update(overrides)
        task = Task(**defaults)
        planning_repository.insert_task(task)
        return task

    return _make


@pytest.fixture
def make_placement(planning_repository: PlanningRepository):
    def _make(task: Task, *, start: datetime, end: datetime, tz: str = "America/New_York") -> ScheduledTask:
        placement = ScheduledTask(task_id=task.id, planned_date=start.date(), timezone=tz, planned_start=start, planned_end=end)
        planning_repository.insert_placement(placement)
        return placement

    return _make


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2024, 6, 3, 13, 0, tzinfo=timezone.utc))  # Monday, 09:00 America/New_York


@pytest.fixture
def service(repository: ExecutionRepository, clock: FakeClock) -> ExecutionService:
    return ExecutionService(repository, clock=clock)


def test_canonical_observation_uses_planned_date_for_weekday_not_created_at(
    service: ExecutionService, repository: ExecutionRepository, clock: FakeClock
, make_task, make_placement) -> None:
    # canonical_planned_date is a Saturday (2024-06-08); created_at (the
    # clock, when the execution is created) is a Monday (2024-06-03). The
    # canonical basis must report the Saturday, not the created_at Monday.
    task = make_task()
    planned_start = datetime(2024, 6, 8, 13, 0, tzinfo=timezone.utc)  # Saturday 09:00 America/New_York
    placement = make_placement(task, start=planned_start, end=planned_start + timedelta(hours=1))
    service.create_canonical_execution(task, placement)

    [observation] = build_observations(repository)

    assert observation.effective_planned_date == planned_start.date()
    assert observation.day_of_week == "Saturday"


def test_legacy_observation_falls_back_to_created_at_weekday(
    service: ExecutionService, repository: ExecutionRepository, clock: FakeClock
) -> None:
    # clock is a Monday; legacy rows have no real planned date, so the
    # created_at-based approximation is used, unchanged from before.
    service.create_execution(
        task_name="Legacy Task", category="study", tag="math",
        planned_date=1, planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )

    [observation] = build_observations(repository)

    assert observation.effective_planned_date is None
    assert observation.day_of_week == "Monday"


def test_canonical_observation_time_bucket_uses_local_wall_clock(
    service: ExecutionService, repository: ExecutionRepository
, make_task, make_placement) -> None:
    task = make_task()
    # 13:00 UTC == 09:00 America/New_York -> morning bucket.
    planned_start = datetime(2024, 6, 3, 13, 0, tzinfo=timezone.utc)
    placement = make_placement(task, start=planned_start, end=planned_start + timedelta(hours=1), tz="America/New_York")
    service.create_canonical_execution(task, placement)

    [observation] = build_observations(repository)

    assert observation.time_bucket.value == "morning"
    assert observation.planned_start == 9 * 60  # local minute-of-day, not the raw UTC minute (780)


def test_cancelled_is_terminal_but_not_completed_or_skipped(
    service: ExecutionService, repository: ExecutionRepository
, make_task) -> None:
    task = make_task()
    execution = service.create_canonical_execution(task)
    service.cancel(execution.id)

    [observation] = build_observations(repository)

    assert observation.is_cancelled is True
    assert observation.is_completed is False
    assert observation.is_skipped is False
    assert observation.is_terminal is True


def test_cancelled_dilutes_completion_and_skip_rate_denominator(
    service: ExecutionService, repository: ExecutionRepository
, make_task) -> None:
    from app.productivity.stats import compute_segment_stats

    # 1 completed, 1 cancelled -> terminal_count=2, completion_rate=0.5, not 1.0.
    task = make_task(name="Completed Task")
    execution = service.create_canonical_execution(task)
    service.start(execution.id)
    service.complete(execution.id)

    cancelled_task = make_task(name="Cancelled Task")
    cancelled_execution = service.create_canonical_execution(cancelled_task)
    service.cancel(cancelled_execution.id)

    observations = build_observations(repository)
    stats = compute_segment_stats(observations)

    assert stats.terminal_count == 2
    assert stats.cancelled_count == 1
    assert stats.completion_rate == pytest.approx(0.5)
    assert stats.skip_rate == pytest.approx(0.0)


def test_mixed_legacy_and_canonical_records_in_one_report(
    service: ExecutionService, repository: ExecutionRepository, clock: FakeClock, tmp_path: Path
, make_task, make_placement) -> None:
    legacy_execution = service.create_execution(
        task_name="Legacy Study", category="study", tag="math",
        planned_date=1, planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )
    service.start(legacy_execution.id)
    clock.advance(timedelta(minutes=45))
    service.complete(legacy_execution.id)

    task = make_task(name="Canonical Study")
    planned_start = datetime(2024, 6, 4, 13, 0, tzinfo=timezone.utc)
    placement = make_placement(task, start=planned_start, end=planned_start + timedelta(hours=1))
    canonical_execution = service.create_canonical_execution(task, placement)
    service.start(canonical_execution.id)
    clock.advance(timedelta(minutes=50))
    service.complete(canonical_execution.id)

    productivity_service = ProductivityService(repository, thresholds=ProductivityThresholds(low=1, moderate=2, high=3))
    report = productivity_service.generate_report()

    assert report.observation_count == 2
    assert report.global_stats.completed_duration_count == 2
    assert report.by_category["study"].completed_duration_count == 2

    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    export_report_to_json(report, json_path)
    export_report_to_csv(report, csv_path)

    assert json_path.exists() and json_path.stat().st_size > 0
    assert csv_path.exists() and csv_path.stat().st_size > 0
