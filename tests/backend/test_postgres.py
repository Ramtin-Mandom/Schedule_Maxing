"""PostgreSQL verification (optional): the real production database engine.

Run with a *disposable* database (see docs/backend.md):

    TEST_DATABASE_URL=postgresql://user@localhost:5432/schedule_maxing_test python -m pytest -m postgres

Safety: skipped unless TEST_DATABASE_URL is set; refused unless the
database name contains "test". Every run works inside its own freshly
created schema (search_path), which is dropped afterwards -- nothing else in
the database is touched. These tests cover what SQLite cannot establish:
PostgreSQL types and partial/composite constraints, concurrent
registrations, and change-log ordering under concurrent transactions.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import func, make_url, select, text
from sqlalchemy.exc import IntegrityError

from backend import models
from backend.app import create_app
from backend.database import create_backend_engine, session_factory
from backend.migrate import current_revision, head_revision, upgrade
from backend.mutations import mutation
from backend.resources import PROJECTS, ProjectCreate
from backend.settings import BackendSettings, normalize_database_url
from tests.backend.conftest import TEST_SECRET, login_headers, register

pytestmark = pytest.mark.postgres

RAW_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
if not RAW_URL:
    pytest.skip("TEST_DATABASE_URL is not set; PostgreSQL tests are optional.", allow_module_level=True)
URL = normalize_database_url(RAW_URL)
if make_url(URL).get_backend_name() != "postgresql" or "test" not in (make_url(URL).database or "").lower():
    pytest.skip("Refusing TEST_DATABASE_URL: it must be a PostgreSQL database whose name contains 'test'.",
                allow_module_level=True)


@pytest.fixture(scope="module")
def pg_engine():
    schema = f"sm_test_{uuid.uuid4().hex[:12]}"
    admin = create_backend_engine(URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_backend_engine(URL, connect_args={"options": f"-csearch_path={schema}"}, pool_size=20)
    try:
        upgrade(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def pg_app(pg_engine):
    return create_app(BackendSettings(database_url=URL, jwt_secret=TEST_SECRET), engine=pg_engine)


def _user(engine) -> uuid.UUID:
    now = datetime.now(timezone.utc)
    user_id = uuid.uuid4()
    with session_factory(engine)() as session:
        session.add(models.User(id=user_id, email=f"{user_id}@example.com", password_hash="x", change_seq=0,
                                created_at=now, updated_at=now, version=1))
        session.commit()
    return user_id


def _column_types(connection, table: str) -> dict[str, str]:
    return dict(connection.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :table"
    ), {"table": table}).all())


def test_concurrent_refresh_has_one_winner_and_replay_revokes_it(pg_engine):
    """Two workers cannot turn one credential into two live descendants."""
    from concurrent.futures import ThreadPoolExecutor
    from tests.backend.conftest import PASSWORD

    settings = BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET)
    app = create_app(settings, engine=pg_engine)
    with TestClient(app) as client:
        register(client, "refresh-race@example.com")
        pair = client.post("/auth/login", json={"email": "refresh-race@example.com", "password": PASSWORD}).json()
    barrier = threading.Barrier(2)

    def rotate():
        with TestClient(app) as client:
            barrier.wait(timeout=10)
            response = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: rotate(), range(2)))
    assert sorted(status for status, _ in outcomes) == [200, 401]
    winner = next(body for status, body in outcomes if status == 200)
    with TestClient(app) as client:
        assert client.get("/auth/me", headers={"Authorization": "Bearer " + winner["access_token"]}).status_code == 401
        assert client.post("/auth/refresh", json={"refresh_token": winner["refresh_token"]}).status_code == 401
    with session_factory(pg_engine)() as session:
        assert session.scalar(select(func.count()).select_from(models.RefreshCredential)) == 2
        assert session.scalar(select(models.NativeSession.revoked_at)) is not None


def test_migrations_match_the_models_on_postgresql(pg_engine) -> None:
    with pg_engine.connect() as connection:
        assert current_revision(connection) == head_revision()
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, models.Base.metadata) == []
        tasks = _column_types(connection, "tasks")
        tags = _column_types(connection, "task_tags")
        dates = _column_types(connection, "task_preferred_dates")
        preferences = _column_types(connection, "preferences")
        change_log = _column_types(connection, "change_log")
        sync_operations = _column_types(connection, "sync_operations")
        json_columns = set(connection.execute(text(
            "SELECT table_name || '.' || column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND data_type IN ('json', 'jsonb')"
        )).scalars())
    # Native types, and tags/preferred dates/recurrence as ordered child rows and scalar columns instead of JSONB.
    assert tasks["id"] == "uuid" and tasks["created_at"] == "timestamp with time zone"
    assert not {"tags", "preferred_dates", "recurrence"} & set(tasks)
    assert tasks["recurrence_end_date"] == "date" and tasks["recurrence_interval"] == "integer"
    assert tags == {"user_id": "uuid", "task_id": "uuid", "position": "integer", "tag": "text"}
    assert dates["preferred_date"] == "date"
    assert "overrides" not in preferences and preferences["reward_weight_importance"] == "double precision"
    assert "payload" not in change_log and change_log["revision_id"] == "uuid"
    assert "result" not in sync_operations and sync_operations["record_revision_id"] == "uuid"
    # The only JSON left is the documented, bounded placement extension object (live and in snapshots).
    assert json_columns == {"placements.optimization_metadata", "placement_revisions.optimization_metadata"}


def test_postgresql_enforces_the_partial_and_composite_constraints(pg_engine) -> None:
    alice, bob = _user(pg_engine), _user(pg_engine)
    now = datetime.now(timezone.utc)
    factory = session_factory(pg_engine)

    def preference(user_id, deleted=None):
        return models.Preference(user_id=user_id, id=uuid.uuid4(), scope="user", scope_date=None, scope_key="user",
                                 optimizer_mode=None, created_at=now, updated_at=now, version=1,
                                 deleted_at=deleted)

    with factory() as session:
        session.add_all([preference(alice, deleted=now), preference(alice)])  # a tombstone and one live layer
        session.commit()
        session.add(preference(alice))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        project = models.Project(user_id=alice, id=uuid.uuid4(), name="A", created_at=now, updated_at=now, version=1)
        session.add(project)
        session.commit()
        forged = models.Task(user_id=bob, id=uuid.uuid4(), project_id=project.id, name="B", category="c",
                             estimated_duration_minutes=5, priority=5, required=False,
                             created_at=now, updated_at=now, version=1)
        session.add(forged)
        with pytest.raises(IntegrityError):  # the composite foreign key includes user_id
            session.commit()


def test_postgresql_backstops_the_placement_extension_object(pg_engine) -> None:
    """Any writer (not only the API) is held to a JSON object of bounded size in optimization_metadata."""
    owner, now = _user(pg_engine), datetime.now(timezone.utc)
    task_id = uuid.uuid4()
    factory = session_factory(pg_engine)
    with factory() as session:
        session.add(models.Task(user_id=owner, id=task_id, name="T", category="c", estimated_duration_minutes=5,
                                priority=5, required=False, created_at=now, updated_at=now, version=1))
        session.commit()

    def placement(metadata) -> models.Placement:
        return models.Placement(user_id=owner, id=uuid.uuid4(), task_id=task_id, planned_date=now.date(),
                                timezone="UTC", planned_start=now, planned_end=now.replace(year=now.year + 1),
                                score=0.0, optimization_metadata=metadata, created_at=now, updated_at=now, version=1)

    for metadata in ([{"task": 1}], {"blob": "x" * 9000}):
        with factory() as session:
            session.add(placement(metadata))
            with pytest.raises(IntegrityError):
                session.commit()
    with factory() as session:
        session.add(placement({"mode": "precise"}))
        session.commit()


def test_concurrent_registrations_create_exactly_one_account(pg_app) -> None:
    results: list[int] = []

    def attempt() -> None:
        with TestClient(pg_app) as client:
            results.append(client.post("/auth/register", json={
                "email": "race@example.com", "password": "correct horse battery"}).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(results) == [201] + [409] * 7


def test_concurrent_mutations_get_gap_free_ordered_sequence_numbers(pg_app, pg_engine) -> None:
    with TestClient(pg_app) as client:
        register(client, "busy@example.com")
        headers = login_headers(client, "busy@example.com")

    def writer(index: int) -> None:
        with TestClient(pg_app) as client:
            for item in range(5):
                assert client.post("/projects", json={"name": f"{index}-{item}"}, headers=headers).status_code == 201

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    with TestClient(pg_app) as client:
        feed = client.get("/changes", params={"limit": 500}, headers=headers).json()["changes"]
    assert [change["seq"] for change in feed] == list(range(1, 41))
    assert len({change["entity_id"] for change in feed}) == 40


def test_a_later_cursor_can_never_skip_an_earlier_uncommitted_change(pg_engine) -> None:
    """
    Transaction A allocates a sequence number and stays open. Transaction B
    (same user) must wait for A's commit before it can allocate, so no reader
    can ever observe B's number without A's.
    """
    user_id = _user(pg_engine)
    factory = session_factory(pg_engine)
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    b_finished = threading.Event()

    def committed_seqs() -> list[int]:
        with factory() as reader:
            return list(reader.scalars(select(models.ChangeLogEntry.seq)
                                       .where(models.ChangeLogEntry.user_id == user_id)
                                       .order_by(models.ChangeLogEntry.seq)))

    def transaction_b() -> None:
        with factory() as session, mutation(session, user_id, clock) as mutator:
            mutator.create(PROJECTS, ProjectCreate(name="B"))
        b_finished.set()

    session_a = factory()
    context_a = mutation(session_a, user_id, clock)
    mutator_a = context_a.__enter__()
    mutator_a.create(PROJECTS, ProjectCreate(name="A"))  # seq 1 allocated, not committed

    thread = threading.Thread(target=transaction_b)
    thread.start()
    time.sleep(1.0)
    assert not b_finished.is_set()  # B is blocked on A's lock
    assert committed_seqs() == []  # a reader sees neither

    context_a.__exit__(None, None, None)  # commit A
    session_a.close()
    thread.join(timeout=30)
    assert b_finished.is_set()
    with factory() as reader:
        names = list(reader.scalars(select(models.ProjectRevision.name)
                                    .join(models.ChangeLogEntry, (models.ChangeLogEntry.user_id == models.ProjectRevision.user_id)
                                          & (models.ChangeLogEntry.revision_id == models.ProjectRevision.id))
                                    .where(models.ChangeLogEntry.user_id == user_id)
                                    .order_by(models.ChangeLogEntry.seq)))
    assert committed_seqs() == [1, 2] and names == ["A", "B"]


def test_concurrent_overlapping_fixed_blocks_cannot_both_be_created(pg_app) -> None:
    """The overlap check runs under the user's change-log row lock, so racing writers serialize on it."""
    with TestClient(pg_app) as client:
        register(client, "blocks@example.com")
        headers = login_headers(client, "blocks@example.com")
    results: list[int] = []

    def attempt(index: int) -> None:
        with TestClient(pg_app) as client:
            results.append(client.post("/fixed-blocks", headers=headers, json={
                "label": f"Block {index}", "planned_date": "2026-03-02", "timezone": "UTC",
                "planned_start": f"2026-03-02T09:{index:02d}:00Z", "planned_end": "2026-03-02T11:00:00Z",
            }).status_code)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(results) == [201] + [409] * 7
    with TestClient(pg_app) as client:
        assert len(client.get("/fixed-blocks", headers=headers).json()["items"]) == 1


