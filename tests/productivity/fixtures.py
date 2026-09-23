"""
fixtures.py

A deterministic, synthetic execution-history builder used by every test in
tests/productivity/, and by the pre-completion demo run. It drives the real
app.execution.service.ExecutionService through its public API (create,
start, pause, resume, complete, skip, record_feedback) with a controllable
FakeClock -- it never inserts rows by hand -- so the resulting data is a
faithful exercise of the Milestone 1 <-> Milestone 2 boundary, not a
shortcut around it.

This data is entirely synthetic and is only ever written to a temporary
database path supplied by the caller (a pytest tmp_path fixture, or an
explicit throwaway path for the demo run). It is never written to the
application's configured DATA_DIR.

Dataset shape (numbers chosen to be small enough to read by eye, using
small ProductivityThresholds in tests rather than requiring hundreds of
rows):
    - "study" tasks in the morning bucket: 8 completed (tightly clustered
      durations, some with focus/energy feedback), 2 skipped after starting,
      1 skipped before starting -- enough for at least LOW/MODERATE evidence
      with small test thresholds.
    - "study" tasks in the evening bucket: 3 completed -- deliberately thin,
      for testing the "time bucket" and "category" fallback levels.
    - "exercise" tasks in the evening bucket: 6 completed, each running
      noticeably longer than planned (for the duration-variance insight), 1
      left paused, 1 left scheduled (never started).
    - "errand" tasks in the afternoon bucket: 2 completed only -- thin
      enough to be INSUFFICIENT evidence at the category level, to exercise
      the "not enough history" insight.
    - one deliberately implausible completed "study" duration (> 1440
      minutes), to exercise the plausibility filter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService


class FakeClock:
    """A controllable clock: returns the same instant until explicitly advanced."""

    def __init__(self, start: datetime) -> None:
        self._current = start

    def __call__(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


def build_synthetic_dataset(repository: ExecutionRepository) -> FakeClock:
    """
    Populate `repository` with the fixture dataset described above via the
    real ExecutionService API. Returns the FakeClock used, in case a caller
    wants to keep driving time forward afterward.
    """
    clock = FakeClock(datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc))  # a Monday
    service = ExecutionService(repository, clock=clock)

    # -- "study" tasks, morning bucket (planned_start 540 = 09:00) --
    study_actuals = [58, 63, 65, 60, 70, 55, 68, 62]
    for index, actual_minutes in enumerate(study_actuals):
        focus = 4 if index % 2 == 0 else None
        energy = 3 if index % 3 == 0 else None
        _complete(
            service,
            clock,
            category="study",
            tag="math",
            planned_start=540,
            planned_duration=60,
            priority=8,
            active_minutes=actual_minutes,
            focus_rating=focus,
            energy_rating=energy,
        )
        clock.advance(timedelta(hours=2))

    _skip_after_start(
        service, clock, category="study", tag="math", planned_start=540, planned_duration=60,
        priority=7, worked_minutes=10,
    )
    clock.advance(timedelta(hours=1))
    _skip_after_start(
        service, clock, category="study", tag="reading", planned_start=570, planned_duration=45,
        priority=6, worked_minutes=5,
    )
    clock.advance(timedelta(hours=1))
    _skip_before_start(
        service, category="study", tag="reading", planned_start=600, planned_duration=30, priority=5,
    )

    # Deliberately implausible completed duration (> 1440 minutes / one day).
    _complete(
        service, clock, category="study", tag="math", planned_start=540, planned_duration=60,
        priority=9, active_minutes=1500,
    )

    clock.advance(timedelta(days=1))

    # -- "study" tasks, evening bucket (planned_start 1200 = 20:00): thin on purpose --
    for actual_minutes in (40, 50, 45):
        _complete(
            service, clock, category="study", tag="review", planned_start=1200, planned_duration=45,
            priority=6, active_minutes=actual_minutes,
        )
        clock.advance(timedelta(hours=3))

    clock.advance(timedelta(days=1))

    # -- "exercise" tasks, evening bucket (planned_start 1080 = 18:00), consistently longer than planned --
    exercise_actuals = [46, 50, 47, 49, 52, 48]  # planned 30 each -> ~+18 median
    for index, actual_minutes in enumerate(exercise_actuals):
        focus = 5 if index % 2 == 0 else None
        _complete(
            service, clock, category="exercise", tag="cardio", planned_start=1080, planned_duration=30,
            priority=5, active_minutes=actual_minutes, focus_rating=focus, energy_rating=4,
        )
        clock.advance(timedelta(hours=4))

    _leave_paused(
        service, clock, category="exercise", tag="cardio", planned_start=1080, planned_duration=30,
        priority=4, worked_minutes=15,
    )
    _leave_scheduled(service, category="exercise", tag="strength", planned_start=1080, planned_duration=45, priority=3)

    clock.advance(timedelta(days=1))

    # -- "errand" tasks, afternoon bucket (planned_start 780 = 13:00): thin on purpose --
    for actual_minutes in (25, 35):
        _complete(
            service, clock, category="errand", tag="car", planned_start=780, planned_duration=30,
            priority=4, active_minutes=actual_minutes,
        )
        clock.advance(timedelta(hours=5))

    return clock


def _complete(
    service: ExecutionService,
    clock: FakeClock,
    *,
    category: str,
    tag: str,
    planned_start: int,
    planned_duration: int,
    priority: int,
    active_minutes: int,
    focus_rating: int | None = None,
    energy_rating: int | None = None,
    interruption_count: int | None = None,
    planned_date: int = 1,
):
    execution = service.create_execution(
        task_name=f"{category}-{tag}-{active_minutes}",
        category=category,
        tag=tag,
        planned_date=planned_date,
        planned_start=planned_start,
        planned_end=planned_start + planned_duration,
        planned_duration=planned_duration,
        priority=priority,
    )
    service.start(execution.id)
    clock.advance(timedelta(minutes=active_minutes))
    completed = service.complete(execution.id)

    if focus_rating is not None or energy_rating is not None or interruption_count is not None:
        completed = service.record_feedback(
            execution.id,
            expected_version=completed.version,
            focus_rating=focus_rating,
            energy_rating=energy_rating,
            interruption_count=interruption_count,
        )

    return completed


def _skip_before_start(
    service: ExecutionService,
    *,
    category: str,
    tag: str,
    planned_start: int,
    planned_duration: int,
    priority: int,
    planned_date: int = 1,
):
    execution = service.create_execution(
        task_name=f"{category}-{tag}-skip-before",
        category=category,
        tag=tag,
        planned_date=planned_date,
        planned_start=planned_start,
        planned_end=planned_start + planned_duration,
        planned_duration=planned_duration,
        priority=priority,
    )
    return service.skip(execution.id)


def _skip_after_start(
    service: ExecutionService,
    clock: FakeClock,
    *,
    category: str,
    tag: str,
    planned_start: int,
    planned_duration: int,
    priority: int,
    worked_minutes: int,
    planned_date: int = 1,
):
    execution = service.create_execution(
        task_name=f"{category}-{tag}-skip-after",
        category=category,
        tag=tag,
        planned_date=planned_date,
        planned_start=planned_start,
        planned_end=planned_start + planned_duration,
        planned_duration=planned_duration,
        priority=priority,
    )
    service.start(execution.id)
    clock.advance(timedelta(minutes=worked_minutes))
    return service.skip(execution.id)


def _leave_paused(
    service: ExecutionService,
    clock: FakeClock,
    *,
    category: str,
    tag: str,
    planned_start: int,
    planned_duration: int,
    priority: int,
    worked_minutes: int,
    planned_date: int = 1,
):
    execution = service.create_execution(
        task_name=f"{category}-{tag}-paused",
        category=category,
        tag=tag,
        planned_date=planned_date,
        planned_start=planned_start,
        planned_end=planned_start + planned_duration,
        planned_duration=planned_duration,
        priority=priority,
    )
    service.start(execution.id)
    clock.advance(timedelta(minutes=worked_minutes))
    return service.pause(execution.id)


def _leave_scheduled(
    service: ExecutionService,
    *,
    category: str,
    tag: str,
    planned_start: int,
    planned_duration: int,
    priority: int,
    planned_date: int = 1,
):
    return service.create_execution(
        task_name=f"{category}-{tag}-scheduled",
        category=category,
        tag=tag,
        planned_date=planned_date,
        planned_start=planned_start,
        planned_end=planned_start + planned_duration,
        planned_duration=planned_duration,
        priority=priority,
    )
