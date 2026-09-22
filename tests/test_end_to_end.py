"""End-to-end integration tests spanning Tasks 1-6: canonical import,
allocation, day generation, canonical execution tracking, and productivity
reporting working together as one coherent pipeline.

Narrower integration points (per-day overrides reaching scoring,
week/month allocation calling the Day Scheduler zero times, exactly-one
selected-day generation, precise/ADHD behavior, mixed legacy/canonical
history, stale-result invalidation) already have focused coverage in
tests/planning/, tests/test_day_engine.py, tests/execution/, and
tests/test_main_cli.py; this file covers the remaining, genuinely
cross-cutting scenarios.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.optimizer import generate_day_schedule
from app.planning.models import DaySchedule, DayScheduleOutput, LocalTimeWindow, Task, TaskRegistry
from app.planning.preferences import resolve_day_preferences
from app.productivity.data_prep import build_observations
from app.productivity.reporting import ProductivityService
from app.productivity.stats import ProductivityThresholds

DAY = date(2024, 6, 3)
TZ = "UTC"


def _make_task(name: str, **overrides) -> Task:
    defaults = dict(
        name=name, category="study", estimated_duration_minutes=60, priority=5,
        preferred_time_window=LocalTimeWindow(start_minute=540, end_minute=660),
    )
    defaults.update(overrides)
    return Task(**defaults)


# -----------------------------------------------------------------------------
# Duplicate task names remain distinguishable from placement through execution
# -----------------------------------------------------------------------------


def test_duplicate_names_stay_distinct_from_generation_through_execution(tmp_path):
    task_a = _make_task("Study Session")
    task_b = _make_task("Study Session")  # identical name, distinct id

    registry = TaskRegistry()
    registry.add(task_a)
    registry.add(task_b)
    day_schedule = DaySchedule(date=DAY, timezone=TZ, task_ids=[task_a.id, task_b.id], tasks=registry)
    preferences = resolve_day_preferences(date=DAY, timezone=TZ)

    output = generate_day_schedule(day_schedule, preferences)
    assert len(output.placements) == 2
    placements_by_task_id = {p.task_id: p for p in output.placements}
    assert task_a.id in placements_by_task_id
    assert task_b.id in placements_by_task_id
    assert placements_by_task_id[task_a.id].id != placements_by_task_id[task_b.id].id

    # Both placements flow into distinct executions, identified by
    # task_id/scheduled_task_id -- never by the (identical) task name.
    connection = get_connection(tmp_path / "executions.db")
    try:
        repository = ExecutionRepository(connection)
        service = ExecutionService(repository)

        execution_a = service.get_or_create_canonical_execution(task_a, placements_by_task_id[task_a.id])
        execution_b = service.get_or_create_canonical_execution(task_b, placements_by_task_id[task_b.id])

        assert execution_a.id != execution_b.id
        assert execution_a.task_id == task_a.id
        assert execution_b.task_id == task_b.id
        assert execution_a.task_name == execution_b.task_name == "Study Session"

        # Re-selecting either placement again resolves to the same
        # execution, not a fresh duplicate, and not the other task's.
        reselected_a = service.get_or_create_canonical_execution(task_a, placements_by_task_id[task_a.id])
        assert reselected_a.id == execution_a.id
    finally:
        connection.close()


# -----------------------------------------------------------------------------
# Canonical execution completion flows into productivity reports/export
# -----------------------------------------------------------------------------


def test_full_pipeline_generation_execution_and_productivity_report(tmp_path):
    """Canonical DaySchedule -> generate_day_schedule -> canonical
    execution -> start/complete -> ProductivityService report, as one
    coherent flow using only public APIs from each milestone."""
    task = _make_task("Deep Work", category="work", priority=8)
    registry = TaskRegistry()
    registry.add(task)
    day_schedule = DaySchedule(date=DAY, timezone=TZ, task_ids=[task.id], tasks=registry)
    preferences = resolve_day_preferences(date=DAY, timezone=TZ)

    output = generate_day_schedule(day_schedule, preferences)
    placement = output.placements[0]

    connection = get_connection(tmp_path / "executions.db")
    try:
        repository = ExecutionRepository(connection)
        clock_time = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)

        class FakeClock:
            def __init__(self, start):
                self._current = start

            def __call__(self):
                return self._current

            def advance(self, minutes):
                from datetime import timedelta

                self._current += timedelta(minutes=minutes)

        clock = FakeClock(clock_time)
        service = ExecutionService(repository, clock=clock)

        execution = service.get_or_create_canonical_execution(task, placement)
        service.start(execution.id)
        clock.advance(55)
        completed = service.complete(execution.id)

        assert completed.actual_active_duration_minutes == 55.0
        assert completed.task_id == task.id

        productivity_service = ProductivityService(repository, thresholds=ProductivityThresholds(low=1, moderate=2, high=3))
        report = productivity_service.generate_report()

        assert report.observation_count == 1
        assert report.by_category["work"].completed_duration_count == 1

        observations = build_observations(repository)
        assert observations[0].effective_planned_date == DAY  # canonical date basis, not created_at
    finally:
        connection.close()


# -----------------------------------------------------------------------------
# DayScheduleOutput JSON round trip preserves identity
# -----------------------------------------------------------------------------


def test_day_schedule_output_json_round_trip_preserves_identity():
    task = _make_task("Study Math", tags=["math"])
    registry = TaskRegistry()
    registry.add(task)
    day_schedule = DaySchedule(date=DAY, timezone=TZ, task_ids=[task.id], tasks=registry)
    preferences = resolve_day_preferences(date=DAY, timezone=TZ)

    output = generate_day_schedule(day_schedule, preferences)

    restored = DayScheduleOutput.model_validate_json(output.model_dump_json())

    assert restored.date == output.date
    assert restored.placements[0].id == output.placements[0].id
    assert restored.placements[0].task_id == task.id
    assert restored.placements[0].planned_start == output.placements[0].planned_start
    assert restored.tasks.get(task.id).name == "Study Math"

    # Re-running generate_day_schedule with the restored output as
    # previous_result reuses the same placement identity, exactly as it
    # would with the original (unserialized) output -- confirming the
    # round trip is lossless enough to support identity reconciliation.
    reconciled = generate_day_schedule(day_schedule, preferences, previous_result=restored)
    assert reconciled.placements[0].id == output.placements[0].id
