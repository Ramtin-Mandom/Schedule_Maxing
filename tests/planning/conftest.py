"""Shared fixtures for persisted-planning tests. Every database used here lives
under pytest's tmp_path, never the real configured data directory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._current = start or datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._current

    def advance(self, minutes: float = 1) -> None:
        self._current += timedelta(minutes=minutes)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "schedule.db"


@pytest.fixture
def connection(db_path: Path):
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def planning_repository(connection) -> PlanningRepository:
    return PlanningRepository(connection)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def planning_service(planning_repository: PlanningRepository, clock: FakeClock) -> PlanningService:
    return PlanningService(planning_repository, clock=clock)


@pytest.fixture
def execution_service(connection) -> ExecutionService:
    """ExecutionService on the same connection as the planning fixtures."""
    return ExecutionService(ExecutionRepository(connection))
