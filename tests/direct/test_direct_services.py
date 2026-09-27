"""Direct persistence services (app/persistence): planning, preferences,
generation, executions and productivity round trips over the server schema;
two users; stale versions; rollback of partial writes; sessions closed after
reads and errors; nested transactions; concurrent workers; direct writes in
the change feed API clients read; and a run with every HTTP package
unavailable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.execution.errors import (
    ExecutionDeletedError,
    ExecutionLinkError,
    ExecutionNotFoundError,
    ExecutionVersionConflictError,
    InvalidTransitionError,
)
from app.execution.models import ExecutionStatus
from app.planning import workflow
from app.planning.application import RangeScope
from app.planning.errors import InvalidReferenceError, VersionConflictError
from app.planning.models import FixedBlock, LocalTimeWindow, RecurrenceSpec, Task
from app.planning.preferences import OptimizerMode, PreferenceOverrides, RewardPreferencesOverride
from backend import models
from backend.database import session_factory

ROOT = Path(__file__).resolve().parents[2]
MONDAY = date(2026, 3, 2)


def task(owner, name: str = "Study", **values) -> Task:
    return Task(user_id=owner.user_id, name=name, category=values.pop("category", "study"),
                estimated_duration_minutes=values.pop("minutes", 60), priority=values.pop("priority", 5), **values)


def block(owner, label: str = "Sleep", start: int = 0, end: int = 7) -> FixedBlock:
    return FixedBlock(user_id=owner.user_id, label=label, category="sleep", planned_date=MONDAY, timezone="UTC",
                      planned_start=datetime(2026, 3, 2, start, tzinfo=timezone.utc),
                      planned_end=datetime(2026, 3, 2, end, tzinfo=timezone.utc))


def change_count(engine, user_id) -> int:
    with session_factory(engine)() as session:
        return session.scalar(select(func.count()).select_from(models.ChangeLogEntry)
                              .where(models.ChangeLogEntry.user_id == user_id))


# -----------------------------------------------------------------------------
# Round trips
# -----------------------------------------------------------------------------


def test_planning_preferences_and_generation_round_trip(alice, clock) -> None:
    planning = alice.planning_service()
    first = planning.create_task(task(alice, "Read", tags=["deep", "deep"], preferred_dates=[MONDAY, MONDAY],
                                      preferred_time_window=LocalTimeWindow(start_minute=540, end_minute=720)))
    second = planning.create_task(task(alice, "Write", dependency_ids=[first.id], required_date=MONDAY,
                                       recurrence=None, minutes=30))
    weekly = planning.create_task(task(alice, "Gym", recurrence=RecurrenceSpec(frequency="weekly", weekdays=[4, 0])))
    planning.create_fixed_block(block(alice))
    overrides = PreferenceOverrides(category_multipliers={"study": 2.0, "rest": None}, optimizer_mode=OptimizerMode.ADHD_FRIENDLY,
                                    reward=RewardPreferencesOverride(tag_relations={"deep": []}))
    saved = planning.save_user_preferences(overrides)

    assert planning.get_task(first.id).tags == ["deep", "deep"]
    assert planning.get_task(first.id).preferred_dates == [MONDAY, MONDAY]
    assert planning.get_task(second.id).dependency_ids == [first.id]
    assert planning.get_task(weekly.id).recurrence.weekdays == [0, 4]
    assert planning.user_preferences().overrides == overrides and saved.version == 1
    assert planning.resolve_preferences([MONDAY], "UTC")[MONDAY].optimizer_mode == OptimizerMode.ADHD_FRIENDLY

    outcome = workflow.generate(planning, range_start=MONDAY, range_end=MONDAY, timezone_name="UTC",
                                scope=RangeScope.PLANNED, clock=clock)
    assert outcome.status == "generated"
    placed = {placement.task_id for placement in planning.placements_for_date(MONDAY)}
    assert {first.id, second.id} <= placed
    freshness = workflow.day_freshness(planning, [MONDAY], "UTC")[MONDAY]
    assert freshness.status == workflow.Freshness.CURRENT
    again = workflow.generate(planning, range_start=MONDAY, range_end=MONDAY, timezone_name="UTC",
                              scope=RangeScope.PLANNED, clock=clock)
    assert again.status == "already_current"


def test_execution_lifecycle_and_productivity_round_trip(alice, clock) -> None:
    planning = alice.planning_service()
    planned = planning.create_task(task(alice, "Read", tags=["deep"]))
    workflow.generate(planning, range_start=MONDAY, range_end=MONDAY, timezone_name="UTC", clock=clock)
    placement = planning.placements_for_date(MONDAY)[0]
    executions = alice.execution_service()

    execution = executions.get_or_create_canonical_execution(planned, placement)
    assert executions.get_or_create_canonical_execution(planned, placement).id == execution.id  # reused
    assert (execution.status, execution.version, execution.tag) == (ExecutionStatus.SCHEDULED, 1, "deep")
    clock.advance(hours=1)
    execution = executions.start(execution.id, expected_version=1)
    clock.advance(minutes=25)
    execution = executions.pause(execution.id)
    clock.advance(minutes=5)
    execution = executions.resume(execution.id)
    clock.advance(minutes=20)
    execution = executions.complete(execution.id, expected_version=execution.version)
    assert (execution.status, execution.version, execution.actual_active_duration_minutes) == (
        ExecutionStatus.COMPLETED, 5, 45.0)
    assert [(s.id, s.ended_at is not None) for s in executions.list_sessions(execution.id)] == [(1, True), (2, True)]
    with pytest.raises(InvalidTransitionError):
        executions.start(execution.id)
    execution = executions.record_feedback(execution.id, expected_version=5, focus_rating=4, note="good")
    assert (execution.focus_rating, execution.note, execution.version) == (4, "good", 6)
    assert executions.find_execution_for_placement(placement.id).id == execution.id

    dashboard = alice.productivity_service().build_dashboard()
    assert dashboard.observation_count == 1

    executions.delete_execution(execution.id, expected_version=6)
    with pytest.raises(ExecutionDeletedError):
        executions.get_or_create_canonical_execution(planned, placement)
    with pytest.raises(ExecutionNotFoundError):
        executions.get_execution(execution.id)
    other = executions.create_canonical_execution(planned)
    assert executions.reset_all_history() == 1 and executions.list_executions() == []
    assert other.task_id == planned.id


# -----------------------------------------------------------------------------
# Users, versions, rollback
# -----------------------------------------------------------------------------


def test_two_users_never_see_or_reference_each_other(alice, bob) -> None:
    mine = alice.planning_service().create_task(task(alice, "Alice's"))
    assert bob.planning_service().list_tasks() == [] and bob.planning_service().get_task(mine.id) is None
    with pytest.raises(InvalidReferenceError):
        bob.planning_service().create_task(task(bob, "Bob's", dependency_ids=[mine.id]))
    with pytest.raises(ExecutionLinkError):  # Bob cannot record work on Alice's task
        bob.execution_service().create_canonical_execution(mine)
    with pytest.raises(ExecutionLinkError):
        alice.execution_service().create_canonical_execution(mine, user_id=bob.user_id)
    execution = alice.execution_service().create_canonical_execution(mine)
    with pytest.raises(ExecutionNotFoundError):
        bob.execution_service().start(execution.id)
    assert bob.execution_service().list_executions() == []


def test_stale_versions_are_refused_and_change_nothing(alice, engine) -> None:
    planning = alice.planning_service()
    created = planning.create_task(task(alice, tags=["a"]))
    planning.update_task(created.model_copy(update={"tags": ["b"]}), expected_version=1)
    before = change_count(engine, alice.user_id)
    with pytest.raises(VersionConflictError):
        planning.update_task(created.model_copy(update={"tags": ["stale"]}), expected_version=1)
    assert planning.get_task(created.id).tags == ["b"] and change_count(engine, alice.user_id) == before

    executions = alice.execution_service()
    execution = executions.create_canonical_execution(created)
    executions.start(execution.id, expected_version=1)
    with pytest.raises(ExecutionVersionConflictError):
        executions.pause(execution.id, expected_version=1)
    assert executions.get_execution(execution.id).status == ExecutionStatus.IN_PROGRESS


def test_a_failed_unit_of_work_rolls_back_every_write(alice, engine) -> None:
    planning = alice.planning_service()
    before = change_count(engine, alice.user_id)
    with pytest.raises(RuntimeError):
        with planning.transaction():
            planning.create_task(task(alice, "First"))
            planning.create_fixed_block(block(alice))
            raise RuntimeError("interrupted")
    assert planning.list_tasks() == [] and planning.fixed_blocks_for_date(MONDAY) == []
    assert change_count(engine, alice.user_id) == before


def test_nested_transactions_are_savepoints_of_one_unit_of_work(alice, engine) -> None:
    planning = alice.planning_service()
    with planning.transaction():
        planning.create_task(task(alice, "Kept"))
        with pytest.raises(RuntimeError):
            with planning.transaction():
                planning.create_task(task(alice, "Undone"))
                raise RuntimeError("inner failure")
        planning.create_task(task(alice, "Also kept"))
        assert change_count(engine, alice.user_id) == 0  # nothing committed before the outer block ends
    assert sorted(t.name for t in planning.list_tasks()) == ["Also kept", "Kept"]
    feed = change_count(engine, alice.user_id)
    assert feed == 2  # the rolled-back savepoint left no change-log entry and no gap


def test_sessions_are_closed_after_reads_writes_and_errors(alice, engine) -> None:
    planning, executions = alice.planning_service(), alice.execution_service()
    created = planning.create_task(task(alice))
    planning.list_tasks()
    planning.get_task(created.id)
    with pytest.raises(VersionConflictError):
        planning.update_task(created, expected_version=99)
    with pytest.raises(ExecutionNotFoundError):
        executions.get_execution(str(uuid.uuid4()))
    alice.productivity_service().build_dashboard()
    with planning.transaction():
        planning.list_tasks()
    assert engine.pool.checkedout() == 0  # no Session (and no read transaction) outlives its operation


def test_concurrent_workers_each_use_their_own_session(alice, engine) -> None:
    planning = alice.planning_service()
    failures: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for item in range(5):
                with planning.transaction():
                    planning.create_task(task(alice, f"{index}-{item}"))
                    planning.list_tasks()
        except BaseException as error:  # noqa: BLE001 - reported below
            failures.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert failures == [] and len(planning.list_tasks()) == 20
    with session_factory(engine)() as session:
        seqs = list(session.scalars(select(models.ChangeLogEntry.seq).where(
            models.ChangeLogEntry.user_id == alice.user_id).order_by(models.ChangeLogEntry.seq)))
    assert seqs == list(range(1, 21))  # the per-user lock kept the feed gap-free and ordered
    assert engine.pool.checkedout() == 0


def test_direct_writes_appear_in_the_change_feed_of_api_clients(backend, alice, engine, clock) -> None:
    pytest.importorskip("fastapi", reason="the HTTP API is not installed (direct-mode-only environment)")
    from fastapi.testclient import TestClient

    from backend.app import create_app
    from backend.settings import BackendSettings
    from tests.direct.conftest import PASSWORD

    created = alice.planning_service().create_task(task(alice, "From the desktop", tags=["x"]))
    execution = alice.execution_service().create_canonical_execution(created)
    app = create_app(BackendSettings(database_url="sqlite://", jwt_secret="t" * 40), engine=engine, clock=clock)
    with TestClient(app) as client:
        token = client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).json()
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        feed = client.get("/changes", headers=headers).json()["changes"]
        assert [(c["entity_type"], c["entity_id"], c["version"]) for c in feed] == [
            ("task", str(created.id), 1), ("execution", execution.id, 1)]
        assert feed[0]["record"]["tags"] == ["x"]
        # ...and the API's own write is visible to the direct path.
        client.put(f"/tasks/{created.id}", headers=headers, json={
            **{k: v for k, v in feed[0]["record"].items() if k not in ("id", "version", "created_at", "updated_at",
                                                                          "deleted_at")},
            "name": "Renamed on the web", "base_version": 1})
    assert alice.planning_service().get_task(created.id).name == "Renamed on the web"


# -----------------------------------------------------------------------------
# No HTTP stack
# -----------------------------------------------------------------------------

PROBE = r"""
import json, sys
BLOCKED = {"fastapi", "starlette", "uvicorn", "httpx", "jwt"}
attempts = []
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            attempts.append(name)
            raise ModuleNotFoundError(name, name=name)
