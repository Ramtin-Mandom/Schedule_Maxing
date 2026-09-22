"""Shared fixtures for productivity tests. Every database used here lives
under pytest's tmp_path, never the real configured data directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.planning.repository import PlanningRepository
from tests.productivity.fixtures import FakeClock, build_synthetic_dataset


@pytest.fixture
def connection(tmp_path: Path):
    conn = get_connection(tmp_path / "executions.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def repository(connection) -> ExecutionRepository:
    return ExecutionRepository(connection)


@pytest.fixture
def planning_repository(connection) -> PlanningRepository:
    """Planning persistence on the same connection (and lock) as `repository`."""
    return PlanningRepository(connection)


@pytest.fixture
def populated_repository(repository: ExecutionRepository) -> tuple[ExecutionRepository, FakeClock]:
    clock = build_synthetic_dataset(repository)
    return repository, clock
