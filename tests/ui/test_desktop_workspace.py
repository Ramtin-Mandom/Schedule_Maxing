"""The desktop's explicit workspace and process boundaries (docs/desktop-web-boundaries.md),
headless and without any web package:

- every controller works in one owner scope chosen by SyncService.workspace_scope():
  ownerless by default, the device's active (associated) account when there is one --
  never records of several owners mixed in one view, generation, export or reset;
- switch_workspace() builds new controllers; running work keeps its scope and a guarded
  result of earlier work is never delivered to the new workspace;
- one process per database (the local web service is refused while the desktop has it);
- shutdown never closes SQLite under a running transaction;
- an unreachable or invalid backend never blocks local work.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.instance_lock import DatabaseInUseError, InstanceLock
from app.planning.models import FixedBlock, Task
from app.planning.scope import OwnerScope
from app.sync.store import SyncStore
from app.ui import background
from app.ui.app_services import describe_startup_failure, open_app_services
from app.ui.background import ControllerResult, WorkerRegistry, run_in_background
from app.ui.schedule_page_controller import SchedulePageController
from tests.ui.test_app_services import FakeWidget, _wait_idle

MON = date(2024, 6, 3)
ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOB = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
FORM = {"day": "1", "category": "study", "tag": "t", "fixed": "False", "start_time": "480", "end_time": "900",
        "duration": "60", "priority": "5"}


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


def open_services(db_path: Path, project_root: Path, **kwargs):
    return open_app_services(db_path, timezone="UTC", project_root=str(project_root), **kwargs)


def page(services) -> SchedulePageController:
    return SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=MON, timezone="UTC")


def names(services) -> set[str]:
    return {task.name for task in services.planning_controller.list_tasks().value}


def owners(connection, table: str = "tasks") -> set:
    return {row[0] for row in connection.execute(f"SELECT user_id FROM {table}")}


def seed_owned(services, user_id: uuid.UUID, name: str) -> Task:
    """A record of another owner, written device-wide (as sync pulls or an association would leave it)."""
    return services.planning_service.create_task(Task(name=name, category="study", estimated_duration_minutes=30,
                                                      priority=5, user_id=user_id, preferred_dates=[MON]))


def activate(connection, user_id: uuid.UUID) -> None:
    """Make `user_id` the device's active, associated account (what association + a restart leave behind)."""
    store = SyncStore(connection)
    account = store.upsert_account("https://backend.test", str(user_id), "alice@example.com")
    store.mark_associated(account.account_key)
    store.set_active(account.account_key)


# -----------------------------------------------------------------------------
# The workspace rule
# -----------------------------------------------------------------------------


def test_the_default_workspace_is_ownerless_and_never_shows_other_owners(tmp_path: Path) -> None:
    services = open_services(tmp_path / "app.db", tmp_path)
    try:
        assert services.workspace.scope == OwnerScope.ownerless()
        assert page(services).submit_task_form({"name": "Mine", **FORM}).ok
        seed_owned(services, ALICE, "Alice's")  # e.g. left by the local web profile signed in as Alice
        seed_owned(services, BOB, "Bob's")

        assert names(services) == {"Mine"}
        run = page(services).make_schedule()
        assert run.ok, run.error
        assert {row.task.name for row in run.value.snapshot.executables} == {"Mine"}  # only this workspace generated
        export = tmp_path / "export.csv"
        assert services.planning_controller.export_planning_csv(str(export)).ok
        text = export.read_text(encoding="utf-8")
        assert "Mine" in text and "Alice's" not in text and "Bob's" not in text
    finally:
        services.close()


def test_an_active_associated_account_is_the_workspace_after_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    services = open_services(db, tmp_path)
    seed_owned(services, ALICE, "Alice's")
    seed_owned(services, BOB, "Bob's")
    activate(services.connection, ALICE)
    services.close()

    services = open_services(db, tmp_path)  # signed out (tokens live in memory only), still Alice's device
    try:
        assert services.sync_service.signed_in is False
        assert services.workspace.scope == OwnerScope.account(ALICE)
        assert names(services) == {"Alice's"}

        assert page(services).submit_task_form({"name": "Offline edit", **FORM}).ok
        created = next(t for t in services.planning_controller.list_tasks().value if t.name == "Offline edit")
        assert created.user_id == ALICE  # owned by the workspace, so it syncs with Alice's records later

        run = page(services).make_schedule()
        assert run.ok, run.error
        [placement] = [row.placement for row in run.value.snapshot.executables if row.task.id == created.id]
        execution = services.execution_controller.get_or_create_canonical_execution(created, placement)
        assert execution.ok, execution.error
        assert execution.value.user_id == ALICE
        assert owners(services.connection, "executions") == {str(ALICE)}
    finally:
        services.close()


