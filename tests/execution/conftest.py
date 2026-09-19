"""Shared fixtures for execution-tracking tests. Every database used here lives under
pytest's tmp_path, never the real configured data directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "executions.db"


@pytest.fixture
def connection(db_path: Path):
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def repository(connection) -> ExecutionRepository:
    return ExecutionRepository(connection)


@pytest.fixture
def service(repository: ExecutionRepository) -> ExecutionService:
    return ExecutionService(repository)


def make_execution_kwargs(**overrides: object) -> dict:
    """Default snapshot fields for ExecutionService.create_execution, with overrides."""
    defaults = dict(
        task_name="Study Math",
        category="study",
        tag="math",
        planned_date=1,
        planned_start=540,
        planned_end=660,
        planned_duration=120,
        priority=8,
    )
    defaults.update(overrides)
    return defaults
