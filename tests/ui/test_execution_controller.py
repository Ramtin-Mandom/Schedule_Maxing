"""Tests for app/ui/execution_controller.py: duplicate prevention, action
gating, live elapsed time, and structured (non-raising) error handling.

None of these instantiate Tk/CustomTkinter -- pure controller/service logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.ui.execution_controller import ExecutionController
from tests.ui.conftest import make_snapshot_kwargs


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def __call__(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc))


@pytest.fixture
def controller_with_clock(repository: ExecutionRepository, clock: FakeClock) -> ExecutionController:
    return ExecutionController(ExecutionService(repository, clock=clock))


# ----------------------------------------------------------------------
# Duplicate-record prevention
# ----------------------------------------------------------------------


def test_get_or_create_execution_prevents_duplicates_on_repeated_selection(
    execution_controller: ExecutionController,
) -> None:
    kwargs = make_snapshot_kwargs()

    first = execution_controller.get_or_create_execution(**kwargs)
    second = execution_controller.get_or_create_execution(**kwargs)

    assert first.ok and second.ok
    assert first.value.id == second.value.id


def test_get_or_create_execution_survives_a_schedule_refresh_simulation(
    execution_controller: ExecutionController,
) -> None:
    # Simulates the UI re-selecting "the same" scheduled task after Make Schedule
    # regenerates the schedule output fresh: same snapshot fields, called again later.
    kwargs = make_snapshot_kwargs()
    created = execution_controller.get_or_create_execution(**kwargs)
    execution_controller.start(created.value.id)

    reopened = execution_controller.get_or_create_execution(**kwargs)

    assert reopened.value.id == created.value.id
    assert reopened.value.status == ExecutionStatus.IN_PROGRESS


# ----------------------------------------------------------------------
# available_actions
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ExecutionStatus.SCHEDULED, ("start", "skip")),
        (ExecutionStatus.IN_PROGRESS, ("pause", "complete", "skip")),
        (ExecutionStatus.PAUSED, ("resume", "complete", "skip")),
        (ExecutionStatus.COMPLETED, ()),
        (ExecutionStatus.SKIPPED, ()),
    ],
)
def test_available_actions_per_status(
    execution_controller: ExecutionController, status: ExecutionStatus, expected: tuple[str, ...]
) -> None:
    assert execution_controller.available_actions(status) == expected


# ----------------------------------------------------------------------
# Invalid actions: structured error, never an exception
# ----------------------------------------------------------------------


def test_invalid_action_returns_error_result_not_exception(execution_controller: ExecutionController) -> None:
    created = execution_controller.get_or_create_execution(**make_snapshot_kwargs())

    # scheduled -> pause is invalid.
    result = execution_controller.pause(created.value.id)

    assert result.ok is False
    assert result.value is None
    assert isinstance(result.error, str) and len(result.error) > 0
    assert "scheduled" in result.error.lower() and "paused" in result.error.lower()


def test_action_on_missing_execution_returns_error_result(execution_controller: ExecutionController) -> None:
    result = execution_controller.start("does-not-exist")

    assert result.ok is False
    assert result.error is not None


# ----------------------------------------------------------------------
# Live elapsed time (paused time excluded)
# ----------------------------------------------------------------------


def test_elapsed_active_minutes_excludes_paused_time(
    controller_with_clock: ExecutionController, clock: FakeClock
) -> None:
    created = controller_with_clock.get_or_create_execution(**make_snapshot_kwargs())
    execution_id = created.value.id

    controller_with_clock.start(execution_id)
    clock.advance(timedelta(minutes=10))  # 10 minutes of real work

    elapsed = controller_with_clock.elapsed_active_minutes(execution_id, now=clock())
    assert elapsed.value == pytest.approx(10.0)

    controller_with_clock.pause(execution_id)
    clock.advance(timedelta(hours=2))  # a 2-hour paused gap -- must not count

    elapsed_while_paused = controller_with_clock.elapsed_active_minutes(execution_id, now=clock())
    assert elapsed_while_paused.value == pytest.approx(10.0)  # unchanged while paused

    controller_with_clock.resume(execution_id)
    clock.advance(timedelta(minutes=5))  # 5 more minutes of real work

    final_elapsed = controller_with_clock.elapsed_active_minutes(execution_id, now=clock())
    assert final_elapsed.value == pytest.approx(15.0)


def test_elapsed_active_minutes_for_never_started_execution_is_zero(
    execution_controller: ExecutionController,
) -> None:
    created = execution_controller.get_or_create_execution(**make_snapshot_kwargs())

    elapsed = execution_controller.elapsed_active_minutes(created.value.id)

    assert elapsed.ok
    assert elapsed.value == 0.0
