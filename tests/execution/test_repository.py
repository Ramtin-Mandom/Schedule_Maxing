"""Tests for app/execution/repository.py: CRUD behavior against a real
temporary SQLite database (no mocking of sqlite3 itself)."""

from __future__ import annotations

import sqlite3

import pytest

from app.execution.errors import ExecutionError, ExecutionNotFoundError
from app.execution.models import ExecutionStatus, TaskExecution
from app.execution.repository import ExecutionRepository

TIMESTAMP = "2024-01-01T09:00:00+00:00"


def _make_execution(**overrides: object) -> TaskExecution:
    defaults = dict(
        id="exec-1",
        task_name="Study Math",
        category="study",
        tag="math",
        planned_date=1,
        planned_start=540,
        planned_end=660,
        planned_duration=120,
        priority=8,
        status=ExecutionStatus.SCHEDULED,
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
    )
    defaults.update(overrides)
    return TaskExecution(**defaults)


def test_create_and_get_execution_round_trips(repository: ExecutionRepository) -> None:
    execution = _make_execution()
    repository.create_execution(execution)

    fetched = repository.get_execution("exec-1")
    assert fetched == execution


def test_create_duplicate_id_raises_integrity_error(repository: ExecutionRepository) -> None:
    repository.create_execution(_make_execution())

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_execution(_make_execution())


def test_get_missing_execution_raises_not_found(repository: ExecutionRepository) -> None:
    with pytest.raises(ExecutionNotFoundError):
        repository.get_execution("does-not-exist")


def test_update_missing_execution_raises_not_found(repository: ExecutionRepository) -> None:
    with pytest.raises(ExecutionNotFoundError):
        repository.update_execution(_make_execution(), expected_version=1)


def test_update_execution_persists_changes(repository: ExecutionRepository) -> None:
    repository.create_execution(_make_execution())

    updated = _make_execution(status=ExecutionStatus.IN_PROGRESS, focus_rating=4, version=2)
    repository.update_execution(updated, expected_version=1)

    fetched = repository.get_execution("exec-1")
    assert fetched.status == ExecutionStatus.IN_PROGRESS
    assert fetched.focus_rating == 4


def test_list_executions_filters_by_status(repository: ExecutionRepository) -> None:
    repository.create_execution(_make_execution(id="exec-1", status=ExecutionStatus.SCHEDULED))
    repository.create_execution(_make_execution(id="exec-2", status=ExecutionStatus.COMPLETED))

    scheduled = repository.list_executions(ExecutionStatus.SCHEDULED)
    completed = repository.list_executions(ExecutionStatus.COMPLETED)
    everything = repository.list_executions()

    assert [execution.id for execution in scheduled] == ["exec-1"]
    assert [execution.id for execution in completed] == ["exec-2"]
    assert {execution.id for execution in everything} == {"exec-1", "exec-2"}


def test_create_and_get_open_session(repository: ExecutionRepository) -> None:
    repository.create_execution(_make_execution())

    session = repository.create_session("exec-1", TIMESTAMP)
    assert session.id is not None
    assert session.ended_at is None

    open_session = repository.get_open_session("exec-1")
    assert open_session is not None
    assert open_session.id == session.id


def test_close_session_sets_ended_at(repository: ExecutionRepository) -> None:
    repository.create_execution(_make_execution())
    session = repository.create_session("exec-1", TIMESTAMP)

    closed = repository.close_session(session.id, "2024-01-01T10:00:00+00:00")
    assert closed.ended_at == "2024-01-01T10:00:00+00:00"
    assert repository.get_open_session("exec-1") is None


def test_close_missing_session_raises(repository: ExecutionRepository) -> None:
    with pytest.raises(ExecutionError):
        repository.close_session(999, TIMESTAMP)


def test_close_already_closed_session_raises(repository: ExecutionRepository) -> None:
    repository.create_execution(_make_execution())
    session = repository.create_session("exec-1", TIMESTAMP)
    repository.close_session(session.id, "2024-01-01T10:00:00+00:00")

    with pytest.raises(ExecutionError):
        repository.close_session(session.id, "2024-01-01T11:00:00+00:00")


def test_list_sessions_returns_chronological_order(repository: ExecutionRepository) -> None:
    repository.create_execution(_make_execution())
    first = repository.create_session("exec-1", "2024-01-01T09:00:00+00:00")
    repository.close_session(first.id, "2024-01-01T09:30:00+00:00")
    second = repository.create_session("exec-1", "2024-01-01T10:00:00+00:00")

    sessions = repository.list_sessions("exec-1")
    assert [session.id for session in sessions] == [first.id, second.id]


def test_delete_all_executions_removes_executions_and_sessions(repository: ExecutionRepository) -> None:
    repository.create_execution(_make_execution(id="exec-1"))
    repository.create_execution(_make_execution(id="exec-2"))
    repository.create_session("exec-1", TIMESTAMP)

    deleted_count = repository.delete_all_executions()

    assert deleted_count == 2
    assert repository.list_executions() == []
    assert repository.list_sessions("exec-1") == []


def test_delete_all_executions_on_empty_database_returns_zero(repository: ExecutionRepository) -> None:
    assert repository.delete_all_executions() == 0