def test_switching_workspace_rebinds_controllers_and_drops_stale_results(tmp_path: Path) -> None:
    services = open_services(tmp_path / "app.db", tmp_path)
    try:
        assert page(services).submit_task_form({"name": "Ownerless", **FORM}).ok
        seed_owned(services, ALICE, "Alice's")
        old_controller = services.planning_controller
        guard = services.workspace_guard()

        registry, widget, delivered = WorkerRegistry(), FakeWidget(), []
        release = threading.Event()

        def slow_list() -> ControllerResult:
            release.wait(timeout=5)
            return old_controller.list_tasks()

        assert run_in_background(widget, slow_list, delivered.append, registry=registry, still_current=guard)
        workspace = services.switch_workspace(OwnerScope.account(ALICE))
        release.set()
        _wait_idle(registry)
        widget.run_pending()

        assert workspace.epoch == 1 and not guard()
        assert delivered == []  # the ownerless result never reaches the account's view
        assert services.planning_controller is not old_controller
        assert names(services) == {"Alice's"}
        assert {task.name for task in old_controller.list_tasks().value} == {"Ownerless"}  # old work keeps its scope

        assert page(services).submit_task_form({"name": "New for Alice", **FORM}).ok
        assert owners(services.connection) == {None, str(ALICE)}
        assert names(services) == {"Alice's", "New for Alice"}

        # With nobody selected or active, the default rule brings the ownerless workspace back.
        assert services.switch_workspace().scope == OwnerScope.ownerless()
        assert names(services) == {"Ownerless"}
    finally:
        services.close()


# -----------------------------------------------------------------------------
# One process per database
# -----------------------------------------------------------------------------


def test_a_second_process_on_the_same_database_is_refused_until_the_first_closes(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    services = open_services(db, tmp_path)
    try:
        with pytest.raises(DatabaseInUseError) as refused:
            InstanceLock(db, "the local web service").acquire()
        assert "the desktop app" in str(refused.value)
        with pytest.raises(DatabaseInUseError) as second:
            open_services(db, tmp_path)
        message = describe_startup_failure(second.value, db)
        assert "already open in another Schedule Maxing process" in message
        assert names(services) == set()  # the first process is unaffected
    finally:
        services.close()

    lock = InstanceLock(db, "the local web service").acquire()  # released by close()
    lock.release()
    open_services(db, tmp_path).close()


def test_in_memory_databases_need_no_lock(tmp_path: Path) -> None:
    first = open_app_services(":memory:", timezone="UTC", project_root=str(tmp_path))
    second = open_app_services(":memory:", timezone="UTC", project_root=str(tmp_path))
    first.close()
    second.close()


# -----------------------------------------------------------------------------
# Shutdown and offline behavior
# -----------------------------------------------------------------------------


def test_close_never_closes_sqlite_under_a_running_transaction(tmp_path: Path) -> None:
    services = open_services(tmp_path / "app.db", tmp_path)
    inside, release, committed = threading.Event(), threading.Event(), []

    def long_transaction() -> None:
        with services.planning_service.transaction():
            services.planning_service.create_task(Task(name="Late", category="study", estimated_duration_minutes=30,
                                                       priority=5))
            inside.set()
            release.wait(timeout=10)
        committed.append(True)

    thread = threading.Thread(target=long_transaction)
    thread.start()
    assert inside.wait(timeout=5)

    assert services.close(timeout=0.2) is False  # refused: the transaction is still running
    assert services.closed is False
    with pytest.raises(DatabaseInUseError):
        InstanceLock(tmp_path / "app.db", "the local web service").acquire()  # still held

    release.set()
    thread.join(timeout=5)
    assert committed == [True]  # the transaction finished on an open connection
    assert services.close(timeout=5) is True  # called again once the transaction ended: now it closes
    assert services.closed is True

    reopened = open_services(tmp_path / "app.db", tmp_path)
    try:
        assert names(reopened) == {"Late"}
    finally:
        reopened.close()


@pytest.mark.parametrize("backend_url", ["http://127.0.0.1:9", "not a url"])
def test_an_unreachable_or_invalid_backend_never_blocks_local_work(tmp_path: Path, backend_url: str) -> None:
    services = open_services(tmp_path / "app.db", tmp_path, backend_url=backend_url)
    try:
        controller = page(services)
        assert controller.submit_task_form({"name": "Offline", **FORM}).ok
        block_start = datetime(2024, 6, 3, 0, tzinfo=timezone.utc)
        block = FixedBlock(label="Sleep", category="sleep", planned_date=MON, timezone="UTC",
                           planned_start=block_start, planned_end=block_start + timedelta(hours=7))
        assert services.planning_controller.save_fixed_block(block).ok
        run = controller.make_schedule()
        assert run.ok, run.error
        assert run.value.placed_count == 1

        report = services.sync_service.sync_now()  # not signed in: inert, whatever the backend does
        assert report.status == "inert"
        status = services.sync_service.status()
        assert status.configured is (backend_url.startswith("http")) and status.signed_in is False
    finally:
        assert services.close(timeout=5) is True
