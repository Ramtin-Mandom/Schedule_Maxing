"""Tests for desktop startup/shutdown (app/ui/app_services.py), the background
worker registry (app/ui/background.py), and restoring execution state for
saved placements across a restart -- all headless, on temporary databases.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import MigrationError
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.ui import background
from app.ui.app_services import describe_startup_failure, open_app_services
from app.ui.background import ControllerResult, WorkerRegistry, run_in_background
from app.ui.schedule_page_controller import SchedulePageController

MON = date(2024, 6, 3)


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


def open_services(db_path: Path, project_root: Path, **kwargs):
    return open_app_services(db_path, timezone="UTC", project_root=str(project_root), **kwargs)


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------


def test_startup_builds_every_controller_on_one_shared_connection(tmp_path: Path) -> None:
    services = open_services(tmp_path / "app.db", tmp_path)
    try:
        assert services.db_path == tmp_path / "app.db"
        assert services.planning_controller.list_tasks().ok
        assert services.execution_controller.list_executions().ok
        assert services.productivity_controller.build_dashboard().ok
        assert background.current_registry() is services.registry
    finally:
        services.close()


def test_startup_loads_saved_data_without_importing_anything(tmp_path: Path) -> None:
    services = open_services(tmp_path / "app.db", tmp_path)
    assert services.planning_controller.list_tasks().value == []  # no sample/CSV rows appear by themselves
    page = SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=MON, timezone="UTC")
    page.submit_task_form({
        "name": "Saved", "day": "1", "category": "study", "tag": "t", "fixed": "False",
        "start_time": "480", "end_time": "600", "duration": "60", "priority": "5",
    })
    services.close()

    reopened = open_services(tmp_path / "app.db", tmp_path)
    try:
        assert [task.name for task in reopened.planning_controller.list_tasks().value] == ["Saved"]
    finally:
        reopened.close()


def test_startup_failure_raises_and_describes_the_problem(tmp_path: Path) -> None:
    unusable = tmp_path / "a_directory.db"
    unusable.mkdir()

    with pytest.raises(sqlite3.OperationalError) as info:
        open_services(unusable, tmp_path)

    message = describe_startup_failure(info.value, unusable)
    assert str(unusable) in message and "could not be opened" in message
    assert "SCHEDULE_MAXING_DATA_DIR" in message


def test_startup_refuses_a_newer_database_and_invalid_timezone(tmp_path: Path) -> None:
    future = tmp_path / "future.db"
    connection = sqlite3.connect(str(future))
    connection.execute("PRAGMA user_version = 42")
    connection.close()
    with pytest.raises(MigrationError):
        open_services(future, tmp_path)

    with pytest.raises(ValueError):
        open_app_services(tmp_path / "ok.db", timezone="Not/AZone")
    assert not (tmp_path / "ok.db").exists()  # validated before anything is opened


# -----------------------------------------------------------------------------
# Shutdown
# -----------------------------------------------------------------------------


def test_close_waits_for_running_workers_then_closes_the_connection(tmp_path: Path) -> None:
    services = open_services(tmp_path / "app.db", tmp_path)
    release = threading.Event()
    finished: list[bool] = []

    def worker() -> None:
        release.wait(timeout=5)
        finished.append(services.planning_controller.list_tasks().ok)  # still usable while running
        services.registry.end()

    assert services.registry.begin()
    threading.Thread(target=worker).start()
    threading.Timer(0.2, release.set).start()

    assert services.close(timeout=5) is True
    assert finished == [True]
    with pytest.raises(sqlite3.ProgrammingError):
        services.connection.execute("SELECT 1")
    assert services.close() is True  # idempotent
    assert services.registry.begin() is False  # no new work after shutdown


class FakeWidget:
    """Duck-typed stand-in for a Tk widget: records scheduled callbacks."""

    def __init__(self) -> None:
        self.exists = True
        self.scheduled: list = []
        self.lock = threading.Lock()

    def after(self, _ms, callback) -> None:
        with self.lock:
            self.scheduled.append(callback)

    def winfo_exists(self) -> bool:
        return self.exists

    def run_pending(self) -> None:
        with self.lock:
            pending, self.scheduled = self.scheduled, []
        for callback in pending:
            callback()


def _wait_idle(registry: WorkerRegistry) -> None:
    deadline = datetime.now() + timedelta(seconds=5)
    while registry.active and datetime.now() < deadline:
        threading.Event().wait(0.01)


def test_results_are_delivered_only_to_existing_widgets_and_not_after_shutdown() -> None:
    registry = WorkerRegistry()
    widget = FakeWidget()
    delivered: list = []

    assert run_in_background(widget, lambda: ControllerResult.success(1), delivered.append, registry=registry)
    _wait_idle(registry)
    widget.exists = False
    widget.run_pending()
    assert delivered == []  # the widget was destroyed before delivery

    widget.exists = True
    run_in_background(widget, lambda: ControllerResult.success(2), delivered.append, registry=registry)
    _wait_idle(registry)
    widget.run_pending()
    assert [result.value for result in delivered] == [2]

    registry.shutdown(timeout=1)
    assert run_in_background(widget, lambda: ControllerResult.success(3), delivered.append, registry=registry) is False


# -----------------------------------------------------------------------------
# Execution state restored by placement identity
# -----------------------------------------------------------------------------


def test_execution_lifecycle_survives_reopen_without_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    services = open_services(db_path, tmp_path)
    page = SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=MON, timezone="UTC")
    for _ in range(2):  # duplicate names on purpose
        page.submit_task_form({
            "name": "Study", "day": "1", "category": "study", "tag": "t", "fixed": "False",
            "start_time": "480", "end_time": "900", "duration": "60", "priority": "5",
        })
    first, second = page.make_schedule().value.snapshot.executables
    executions = services.execution_controller

    assert executions.find_execution_for_placement(first.placement.id).value is None  # viewing creates nothing
    created = executions.get_or_create_canonical_execution(first.task, first.placement).value
    for action in ("start", "pause", "resume"):
        assert getattr(executions, action)(created.id).ok
    services.close()

    services = open_services(db_path, tmp_path)
    try:
        page = SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=MON, timezone="UTC")
        restored_first, restored_second = page.load().value.executables
        executions = services.execution_controller

        restored = executions.find_execution_for_placement(restored_first.placement.id).value
        assert restored.id == created.id and restored.status == ExecutionStatus.IN_PROGRESS
        assert executions.find_execution_for_placement(restored_second.placement.id).value is None
        again = executions.get_or_create_canonical_execution(restored_first.task, restored_first.placement).value
        assert again.id == created.id

        completed = executions.complete(created.id).value
        assert completed.status == ExecutionStatus.COMPLETED
        assert completed.task_id == first.task.id and completed.scheduled_task_id == first.placement.id
        assert len(executions.list_executions().value) == 1
        sessions = ExecutionService(ExecutionRepository(services.connection)).list_sessions(created.id)
        assert len(sessions) == 2 and all(session.ended_at is not None for session in sessions)
        assert completed.actual_first_start_at <= datetime.now(timezone.utc)
    finally:
        services.close()
