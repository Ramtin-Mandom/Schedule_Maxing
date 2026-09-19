"""Tests for app/productivity/data_prep.py: period filtering, implausible-duration
flagging, and start-delay recovery for statuses M1 never computes it for."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.productivity.data_prep import MAX_PLAUSIBLE_DURATION_MINUTES, build_observations
from tests.productivity.fixtures import FakeClock


def test_build_observations_all_time_includes_everything(repository: ExecutionRepository) -> None:
    clock = FakeClock(datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc))
    service = ExecutionService(repository, clock=clock)
    service.create_execution(
        task_name="A", category="study", tag="math", planned_date=1,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )

    observations = build_observations(repository, period="all_time")
    assert len(observations) == 1


def test_build_observations_last_7_days_excludes_old_records(repository: ExecutionRepository) -> None:
    clock = FakeClock(datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc))
    service = ExecutionService(repository, clock=clock)
    service.create_execution(
        task_name="Old", category="study", tag="math", planned_date=1,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )

    clock.advance(timedelta(days=10))
    service.create_execution(
        task_name="Recent", category="study", tag="math", planned_date=2,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )

    reference_now = clock()
    observations = build_observations(repository, period="last_7_days", now=reference_now)

    assert [observation.task_name for observation in observations] == ["Recent"]


def test_implausible_duration_is_flagged_not_dropped(repository: ExecutionRepository) -> None:
    clock = FakeClock(datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc))
    service = ExecutionService(repository, clock=clock)
    execution = service.create_execution(
        task_name="Marathon", category="study", tag="math", planned_date=1,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )
    service.start(execution.id)
    clock.advance(timedelta(minutes=MAX_PLAUSIBLE_DURATION_MINUTES + 60))
    service.complete(execution.id)

    observations = build_observations(repository)
    assert len(observations) == 1
    observation = observations[0]
    # The value itself is preserved untouched -- only flagged, never dropped or corrected.
    assert observation.actual_active_duration_minutes == MAX_PLAUSIBLE_DURATION_MINUTES + 60
    assert observation.is_duration_plausible is False


def test_plausible_duration_is_not_flagged(repository: ExecutionRepository) -> None:
    clock = FakeClock(datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc))
    service = ExecutionService(repository, clock=clock)
    execution = service.create_execution(
        task_name="Normal", category="study", tag="math", planned_date=1,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )
    service.start(execution.id)
    clock.advance(timedelta(minutes=65))
    service.complete(execution.id)

    observations = build_observations(repository)
    assert observations[0].is_duration_plausible is True


def test_start_delay_recovered_for_skipped_after_starting(repository: ExecutionRepository) -> None:
    clock = FakeClock(datetime(2024, 1, 1, 9, 20, tzinfo=timezone.utc))  # 09:20 = minute 560
    service = ExecutionService(repository, clock=clock)
    execution = service.create_execution(
        task_name="Skipped", category="study", tag="math", planned_date=1,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,  # planned 09:00
    )
    service.start(execution.id)  # actually started at 09:20 -> 20 minutes late
    clock.advance(timedelta(minutes=5))
    service.skip(execution.id)

    observations = build_observations(repository)
    observation = observations[0]
    assert observation.status.value == "skipped"
    # M1 never persists start_delay_minutes for a skip; data_prep must recover it.
    assert observation.start_delay_minutes == 20.0


def test_start_delay_is_none_for_never_started_execution(repository: ExecutionRepository) -> None:
    clock = FakeClock(datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc))
    service = ExecutionService(repository, clock=clock)
    execution = service.create_execution(
        task_name="Untouched", category="study", tag="math", planned_date=1,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )
    service.skip(execution.id)  # scheduled -> skipped, no session ever created

    observations = build_observations(repository)
    assert observations[0].start_delay_minutes is None
    assert observations[0].actual_active_duration_minutes is None


def test_days_override_takes_precedence_over_period(repository: ExecutionRepository) -> None:
    clock = FakeClock(datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc))
    service = ExecutionService(repository, clock=clock)
    service.create_execution(
        task_name="Old", category="study", tag="math", planned_date=1,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )

    clock.advance(timedelta(days=20))
    service.create_execution(
        task_name="Recent", category="study", tag="math", planned_date=2,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )

    reference_now = clock()
    # period="all_time" would keep both; days=30 (explicit override) should still keep both,
    # days=7 should keep only the recent one.
    all_within_30 = build_observations(repository, period="all_time", days=30, now=reference_now)
    only_within_7 = build_observations(repository, period="all_time", days=7, now=reference_now)

    assert {observation.task_name for observation in all_within_30} == {"Old", "Recent"}
    assert [observation.task_name for observation in only_within_7] == ["Recent"]


def test_days_none_falls_back_to_period(repository: ExecutionRepository) -> None:
    clock = FakeClock(datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc))
    service = ExecutionService(repository, clock=clock)
    service.create_execution(
        task_name="Old", category="study", tag="math", planned_date=1,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )
    clock.advance(timedelta(days=10))
    service.create_execution(
        task_name="Recent", category="study", tag="math", planned_date=2,
        planned_start=540, planned_end=600, planned_duration=60, priority=5,
    )

    # days omitted (None) -> behaves exactly like the original period-only API.
    observations = build_observations(repository, period="last_7_days", now=clock())
    assert [observation.task_name for observation in observations] == ["Recent"]
