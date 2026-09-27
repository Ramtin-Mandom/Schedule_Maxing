"""The desktop in direct PostgreSQL mode (app/ui/direct_services.py), headless:
the same controllers the pages use, on the disposable database of
tests/direct/conftest.py. Register/sign in/sign out, two accounts and stale
worker results, task and fixed-block CRUD, preferences, the sample CSV,
generation and freshness, the execution lifecycle, restart persistence, safe
failures with no local writes, and an orderly shutdown."""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import exc as sa_exc

from app.persistence.direct import DirectBackend
from app.planning.csv_import import ImportMode
from app.planning.models import Task
from app.planning.preferences import OptimizerMode
from app.ui.background import WorkerRegistry
from app.ui.direct_services import DirectAccountController, open_direct_app_services
from app.ui.schedule_page_controller import SchedulePageController
from config import settings
from tests.direct.conftest import PASSWORD

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "samples" / "inputs" / "valid_single_day_basic.csv"
MON = date(2026, 3, 2)
FORM = {"day": "1", "category": "study", "tag": "t", "fixed": "False", "start_time": "480", "end_time": "1200",
        "duration": "60", "priority": "5"}


@pytest.fixture
def desktop(engine, clock):
    services = open_direct_app_services(backend=DirectBackend(engine, clock=clock), timezone="UTC",
                                        project_root=str(ROOT), registry=WorkerRegistry())
    yield services, DirectAccountController(services)
    services.close()


def sign_in(services, account, email: str, *, register: bool = True) -> None:
    if register:
        assert account.register(email, PASSWORD, display_name=email.split("@")[0]).ok
    result = account.sign_in(email, PASSWORD)
    assert result.ok, result.error
    services.switch_workspace()  # what the app does on the Tk thread after a sign-in


def page(services) -> SchedulePageController:
    return SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=MON, timezone="UTC")


def test_register_sign_in_profile_and_sign_out(desktop) -> None:
    services, account = desktop
    assert account.connection().value.state == "signed_out"
    refused = services.planning_controller.list_tasks()
    assert not refused.ok and "signed out" in refused.error.lower()  # nothing works before signing in

    assert account.register("Ada@Example.com", PASSWORD, display_name="Ada").ok
    duplicate = account.register("ada@example.com", PASSWORD)
    assert not duplicate.ok and "already exists" in duplicate.error
    wrong = account.sign_in("ada@example.com", "not the password")
    unknown = account.sign_in("nobody@example.com", PASSWORD)
    assert not wrong.ok and wrong.error == unknown.error == "The email or password is incorrect."
    invalid = account.sign_in("not-an-email", "")
    assert not invalid.ok and set(invalid.cause.errors) == {"email", "password"}

    sign_in(services, account, "ada@example.com", register=False)
    view = account.connection().value
    assert view.state == "signed_in" and view.signed_in_email == "ada@example.com" and view.backend_url is None
    assert account.profile().value == {"email": "ada@example.com", "display_name": "Ada"}
    assert services.planning_controller.list_tasks().ok
    for unavailable in (account.sync_now(), account.association_preview(), account.configure_backend("x")):
        assert not unavailable.ok and "direct PostgreSQL" in unavailable.error

    assert account.sign_out().ok
    services.switch_workspace()
    assert account.connection().value.state == "signed_out"
    assert not services.planning_controller.list_tasks().ok
    # No password is kept by the services or the controller.
    for holder in (services, account, services.backend):
        assert PASSWORD not in repr(vars(holder))


def test_two_accounts_and_stale_worker_results(desktop) -> None:
    services, account = desktop
    sign_in(services, account, "alice@example.com")
    alice_planning = services.planning_controller
    created = alice_planning.add_or_update_task(Task(name="Alice's", category="c", estimated_duration_minutes=30,
                                                     priority=5)).value
    alice_guard = services.workspace_guard()  # a worker started now, under Alice

    assert account.sign_out().ok
    services.switch_workspace()
    sign_in(services, account, "bob@example.com")
    assert alice_guard() is False  # its result would be dropped, never shown in Bob's pages
    assert services.planning_controller.list_tasks().value == []
    assert services.planning_controller.get_task(created.id).value is None
    late = alice_planning.list_tasks()  # Alice's old controller no longer reaches her records either
    assert not late.ok and "signed out" in late.error.lower()


