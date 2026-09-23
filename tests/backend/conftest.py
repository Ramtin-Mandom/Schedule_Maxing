"""Shared fixtures for backend tests.

Every test gets its own in-memory SQLite database, created by the real
Alembic migrations, and an injectable server clock. These tests exercise
the API and the shared mutation path on SQLite by default. With
BACKEND_TESTS_ON_POSTGRES=1 and a disposable TEST_DATABASE_URL, the same
tests run on PostgreSQL (one private schema per test). PostgreSQL-specific
behavior is covered by tests/backend/test_postgres.py (marked `postgres`,
skipped unless TEST_DATABASE_URL names a disposable database).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import make_url, text

from backend.app import create_app
from backend.database import create_backend_engine
from backend.migrate import upgrade
from backend.settings import BackendSettings, normalize_database_url

TEST_SECRET = "unit-test-secret-" + "x" * 32
PASSWORD = "correct horse battery"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def settings() -> BackendSettings:
    return BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET)


def _postgres_url() -> str | None:
    """
    With BACKEND_TESTS_ON_POSTGRES=1, every backend test runs on the disposable
    PostgreSQL database in TEST_DATABASE_URL instead of SQLite (same guard as
    test_postgres.py: the database name must contain "test").
    """
    if os.environ.get("BACKEND_TESTS_ON_POSTGRES") != "1":
        return None
    url = normalize_database_url(os.environ.get("TEST_DATABASE_URL", "").strip())
    parsed = make_url(url) if url else None
    if parsed is None or parsed.get_backend_name() != "postgresql" or "test" not in (parsed.database or "").lower():
        pytest.exit("BACKEND_TESTS_ON_POSTGRES=1 needs a PostgreSQL TEST_DATABASE_URL whose database name contains 'test'.")
    return url


@pytest.fixture
def engine():
    url = _postgres_url()
    if url is None:
        engine = create_backend_engine("sqlite://")
        upgrade(engine)
        try:
            yield engine
        finally:
            engine.dispose()
        return

    schema = f"sm_test_{uuid.uuid4().hex[:12]}"  # a private schema per test, dropped afterwards
    admin = create_backend_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_backend_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        upgrade(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(settings, engine, clock):
    return create_app(settings, engine=engine, clock=clock)


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, email: str, password: str = PASSWORD, **extra) -> dict:
    response = client.post("/auth/register", json={"email": email, "password": password, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def login_headers(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def account(client: TestClient, email: str) -> dict[str, str]:
    register(client, email)
    return login_headers(client, email)


@pytest.fixture
def alice(client) -> dict[str, str]:
    return account(client, "alice@example.com")


@pytest.fixture
def bob(client) -> dict[str, str]:
    return account(client, "bob@example.com")


# -----------------------------------------------------------------------------
# Record payloads
# -----------------------------------------------------------------------------


def task_payload(**overrides) -> dict:
    return {"name": "Study", "category": "study", "estimated_duration_minutes": 60, "priority": 5, **overrides}


def block_payload(**overrides) -> dict:
    return {"label": "Sleep", "category": "sleep", "planned_date": "2026-03-02", "timezone": "UTC",
            "planned_start": "2026-03-02T00:00:00Z", "planned_end": "2026-03-02T07:00:00Z", **overrides}


def placement_payload(task_id: str, **overrides) -> dict:
    return {"task_id": task_id, "planned_date": "2026-03-02", "timezone": "UTC",
            "planned_start": "2026-03-02T09:00:00Z", "planned_end": "2026-03-02T10:00:00Z", "score": 3.5,
            "optimization_metadata": {"mode": "precise"}, **overrides}


def preference_payload(**overrides) -> dict:
    return {"scope": "user", "overrides": {"optimizer_mode": "adhd_friendly",
                                           "category_multipliers": {"study": 2.0, "work": None}}, **overrides}


def generation_payload(**overrides) -> dict:
    return {"planned_date": "2026-03-02", "timezone": "UTC", "engine_mode": "precise_greedy",
            "range_start": "2026-03-02", "range_end": "2026-03-08", "range_scope": "planned",
            "allocation_id": "11111111-1111-4111-8111-111111111111", "fingerprint": "f" * 64,
            "fingerprint_version": 1, "placements_digest": "d" * 64, "placement_count": 1,
            "unscheduled_count": 0, "total_score": 3.5, "generated_at": "2026-03-02T11:00:00Z", **overrides}


def execution_payload(task_id: str | None = None, placement_id: str | None = None, **overrides) -> dict:
    return {"task_id": task_id, "scheduled_task_id": placement_id, "task_name": "Study", "category": "study",
            "tag": "math", "planned_duration": 60, "priority": 5, **overrides}


def create(client: TestClient, headers: dict, path: str, payload: dict) -> dict:
    response = client.post(f"/{path}", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def seed(client: TestClient, headers: dict) -> dict[str, dict]:
    """One live record of every resource type for the given account."""
    project = create(client, headers, "projects", {"name": "Thesis"})
    task = create(client, headers, "tasks", task_payload(project_id=project["id"]))
    placement = create(client, headers, "placements", placement_payload(task["id"]))
    return {
        "projects": project,
        "tasks": task,
        "fixed-blocks": create(client, headers, "fixed-blocks", block_payload()),
        "placements": placement,
        "preferences": create(client, headers, "preferences", preference_payload()),
        "schedule-generations": create(client, headers, "schedule-generations", generation_payload()),
        "executions": create(client, headers, "executions", execution_payload(task["id"], placement["id"])),
    }


#: Server-owned metadata a client never sends back.
META = ("id", "version", "created_at", "updated_at", "deleted_at")


def editable(record: dict, **changes) -> dict:
    """A PUT body from a returned record: its content, the changes, and its version as the precondition."""
    return {**{k: v for k, v in record.items() if k not in META}, **changes, "base_version": record["version"]}