sys.meta_path.insert(0, Block())
from datetime import date
from backend.database import create_backend_engine
from backend.migrate import upgrade
from app.persistence.direct import DirectBackend
from app.planning.models import Task
engine = create_backend_engine("sqlite:///" + sys.argv[1])
upgrade(engine)
backend = DirectBackend(engine)
backend.check_schema()
backend.register(email="probe@example.com", password="correct horse battery")
account = backend.sign_in(email="probe@example.com", password="correct horse battery")
planning = account.planning_service()
created = planning.create_task(Task(user_id=account.user_id, name="p", category="c", estimated_duration_minutes=5,
                                    priority=5))
executions = account.execution_service()
execution = executions.start(executions.create_canonical_execution(created).id)
dashboard = account.productivity_service().build_dashboard()
tasks = len(planning.list_tasks())
backend.close()
loaded = sorted(name for name in sys.modules if name.split(".")[0] in BLOCKED or name in ("backend.api", "backend.security"))
print(json.dumps({"attempts": attempts, "loaded": loaded, "status": execution.status.value, "tasks": tasks}))
"""


def test_direct_services_run_with_every_http_package_unavailable(tmp_path) -> None:
    env = {key: value for key, value in os.environ.items() if key not in ("DATABASE_URL", "JWT_SECRET",
                                                                          "TEST_DATABASE_URL")}
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run([sys.executable, "-c", PROBE, (tmp_path / "probe.db").as_posix()], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"attempts": [], "loaded": [], "status": "in_progress", "tasks": 1}


def test_a_thousand_tasks_round_trip_through_the_direct_services_with_bounded_queries(alice, engine) -> None:
    from sqlalchemy import event

    planning = alice.planning_service()
    created = planning.create_tasks([
        task(alice, f"Task {index}", tags=[f"t{index % 5}", "shared", f"t{index % 5}"][: 1 + index % 3],
             preferred_dates=[MONDAY] * (index % 2), minutes=15 + index % 4 * 15)
        for index in range(1000)
    ])
    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        listed = planning.list_tasks()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert {t.id: (t.tags, t.preferred_dates) for t in listed} == {t.id: (t.tags, t.preferred_dates) for t in created}
    # One query for the tasks and one per child kind per 500 tasks -- never one per task (plus a pool pre-ping).
    assert len(statements) <= 2 + 4 * 2
    assert engine.pool.checkedout() == 0
