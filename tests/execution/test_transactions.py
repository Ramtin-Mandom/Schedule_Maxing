"""Tests for transaction ownership and composition (app.execution.db.transaction)
and for the atomicity of each logical ExecutionService mutation: a nested
repository call joins the caller's transaction as a savepoint and never
commits it early, an injected failure part-way through an operation rolls
back every earlier step, and a shared connection is safe to use from
background threads.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.execution.db import transaction
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from tests.execution.conftest import make_execution_kwargs


class FakeClock:
    def __init__(self) -> None:
        self._current = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._current

    def advance(self, minutes: float) -> None:
        self._current += timedelta(minutes=minutes)


class InjectedFailure(RuntimeError):
    pass


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def clocked_service(repository: ExecutionRepository, clock: FakeClock) -> ExecutionService:
    return ExecutionService(repository, clock=clock)


def _count(connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# -----------------------------------------------------------------------------
# transaction() composition
# -----------------------------------------------------------------------------


def test_nested_repository_write_does_not_commit_the_outer_transaction(connection, repository, service) -> None:
    with pytest.raises(InjectedFailure):
        with repository.transaction():
            service.create_execution(**make_execution_kwargs())  # its own transaction nests as a savepoint
            assert connection.in_transaction
            raise InjectedFailure

    assert _count(connection, "executions") == 0
    assert not connection.in_transaction


def test_failed_inner_savepoint_rolls_back_only_its_own_writes(connection, service) -> None:
    with transaction(connection):
        kept = service.create_execution(**make_execution_kwargs(task_name="Kept"))
        try:
            with transaction(connection):
                service.create_execution(**make_execution_kwargs(task_name="Discarded"))
                raise InjectedFailure
        except InjectedFailure:
            pass

    assert [execution.id for execution in service.list_executions()] == [kept.id]


def test_outer_commit_happens_once_at_the_end(connection, service) -> None:
    with transaction(connection):
        service.create_execution(**make_execution_kwargs(task_name="A"))
        service.create_execution(**make_execution_kwargs(task_name="B"))
        assert connection.in_transaction
    assert not connection.in_transaction
    assert _count(connection, "executions") == 2


# -----------------------------------------------------------------------------
# Each ExecutionService mutation is atomic
# -----------------------------------------------------------------------------


def test_start_rolls_back_the_status_change_if_opening_the_session_fails(
    connection, repository, clocked_service, monkeypatch
) -> None:
    execution = clocked_service.create_execution(**make_execution_kwargs())

    def fail(*args, **kwargs):
        raise InjectedFailure

    monkeypatch.setattr(repository, "create_session", fail)
    with pytest.raises(InjectedFailure):
        clocked_service.start(execution.id)

    reloaded = repository.get_execution(execution.id)
    assert reloaded.status == ExecutionStatus.SCHEDULED
    assert reloaded.actual_first_start_at is None
    assert reloaded.updated_at == execution.updated_at
    assert _count(connection, "work_sessions") == 0


def test_complete_rolls_back_session_close_and_transition_if_the_final_write_fails(
    connection, repository, clocked_service, clock, monkeypatch
) -> None:
    execution = clocked_service.create_execution(**make_execution_kwargs())
    clocked_service.start(execution.id)
    clock.advance(30)

    original_update = repository.update_execution
    calls = {"count": 0}

    def fail_on_second_update(updated):
        calls["count"] += 1
        if calls["count"] == 2:  # after the transition and the session close
            raise InjectedFailure
        return original_update(updated)

    monkeypatch.setattr(repository, "update_execution", fail_on_second_update)
    with pytest.raises(InjectedFailure):
        clocked_service.complete(execution.id)

    reloaded = repository.get_execution(execution.id)
    assert reloaded.status == ExecutionStatus.IN_PROGRESS
    assert reloaded.actual_active_duration_minutes is None
    assert repository.get_open_session(execution.id) is not None  # the session close was rolled back too

    monkeypatch.undo()
    assert clocked_service.complete(execution.id).actual_active_duration_minutes == 30.0


def test_pause_rolls_back_the_transition_if_closing_the_session_fails(
    repository, clocked_service, monkeypatch
) -> None:
    execution = clocked_service.create_execution(**make_execution_kwargs())
    clocked_service.start(execution.id)

    def fail(*args, **kwargs):
        raise InjectedFailure

    monkeypatch.setattr(repository, "close_session", fail)
    with pytest.raises(InjectedFailure):
        clocked_service.pause(execution.id)

    assert repository.get_execution(execution.id).status == ExecutionStatus.IN_PROGRESS
    assert repository.get_open_session(execution.id) is not None


# -----------------------------------------------------------------------------
# Background-thread access to one shared connection
# -----------------------------------------------------------------------------


def test_concurrent_lifecycles_from_many_threads(connection, service) -> None:
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for attempt in range(10):
                execution = service.create_execution(**make_execution_kwargs(task_name=f"T{index}-{attempt}"))
                service.start(execution.id)
                service.pause(execution.id)
                service.resume(execution.id)
                service.complete(execution.id)
        except BaseException as error:  # noqa: BLE001 - collected and asserted below
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert _count(connection, "executions") == 60
    assert _count(connection, "work_sessions") == 120
    assert len(service.list_executions(ExecutionStatus.COMPLETED)) == 60


def test_other_threads_cannot_interleave_with_an_open_transaction(connection, service) -> None:
    inside = threading.Event()
    release = threading.Event()
    reader_done = threading.Event()
    seen: list[int] = []

    def writer() -> None:
        with transaction(connection):
            service.create_execution(**make_execution_kwargs())
            inside.set()
            release.wait(timeout=10)

    def reader() -> None:
        inside.wait(timeout=10)
        seen.append(len(service.list_executions()))
        reader_done.set()

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    reader_thread.start()

    assert inside.wait(timeout=10)
    # The reader is blocked on the connection lock while the writer's
    # transaction is open -- it can neither see uncommitted rows nor run a
    # statement inside the writer's transaction.
    assert not reader_done.wait(timeout=0.3)
    release.set()
    writer_thread.join(timeout=10)
    reader_thread.join(timeout=10)

    assert seen == [1]
