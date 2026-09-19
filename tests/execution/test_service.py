"""Tests for app/execution/service.py: state transitions, pause/resume
behavior, completion metrics, and feedback validation.

All timing is driven by an explicit FakeClock so tests never sleep and never
depend on wall-clock time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.execution.errors import InvalidFeedbackError, InvalidTransitionError
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import (
    ExecutionService,
    compute_active_duration_minutes,
    compute_start_delay_minutes,
)
from app.models import ScheduledTask, TimeWindow
from tests.execution.conftest import make_execution_kwargs


class FakeClock:
    """A controllable clock: returns the same instant until explicitly advanced."""

    def __init__(self, start: datetime) -> None:
        self._current = start

    def __call__(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def service_with_clock(repository: ExecutionRepository, clock: FakeClock) -> ExecutionService:
    return ExecutionService(repository, clock=clock)


# ----------------------------------------------------------------------
# Creation
# ----------------------------------------------------------------------


def test_create_execution_starts_scheduled_with_unique_id(service_with_clock: ExecutionService) -> None:
    first = service_with_clock.create_execution(**make_execution_kwargs())
    second = service_with_clock.create_execution(**make_execution_kwargs())

    assert first.status == ExecutionStatus.SCHEDULED
    assert first.id != second.id
    assert first.task_name == "Study Math"
    assert first.created_at == first.updated_at


def test_create_execution_from_scheduled_task_derives_duration(service_with_clock: ExecutionService) -> None:
    scheduled_task = ScheduledTask(
        name="Study Math",
        category="study",
        tag="math",
        time_window=TimeWindow(start_time=540, end_time=660),
        score=42.0,
    )

    execution = service_with_clock.create_execution_from_scheduled_task(
        scheduled_task, planned_date=1, priority=8
    )

    assert execution.planned_start == 540
    assert execution.planned_end == 660
    assert execution.planned_duration == 120
    assert execution.priority == 8
    assert execution.planned_date == 1


# ----------------------------------------------------------------------
# Valid transitions
# ----------------------------------------------------------------------


def test_start_opens_a_session_and_sets_in_progress(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())

    started = service_with_clock.start(execution.id)

    assert started.status == ExecutionStatus.IN_PROGRESS


def test_pause_then_resume_round_trip(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.start(execution.id)

    paused = service_with_clock.pause(execution.id)
    assert paused.status == ExecutionStatus.PAUSED

    resumed = service_with_clock.resume(execution.id)
    assert resumed.status == ExecutionStatus.IN_PROGRESS


def test_complete_from_in_progress(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.start(execution.id)

    completed = service_with_clock.complete(execution.id)
    assert completed.status == ExecutionStatus.COMPLETED
    assert completed.actual_active_duration_minutes is not None


def test_complete_from_paused(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.start(execution.id)
    service_with_clock.pause(execution.id)

    completed = service_with_clock.complete(execution.id)
    assert completed.status == ExecutionStatus.COMPLETED


@pytest.mark.parametrize("source_status_setup", ["scheduled", "in_progress", "paused"])
def test_skip_from_each_valid_source(service_with_clock: ExecutionService, source_status_setup: str) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())

    if source_status_setup in ("in_progress", "paused"):
        service_with_clock.start(execution.id)
    if source_status_setup == "paused":
        service_with_clock.pause(execution.id)

    skipped = service_with_clock.skip(execution.id)
    assert skipped.status == ExecutionStatus.SKIPPED
    assert skipped.actual_active_duration_minutes is None
    assert skipped.duration_variance_minutes is None
    assert skipped.start_delay_minutes is None


# ----------------------------------------------------------------------
# Invalid transitions
# ----------------------------------------------------------------------


def test_pause_from_scheduled_is_invalid(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())

    with pytest.raises(InvalidTransitionError):
        service_with_clock.pause(execution.id)


def test_resume_from_scheduled_is_invalid(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())

    with pytest.raises(InvalidTransitionError):
        service_with_clock.resume(execution.id)


def test_complete_from_scheduled_is_invalid(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())

    with pytest.raises(InvalidTransitionError):
        service_with_clock.complete(execution.id)


def test_start_twice_is_invalid(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.start(execution.id)

    with pytest.raises(InvalidTransitionError):
        service_with_clock.start(execution.id)


@pytest.mark.parametrize("action", ["start", "pause", "resume", "complete", "skip"])
def test_no_transition_out_of_completed(service_with_clock: ExecutionService, action: str) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.start(execution.id)
    service_with_clock.complete(execution.id)

    with pytest.raises(InvalidTransitionError):
        getattr(service_with_clock, action)(execution.id)


@pytest.mark.parametrize("action", ["start", "pause", "resume", "complete", "skip"])
def test_no_transition_out_of_skipped(service_with_clock: ExecutionService, action: str) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.skip(execution.id)

    with pytest.raises(InvalidTransitionError):
        getattr(service_with_clock, action)(execution.id)


def test_invalid_transition_error_names_states(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())

    with pytest.raises(InvalidTransitionError) as excinfo:
        service_with_clock.pause(execution.id)

    assert excinfo.value.current_status == ExecutionStatus.SCHEDULED
    assert excinfo.value.attempted_status == ExecutionStatus.PAUSED
    assert execution.id in str(excinfo.value)


# ----------------------------------------------------------------------
# Active-duration and pause exclusion
# ----------------------------------------------------------------------


def test_paused_time_is_excluded_from_active_duration(
    service_with_clock: ExecutionService, clock: FakeClock
) -> None:
    execution = service_with_clock.create_execution(
        **make_execution_kwargs(planned_start=540, planned_duration=45)
    )

    service_with_clock.start(execution.id)  # 09:00
    clock.advance(timedelta(minutes=30))  # worked 09:00 - 09:30
    service_with_clock.pause(execution.id)

    clock.advance(timedelta(hours=2))  # a 2-hour paused gap; must not count

    service_with_clock.resume(execution.id)  # 11:30
    clock.advance(timedelta(minutes=15))  # worked 11:30 - 11:45
    completed = service_with_clock.complete(execution.id)

    assert completed.actual_active_duration_minutes == pytest.approx(45.0)


def test_duration_variance_positive_when_over_estimate(
    service_with_clock: ExecutionService, clock: FakeClock
) -> None:
    execution = service_with_clock.create_execution(
        **make_execution_kwargs(planned_start=540, planned_duration=30)
    )

    service_with_clock.start(execution.id)
    clock.advance(timedelta(minutes=45))
    completed = service_with_clock.complete(execution.id)

    assert completed.actual_active_duration_minutes == pytest.approx(45.0)
    assert completed.duration_variance_minutes == pytest.approx(15.0)


def test_duration_variance_negative_when_under_estimate(
    service_with_clock: ExecutionService, clock: FakeClock
) -> None:
    execution = service_with_clock.create_execution(
        **make_execution_kwargs(planned_start=540, planned_duration=60)
    )

    service_with_clock.start(execution.id)
    clock.advance(timedelta(minutes=45))
    completed = service_with_clock.complete(execution.id)

    assert completed.duration_variance_minutes == pytest.approx(-15.0)


def test_start_delay_reflects_late_start(service_with_clock: ExecutionService, clock: FakeClock) -> None:
    # clock starts at 09:00 UTC; planned_start is 08:30 (510 minutes) -> 30 minutes late.
    execution = service_with_clock.create_execution(
        **make_execution_kwargs(planned_start=510, planned_duration=30)
    )

    service_with_clock.start(execution.id)
    clock.advance(timedelta(minutes=30))
    completed = service_with_clock.complete(execution.id)

    assert completed.start_delay_minutes == pytest.approx(30.0)


def test_start_delay_uses_earliest_session_across_pause_resume(
    service_with_clock: ExecutionService, clock: FakeClock
) -> None:
    execution = service_with_clock.create_execution(
        **make_execution_kwargs(planned_start=540, planned_duration=30)
    )

    service_with_clock.start(execution.id)  # first session starts 09:00 (delay 0)
    clock.advance(timedelta(minutes=10))
    service_with_clock.pause(execution.id)
    clock.advance(timedelta(hours=1))
    service_with_clock.resume(execution.id)  # second session starts later
    clock.advance(timedelta(minutes=5))
    completed = service_with_clock.complete(execution.id)

    # Delay must be based on the *first* session (09:00), not the resumed one.
    assert completed.start_delay_minutes == pytest.approx(0.0)


# ----------------------------------------------------------------------
# Pure duration/delay helper functions
# ----------------------------------------------------------------------


def test_compute_active_duration_minutes_ignores_open_sessions() -> None:
    from app.execution.models import WorkSession

    sessions = [
        WorkSession(id=1, execution_id="e", started_at="2024-01-01T09:00:00+00:00", ended_at="2024-01-01T09:30:00+00:00"),
        WorkSession(id=2, execution_id="e", started_at="2024-01-01T10:00:00+00:00", ended_at=None),
    ]

    assert compute_active_duration_minutes(sessions) == pytest.approx(30.0)


def test_compute_start_delay_minutes_negative_for_early_start() -> None:
    # Started at 08:45 (525 minutes) but planned for 09:00 (540 minutes) -> -15.
    delay = compute_start_delay_minutes("2024-01-01T08:45:00+00:00", planned_start=540)
    assert delay == pytest.approx(-15.0)


# ----------------------------------------------------------------------
# Feedback
# ----------------------------------------------------------------------


def test_record_feedback_updates_only_provided_fields(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())

    first = service_with_clock.record_feedback(execution.id, focus_rating=4)
    assert first.focus_rating == 4
    assert first.energy_rating is None

    second = service_with_clock.record_feedback(execution.id, energy_rating=3, note="Went well")
    assert second.focus_rating == 4  # untouched by the second call
    assert second.energy_rating == 3
    assert second.note == "Went well"


def test_record_feedback_bumps_updated_at(service_with_clock: ExecutionService, clock: FakeClock) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    clock.advance(timedelta(minutes=5))

    updated = service_with_clock.record_feedback(execution.id, interruption_count=2)
    assert updated.updated_at != execution.updated_at


def test_record_feedback_allowed_in_any_status(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.skip(execution.id)

    updated = service_with_clock.record_feedback(execution.id, note="Skipped, ran out of time")
    assert updated.status == ExecutionStatus.SKIPPED
    assert updated.note == "Skipped, ran out of time"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"focus_rating": 0},
        {"focus_rating": 6},
        {"energy_rating": 0},
        {"energy_rating": 6},
        {"interruption_count": -1},
    ],
)
def test_record_feedback_rejects_out_of_range_values(
    service_with_clock: ExecutionService, kwargs: dict
) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())

    with pytest.raises(InvalidFeedbackError):
        service_with_clock.record_feedback(execution.id, **kwargs)


# ----------------------------------------------------------------------
# open_execution_service context manager
# ----------------------------------------------------------------------


def test_open_execution_service_closes_connection(tmp_path) -> None:
    from app.execution.service import open_execution_service

    db_path = tmp_path / "executions.db"

    with open_execution_service(db_path) as service_instance:
        execution = service_instance.create_execution(**make_execution_kwargs())
        assert execution.status == ExecutionStatus.SCHEDULED
        connection = service_instance._repository._connection

    # The connection is closed on exit; using it now must raise.
    with pytest.raises(Exception):
        connection.execute("SELECT 1")


# ----------------------------------------------------------------------
# get_or_create_execution (duplicate prevention)
# ----------------------------------------------------------------------


def test_get_or_create_execution_returns_same_execution_for_identical_snapshot(
    service_with_clock: ExecutionService,
) -> None:
    kwargs = make_execution_kwargs()

    first = service_with_clock.get_or_create_execution(**kwargs)
    second = service_with_clock.get_or_create_execution(**kwargs)

    assert first.id == second.id


def test_get_or_create_execution_finds_existing_regardless_of_status(
    service_with_clock: ExecutionService,
) -> None:
    kwargs = make_execution_kwargs()
    created = service_with_clock.get_or_create_execution(**kwargs)
    service_with_clock.start(created.id)  # now in_progress, not scheduled

    found = service_with_clock.get_or_create_execution(**kwargs)

    assert found.id == created.id
    assert found.status == ExecutionStatus.IN_PROGRESS


def test_get_or_create_execution_creates_new_for_different_snapshot(
    service_with_clock: ExecutionService,
) -> None:
    first = service_with_clock.get_or_create_execution(**make_execution_kwargs(task_name="Study Math"))
    second = service_with_clock.get_or_create_execution(**make_execution_kwargs(task_name="Study Physics"))

    assert first.id != second.id


def test_get_or_create_execution_creates_new_when_planned_time_differs(
    service_with_clock: ExecutionService,
) -> None:
    first = service_with_clock.get_or_create_execution(**make_execution_kwargs(planned_start=540, planned_end=660))
    second = service_with_clock.get_or_create_execution(**make_execution_kwargs(planned_start=600, planned_end=720))

    assert first.id != second.id


# ----------------------------------------------------------------------
# reset_all_history
# ----------------------------------------------------------------------


def test_reset_all_history_clears_executions_and_sessions(
    service_with_clock: ExecutionService, repository: ExecutionRepository
) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.start(execution.id)

    deleted_count = service_with_clock.reset_all_history()

    assert deleted_count == 1
    assert service_with_clock.list_executions() == []
    assert repository.list_sessions(execution.id) == []


def test_reset_all_history_on_empty_history_returns_zero(service_with_clock: ExecutionService) -> None:
    assert service_with_clock.reset_all_history() == 0


# ----------------------------------------------------------------------
# list_sessions passthrough
# ----------------------------------------------------------------------


def test_list_sessions_passthrough(service_with_clock: ExecutionService) -> None:
    execution = service_with_clock.create_execution(**make_execution_kwargs())
    service_with_clock.start(execution.id)

    sessions = service_with_clock.list_sessions(execution.id)

    assert len(sessions) == 1
    assert sessions[0].ended_at is None
