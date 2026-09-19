"""Shared fixtures for app/ui controller tests. Every database used here lives
under pytest's tmp_path, never the real configured data directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.productivity.reporting import ProductivityService
from app.productivity.stats import ProductivityThresholds
from app.ui.execution_controller import ExecutionController
from app.ui.productivity_controller import ProductivityController


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
def execution_service(repository: ExecutionRepository) -> ExecutionService:
    return ExecutionService(repository)


@pytest.fixture
def execution_controller(execution_service: ExecutionService) -> ExecutionController:
    return ExecutionController(execution_service)


@pytest.fixture
def productivity_service(repository: ExecutionRepository) -> ProductivityService:
    return ProductivityService(repository, thresholds=ProductivityThresholds(low=3, moderate=6, high=10))


@pytest.fixture
def productivity_controller(
    productivity_service: ProductivityService, execution_controller: ExecutionController
) -> ProductivityController:
    return ProductivityController(productivity_service, execution_controller)


def make_snapshot_kwargs(**overrides: object) -> dict:
    defaults = dict(
        task_name="Study Math",
        category="study",
        tag="math",
        planned_date=1,
        planned_start=540,
        planned_end=600,
        planned_duration=60,
        priority=8,
    )
    defaults.update(overrides)
    return defaults
