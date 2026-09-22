"""
ml_fixtures.py

A second, deterministic execution-history fixture, additive to
tests/productivity/fixtures.py (which is never modified by this module).

tests/productivity/fixtures.py's build_synthetic_dataset produces whole
category "blocks" one after another in a single chronological run (all 8
study/morning rows, then all 3 study/evening rows, and so on) -- a
chronological train/test split on that dataset would put entire categories
into train-only or test-only. build_interleaved_dataset instead
round-robins across category/time-bucket combinations with strictly
increasing created_at, so a chronological cut lands across every category,
which is what real usage (and a realistic split) looks like.

No randomness anywhere: every duration is derived from a fixed formula, so
this fixture is exactly reproducible run to run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from tests.productivity.fixtures import FakeClock

# (category, tag, planned_start) -- planned_start picks a distinct time
# bucket per category (morning / evening / afternoon) via
# app.productivity.buckets.time_bucket_for_minutes.
_CATEGORY_ROTATION = [
    ("study", "math", 540),  # 09:00 -> morning
    ("exercise", "cardio", 1080),  # 18:00 -> evening
    ("errand", "car", 780),  # 13:00 -> afternoon
]

_BASE_OFFSET_MINUTES = {"study": 5, "exercise": -3, "errand": 8}


def build_interleaved_dataset(repository: ExecutionRepository, *, count: int = 60) -> FakeClock:
    """
    Populate `repository` with `count` completed executions, round-robining
    across three category/time-bucket combinations, each with a strictly
    increasing created_at, via the real ExecutionService API.

    Duration formula (deterministic, no RNG):
        actual_minutes = planned_duration(60) + base_offset(category) + (index % 5)
    -- a small, fixed, category-specific bias plus a bounded, repeating
    residual, giving a linear model some learnable per-category signal
    without inventing implausible outcomes (durations stay in a normal
    45-75 minute range for a 60-minute planned task).
    """
    clock = FakeClock(datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc))
    service = ExecutionService(repository, clock=clock)
    planned_duration = 60

    for index in range(count):
        category, tag, planned_start = _CATEGORY_ROTATION[index % len(_CATEGORY_ROTATION)]
        actual_minutes = planned_duration + _BASE_OFFSET_MINUTES[category] + (index % 5)

        execution = service.create_execution(
            task_name=f"{category}-{tag}-{index}",
            category=category,
            tag=tag,
            planned_date=1,
            planned_start=planned_start,
            planned_end=planned_start + planned_duration,
            planned_duration=planned_duration,
            priority=5,
        )
        service.start(execution.id)
        clock.advance(timedelta(minutes=actual_minutes))
        service.complete(execution.id)
        clock.advance(timedelta(hours=2))

    return clock