def test_the_query_plans_can_use_the_indexes_the_direct_path_relies_on(pg_engine) -> None:
    """
    Index applicability, not planner choice. For each query shape the services
    issue: (1) an index whose leading columns are exactly the query's equality
    columns exists (inspected in pg_indexes), and (2) with sequential scans
    disabled for the session, the planner answers the query from an index
    rather than a table scan. Which of several usable indexes it picks for an
    empty table is its business, so no plan is required to name one index, and
    nothing depends on sample sizes or timings.
    """
    user = uuid.uuid4()
    queries = {  # index -> (its leading columns, the query shape)
        "ix_executions_user_task": ("(user_id, task_id)",
                                    "SELECT id FROM executions WHERE user_id = :u AND task_id = ANY(:ids)"),
        "uq_placements_user_task_id": ("(user_id, task_id, id)",
                                       "SELECT id FROM placements WHERE user_id = :u AND task_id = ANY(:ids)"),
        "task_tags_pkey": ('(user_id, task_id, "position")',
                           "SELECT tag FROM task_tags WHERE user_id = :u AND task_id = ANY(:ids) ORDER BY position"),
        "change_log_pkey": ("(user_id, seq)",
                            "SELECT seq FROM change_log WHERE user_id = :u AND seq > 0 ORDER BY seq LIMIT 100"),
        "uq_preferences_live_scope": ("(user_id, scope_key) WHERE (deleted_at IS NULL)",
                                      "SELECT id FROM preferences WHERE user_id = :u AND scope_key = 'user' "
                                      "AND deleted_at IS NULL"),
    }
    with pg_engine.connect() as connection:
        definitions = dict(connection.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")).all())
        connection.execute(text("SET enable_seqscan = off"))
        for index, (columns, query) in queries.items():
            assert index in definitions and columns in definitions[index], (index, definitions.get(index))
            rows = connection.execute(text("EXPLAIN " + query), {"u": user, "ids": [uuid.uuid4()]}).scalars()
            plan = " | ".join(rows)
            assert "Index" in plan and "Seq Scan" not in plan, (index, plan)


# -----------------------------------------------------------------------------
# Execution lifecycle and explicit rescheduling under concurrency (Milestone 5)
# -----------------------------------------------------------------------------


def _planned_work(client, headers) -> tuple[dict, dict, dict]:
    """A task, its placement on 2026-03-02 09:00-10:00 and the placement's scheduled execution."""
    task = client.post("/tasks", headers=headers, json={"name": "Study", "category": "study",
                                                         "estimated_duration_minutes": 60, "priority": 5}).json()
    placement = client.post("/placements", headers=headers, json={
        "task_id": task["id"], "planned_date": "2026-03-02", "timezone": "UTC",
        "planned_start": "2026-03-02T09:00:00Z", "planned_end": "2026-03-02T10:00:00Z"}).json()
    execution = client.post("/executions", headers=headers, json={
        "task_id": task["id"], "scheduled_task_id": placement["id"], "task_name": "Study", "category": "study",
        "planned_duration": 60, "priority": 5}).json()
    return task, placement, execution


def _race(target, count: int = 8) -> None:
    threads = [threading.Thread(target=target, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)


def test_concurrent_moves_of_one_placement_have_exactly_one_winner(pg_app) -> None:
    with TestClient(pg_app) as client:
        register(client, "mover@example.com")
        headers = login_headers(client, "mover@example.com")
        task, placement, execution = _planned_work(client, headers)
    results: list[tuple[int, str | None]] = []

    def attempt(index: int) -> None:
        with TestClient(pg_app) as client:
            response = client.post(f"/planning/placements/{placement['id']}/reschedule", headers=headers, json={
                "base_version": placement["version"], "planned_date": "2026-03-02", "timezone": "UTC",
                "planned_start": f"2026-03-02T{12 + index % 6:02d}:00:00Z",
                "planned_end": f"2026-03-02T{13 + index % 6:02d}:00:00Z"})
            results.append((response.status_code, response.json().get("error", {}).get("code")))

    _race(attempt)
    assert sorted(status for status, _ in results) == [200] + [409] * 7
    assert {code for status, code in results if status == 409} <= {"deleted", "version_conflict"}
    with TestClient(pg_app) as client:
        live = [p for p in client.get("/placements", headers=headers).json()["items"] if p["task_id"] == task["id"]]
        assert len(live) == 1  # never two replacements
        assert client.get(f"/executions/{execution['id']}", headers=headers).json()["version"] == 2  # cancelled once
        feed = client.get("/changes", params={"limit": 500}, headers=headers).json()["changes"]
    assert [change["seq"] for change in feed] == list(range(1, len(feed) + 1))


def test_concurrent_starts_of_one_execution_open_one_session(pg_app) -> None:
    with TestClient(pg_app) as client:
        register(client, "starter@example.com")
        headers = login_headers(client, "starter@example.com")
        _, _, execution = _planned_work(client, headers)
    statuses: list[int] = []

    def attempt(_index: int) -> None:
        with TestClient(pg_app) as client:
            statuses.append(client.post(f"/executions/{execution['id']}/actions/start", headers=headers,
                                        json={"base_version": 1}).status_code)

    _race(attempt)
    assert sorted(statuses) == [200] + [409] * 7
    with TestClient(pg_app) as client:
        stored = client.get(f"/executions/{execution['id']}", headers=headers).json()
    assert stored["status"] == "in_progress" and len(stored["sessions"]) == 1 and stored["version"] == 2


def test_a_move_that_fails_part_way_rolls_back_on_postgresql(pg_app, pg_engine, monkeypatch) -> None:
    from backend.planning_repository import ServerPlanningRepository

    with TestClient(pg_app) as client:
        register(client, "rollback@example.com")
        headers = login_headers(client, "rollback@example.com")
        task, placement, execution = _planned_work(client, headers)
        feed_before = client.get("/changes", params={"limit": 500}, headers=headers).json()["changes"]

    def fail(*_args, **_kwargs):
        raise RuntimeError("connection lost (injected)")  # after the tombstone and the replacement were written

    monkeypatch.setattr(ServerPlanningRepository, "cancel_unstarted_execution", fail)
    with TestClient(pg_app, raise_server_exceptions=False) as client:
        response = client.post(f"/planning/placements/{placement['id']}/reschedule", headers=headers, json={
            "base_version": placement["version"], "planned_date": "2026-03-02", "timezone": "UTC",
            "planned_start": "2026-03-02T14:00:00Z", "planned_end": "2026-03-02T15:00:00Z"})
        assert response.status_code == 500
    monkeypatch.undo()

    with TestClient(pg_app) as client:
        assert client.get(f"/placements/{placement['id']}", headers=headers).json() == placement
        assert [p["id"] for p in client.get("/placements", headers=headers).json()["items"]
                if p["task_id"] == task["id"]] == [placement["id"]]
        assert client.get(f"/executions/{execution['id']}", headers=headers).json() == execution
        assert client.get("/changes", params={"limit": 500}, headers=headers).json()["changes"] == feed_before


def _counts(engine, user_email: str) -> dict:
    with session_factory(engine)() as session:
        user_id = session.scalar(select(models.User.id).where(models.User.email == user_email))
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model)
                                                    .where(model.user_id == user_id))
                for model in (models.Placement, models.Execution, models.WorkSession, models.ChangeLogEntry,
                              models.SyncOperation)}