def test_tasks_blocks_preferences_import_generation_and_executions(desktop) -> None:
    services, account = desktop
    sign_in(services, account, "planner@example.com")
    schedule = page(services)
    for name in ("Read", "Write", "Scratch"):
        assert schedule.submit_task_form({**FORM, "name": name}).ok
    assert schedule.submit_task_form({**FORM, "name": "Sleep", "fixed": "True", "category": "sleep",
                                      "start_time": "0", "end_time": "420"}).ok
    rows = {row.name: row for row in schedule.load().value.rows}
    state = schedule.form_state_for(rows["Read"].ref).value
    assert schedule.submit_task_form({**state.values, "name": "Read (edited)"}, editing=state.ref).ok
    assert schedule.delete(rows["Scratch"].ref).ok
    assert {row.name for row in schedule.load().value.rows} == {"Read (edited)", "Write", "Sleep"}

    planning = services.planning_controller
    assert planning.set_engine_mode(OptimizerMode.ADHD_FRIENDLY).ok
    assert planning.user_preferences().value.overrides.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
    assert planning.set_engine_mode(OptimizerMode.PRECISE_GREEDY).ok

    sample_day = date(2026, 3, 3)
    imported = planning.import_csv_file(str(SAMPLE), anchor_date=sample_day, mode=ImportMode.APPEND)
    assert imported.ok, imported.error
    assert len(planning.get_fixed_blocks(sample_day).value) == 4

    generated = planning.generate(MON, sample_day)
    assert generated.ok, generated.error and generated.value.status == "generated"
    assert planning.generate(MON, sample_day).value.status == "already_current"
    freshness = planning.day_freshness([MON, sample_day]).value
    assert {state.status.value for state in freshness.values()} == {"current"}

    placement = planning.get_placements(MON).value[0]
    executions = services.execution_controller
    task = planning.get_task(placement.task_id).value
    execution = executions.get_or_create_canonical_execution(task, placement).value
    for action in ("start", "pause", "resume", "complete"):
        result = getattr(executions, action)(execution.id)
        assert result.ok, (action, result.error)
    assert executions.get_execution(execution.id).value.status.value == "completed"
    assert services.productivity_controller.build_dashboard().value.observation_count == 1


def test_records_survive_closing_and_reopening(engine, clock) -> None:
    services = open_direct_app_services(backend=DirectBackend(engine, clock=clock), timezone="UTC",
                                        project_root=str(ROOT), registry=WorkerRegistry())
    account = DirectAccountController(services)
    sign_in(services, account, "durable@example.com")
    assert page(services).submit_task_form({**FORM, "name": "Keep me"}).ok
    run = services.planning_controller.generate(MON, MON)
    assert run.ok, run.error
    placement_ids = sorted(str(p.id) for p in services.planning_controller.get_placements(MON).value)
    assert services.close()

    reopened = open_direct_app_services(backend=DirectBackend(engine, clock=clock), timezone="UTC",
                                        project_root=str(ROOT), registry=WorkerRegistry())
    try:
        sign_in(reopened, DirectAccountController(reopened), "durable@example.com", register=False)
        assert [row.name for row in page(reopened).load().value.rows] == ["Keep me"]
        assert sorted(str(p.id) for p in reopened.planning_controller.get_placements(MON).value) == placement_ids
        assert reopened.planning_controller.generate(MON, MON).value.status == "already_current"
    finally:
        reopened.close()


def test_a_database_failure_is_reported_and_nothing_is_written_locally(desktop, monkeypatch) -> None:
    services, account = desktop
    sign_in(services, account, "fails@example.com")

    def unavailable():
        raise sa_exc.OperationalError("SELECT 1", {"password": PASSWORD}, Exception("connection to db.internal failed"))

    class Unreachable:  # the engine the schema check uses fails the same way
        def connect(self):
            unavailable()

    monkeypatch.setattr(services.backend, "_factory", unavailable)
    monkeypatch.setattr(services.backend, "_engine", Unreachable())
    saved = page(services).submit_task_form({**FORM, "name": "Lost"})
    assert not saved.ok and "could not be reached" in saved.error
    assert PASSWORD not in saved.error and "db.internal" not in saved.error and "SELECT" not in saved.error
    loaded = services.planning_controller.list_tasks()
    assert not loaded.ok and "could not be reached" in loaded.error
    status = account.check_backend().value
    assert status.reachable is False and "could not be reached" in status.last_error
    assert not (Path(settings.DATA_DIR) / settings.EXECUTION_DB_FILENAME).exists()  # no silent SQLite fallback

    monkeypatch.undo()
    assert services.planning_controller.list_tasks().value == []  # the failed write really was not saved
    assert account.check_backend().value.reachable is True


def test_a_missing_migration_is_reported_on_sign_in(blank_engine, clock) -> None:
    from backend.migrate import upgrade

    upgrade(blank_engine, "0005")
    services = open_direct_app_services(backend=DirectBackend(blank_engine, clock=clock), timezone="UTC",
                                        registry=WorkerRegistry())
    account = DirectAccountController(services)
    try:
        result = account.sign_in("someone@example.com", PASSWORD)
        assert not result.ok and "schema revision 0005" in result.error and "backend.migrate" in result.error
        assert account.connection().value.headline == "Database unavailable"
    finally:
        services.close()


def test_shutdown_waits_for_workers_before_closing_the_database(desktop) -> None:
    services, account = desktop
    sign_in(services, account, "closing@example.com")
    release, started = threading.Event(), threading.Event()
    registry = services.registry
    assert registry.begin()

    def worker() -> None:
        started.set()
        release.wait(10)
        services.planning_controller.list_tasks()  # still has its database while the app waits
        registry.end()

    thread = threading.Thread(target=worker)
    thread.start()
    started.wait(5)
    assert services.close(timeout=0.2) is False and not services.backend.closed  # not closed under the worker
    release.set()
    thread.join(10)
    assert services.close(timeout=5) is True and services.backend.closed
    assert not services.planning_controller.list_tasks().ok  # after shutdown nothing reaches the database
