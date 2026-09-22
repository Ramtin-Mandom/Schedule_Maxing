"""Shared builders for the core scheduling domain (Task, FixedBlock, DaySchedule,
ScheduledTask). Used by tests/test_constraints.py, test_pert.py, test_reward.py,
test_optimizer.py, and test_data_processor.py so each test only sets the fields
it actually cares about.
"""

from __future__ import annotations

import pytest

from app.models import DaySchedule, FixedBlock, ScheduledTask, Task, TimeWindow
from config import settings


@pytest.fixture(autouse=True)
def _isolate_default_data_location(tmp_path_factory, monkeypatch):
    """
    Safety net: any code path that opens the *default* database location
    (no explicit path) during tests gets a throwaway directory instead of the
    real per-user data folder or the checkout's legacy data/ folder.
    """
    isolated = tmp_path_factory.mktemp("default_data_dir")
    monkeypatch.setattr(settings, "DATA_DIR", isolated / "data")
    monkeypatch.setattr(settings, "LEGACY_DATA_DIR", isolated / "legacy_repo_data")
    monkeypatch.setattr(settings, "DATA_DIR_OVERRIDDEN", False)


@pytest.fixture
def make_time_window():
    def _make(start: int, end: int) -> TimeWindow:
        return TimeWindow(start_time=start, end_time=end)

    return _make


@pytest.fixture
def make_task():
    def _make(
        name: str = "Task",
        *,
        date: int = 1,
        category: str = "study",
        tag: str = "",
        duration: int = 60,
        priority: int = 5,
        preference_start: int = 0,
        preference_end: int = 1440,
        dependencies: list[str] | None = None,
        fixed: bool = False,
    ) -> Task:
        return Task(
            name=name,
            date=date,
            category=category,
            tag=tag,
            fixed=fixed,
            duration=duration,
            priority=priority,
            preference_time=TimeWindow(
                start_time=preference_start,
                end_time=preference_end,
            ),
            dependencies=dependencies or [],
        )

    return _make


@pytest.fixture
def make_fixed_block():
    def _make(
        name: str = "Fixed",
        *,
        start: int,
        end: int,
        category: str = "fixed",
    ) -> FixedBlock:
        return FixedBlock(
            name=name,
            time_window=TimeWindow(start_time=start, end_time=end),
            category=category,
        )

    return _make


@pytest.fixture
def make_day_schedule():
    def _make(
        *,
        day_start: int = 0,
        day_end: int = 1440,
        fixed_blocks: list[FixedBlock] | None = None,
        tasks: list[Task] | None = None,
    ) -> DaySchedule:
        return DaySchedule(
            time_window=TimeWindow(start_time=day_start, end_time=day_end),
            fixed_blocks=fixed_blocks or [],
            tasks=tasks or [],
        )

    return _make


@pytest.fixture
def make_scheduled_task():
    def _make(
        name: str = "Scheduled",
        *,
        start: int,
        end: int,
        category: str = "study",
        tag: str = "",
        score: float = 0.0,
    ) -> ScheduledTask:
        return ScheduledTask(
            name=name,
            category=category,
            tag=tag,
            time_window=TimeWindow(start_time=start, end_time=end),
            score=score,
        )

    return _make