def test_competing_finish_skip_and_cancel_have_exactly_one_winner(pg_app, pg_engine) -> None:
    with TestClient(pg_app) as client:
        register(client, "ends@example.com")
        headers = login_headers(client, "ends@example.com")
        _, _, execution = _planned_work(client, headers)
        assert client.post(f"/executions/{execution['id']}/actions/start", headers=headers,
                           json={"base_version": 1}).status_code == 200
    before = _counts(pg_engine, "ends@example.com")
    outcomes: dict[str, int] = {}

    def attempt(index: int) -> None:
        action = ("complete", "skip", "cancel")[index % 3]
        with TestClient(pg_app) as client:
            status = client.post(f"/executions/{execution['id']}/actions/{action}", headers=headers,
                                 json={"base_version": 2}).status_code
            outcomes[f"{action}-{index}"] = status

    _race(attempt, 9)
    assert sorted(outcomes.values()) == [200] + [409] * 8
    with TestClient(pg_app) as client:
        stored = client.get(f"/executions/{execution['id']}", headers=headers).json()
    winner = next(key.split("-")[0] for key, status in outcomes.items() if status == 200)
    assert stored["status"] == {"complete": "completed", "skip": "skipped", "cancel": "cancelled"}[winner]
    assert stored["version"] == 3 and len(stored["sessions"]) == 1 and stored["sessions"][0]["ended_at"]
    after = _counts(pg_engine, "ends@example.com")
    assert after["change_log"] == before["change_log"] + 1 and after["work_sessions"] == before["work_sessions"]


def test_a_move_racing_a_start_never_moves_started_work(pg_app, pg_engine) -> None:
    with TestClient(pg_app) as client:
        register(client, "move-race@example.com")
        headers = login_headers(client, "move-race@example.com")
        task, placement, execution = _planned_work(client, headers)
    results: dict[str, int] = {}

    def attempt(index: int) -> None:
        with TestClient(pg_app) as client:
            if index == 0:
                results["move"] = client.post(f"/planning/placements/{placement['id']}/reschedule", headers=headers, json={
                    "base_version": placement["version"], "planned_date": "2026-03-02", "timezone": "UTC",
                    "planned_start": "2026-03-02T14:00:00Z", "planned_end": "2026-03-02T15:00:00Z"}).status_code
            else:
                results["start"] = client.post(f"/executions/{execution['id']}/actions/start", headers=headers,
                                               json={"base_version": 1}).status_code

    _race(attempt, 2)
    assert sorted(results.values()) == [200, 409]
    with TestClient(pg_app) as client:
        stored = client.get(f"/executions/{execution['id']}", headers=headers).json()
        live = [p for p in client.get("/placements", headers=headers).json()["items"] if p["task_id"] == task["id"]]
    if results["start"] == 200:  # the start won: the placement stays, the work is in progress
        assert stored["status"] == "in_progress" and [p["id"] for p in live] == [placement["id"]]
    else:  # the move won: the untouched attempt was cancelled with the move
        assert stored["status"] == "cancelled" and stored["sessions"] == [] and live[0]["id"] != placement["id"]


def test_reusing_a_reschedule_op_id_for_another_move_changes_nothing(pg_app, pg_engine) -> None:
    with TestClient(pg_app) as client:
        register(client, "reuse@example.com")
        headers = login_headers(client, "reuse@example.com")
        _, placement, _ = _planned_work(client, headers)
        op = {"op_id": str(uuid.uuid4()), "entity_type": "placement", "entity_id": placement["id"], "kind": "action",
              "action": "reschedule", "base_version": placement["version"], "payload": {
                  "replacement_id": str(uuid.uuid4()), "planned_date": "2026-03-02", "timezone": "UTC",
                  "planned_start": "2026-03-02T14:00:00Z", "planned_end": "2026-03-02T15:00:00Z"}}
        first = client.post("/sync/push", json={"operations": [op]}, headers=headers).json()["results"][0]
        assert first["status"] == "applied" and len(first["related"]) == 2
        applied = _counts(pg_engine, "reuse@example.com")
        assert client.post("/sync/push", json={"operations": [op]}, headers=headers).json()["results"][0] == first
        other = {**op, "payload": {**op["payload"], "planned_start": "2026-03-02T16:00:00Z",
                                   "planned_end": "2026-03-02T17:00:00Z"}}
        reused = client.post("/sync/push", json={"operations": [other]}, headers=headers).json()["results"][0]
        assert reused["status"] == "rejected" and reused["error"]["code"] == "op_id_reused"
    assert _counts(pg_engine, "reuse@example.com") == applied  # no placement, execution, session or change entry
