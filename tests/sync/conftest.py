"""Fixtures for synchronization tests: an in-process backend (FastAPI test
client over its own database), injectable transports, and "devices" -- each
a separate temporary SQLite database with the real desktop services and a
SyncService, which can be closed and reopened to simulate a restart.

The backend database is in-memory SQLite by default; with
BACKEND_TESTS_ON_POSTGRES=1 and a disposable TEST_DATABASE_URL it is
PostgreSQL (the same switch as tests/backend)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.models import FixedBlock, Task
from app.planning.repository import PlanningRepository
from app.sync.service import SyncService
from app.sync.transport import (
    PullPage,
    TransportError,
    _classify,
    login_via,
    pull_via,
)
from app.ui.planning_controller import PlanningController
from backend.app import create_app
from backend.database import create_backend_engine
from backend.migrate import upgrade
from backend.settings import BackendSettings
from tests.backend.conftest import TEST_SECRET, _postgres_url

PASSWORD = "correct horse battery"
MON = date(2026, 3, 2)


class InProcessTransport:
    """SyncTransport over the FastAPI test client, classifying errors exactly like HttpTransport."""

    base_url = "http://backend.test"

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.pushed: list[list[dict]] = []

    def _request(self, method: str, path: str, token: str | None, body: dict | None) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise _classify(response.status_code, response.json())
        return response.json()

    def login(self, email: str, password: str):
        return login_via(self._request, email, password)

    def push(self, token: str, operations: list[dict]) -> list[dict]:
        self.pushed.append(operations)
        return self._request("POST", "/sync/push", token, {"operations": operations})["results"]

    def pull(self, token: str, after: int, limit: int) -> PullPage:
        return pull_via(self._request, token, after, limit)


class FlakyTransport(InProcessTransport):
    """Injects failures: fail before sending, or lose the response after the server applied the request."""

    def __init__(self, client: TestClient) -> None:
        super().__init__(client)
        self.fail_push = 0
        self.lose_push_response = 0
        self.fail_pull = 0
        self.during_push = None  # callable run while the request is "in flight"

    def push(self, token: str, operations: list[dict]) -> list[dict]:
        if self.fail_push:
            self.fail_push -= 1
            raise TransportError("connection refused (injected)")
        results = super().push(token, operations)
        if self.during_push is not None:
            hook, self.during_push = self.during_push, None
            hook()
        if self.lose_push_response:
            self.lose_push_response -= 1
            raise TransportError("timed out waiting for the response (injected)")
        return results

    def pull(self, token: str, after: int, limit: int) -> PullPage:
        if self.fail_pull:
            self.fail_pull -= 1
            raise TransportError("connection reset (injected)")
        return super().pull(token, after, limit)


class Server:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.app = create_app(BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET), engine=engine)
        self.client = TestClient(self.app)

    def register(self, email: str) -> dict:
        response = self.client.post("/auth/register", json={"email": email, "password": PASSWORD})
        assert response.status_code == 201, response.text
        return response.json()

    def headers(self, email: str) -> dict:
        token = self.client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def get(self, email: str, path: str, **params) -> dict:
        return self.client.get(path, params=params, headers=self.headers(email)).json()

    def changes(self, email: str) -> list[dict]:
        return self.get(email, "/changes", limit=500)["changes"]


@pytest.fixture
def server():
    url = _postgres_url()
    schema = None
    if url is None:
        engine = create_backend_engine("sqlite://")
    else:
        schema = f"sm_test_{uuid.uuid4().hex[:12]}"
        admin = create_backend_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_backend_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    upgrade(engine)
    instance = Server(engine)
    try:
        yield instance
    finally:
        instance.client.close()
        engine.dispose()
        if schema is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


class Device:
    """One desktop installation: its own SQLite file and services. reopen() simulates a restart."""

    def __init__(self, tmp_path: Path, name: str, transport, project_root: Path) -> None:
        self.path = tmp_path / f"{name}.db"
        self.transport = transport
        self.project_root = project_root
        self._open()

    def _open(self) -> None:
        self.connection = get_connection(self.path)
        self.planning = PlanningService(PlanningRepository(self.connection))
        self.controller = PlanningController(service=self.planning, timezone="UTC", project_root=str(self.project_root))
        self.executions = ExecutionService(ExecutionRepository(self.connection))
        self.sync = SyncService(self.connection, self.transport, backoff_base=1.0, backoff_max=8.0)

    def reopen(self, *, keep_session: bool = True) -> None:
        token, key = self.sync._token, self.sync._account_key
        self.sync.stop()
        self.connection.close()
        self._open()
        if keep_session:  # the in-memory token survives only because the test says so
            self.sync._token, self.sync._account_key = token, key

    def sign_in(self, email: str, *, associate: bool = True):
        account = self.sync.sign_in(email, PASSWORD)
        if associate:
            self.sync.associate_local_data()
        return account

    def sync_now(self):
        report = self.sync.sync_now()
        return report

    def add_task(self, name: str = "Study", **fields) -> Task:
        return self.planning.create_task(Task(name=name, category="study", estimated_duration_minutes=60, priority=5,
                                              **fields))

    def add_block(self, day: date = MON, hour: int = 0, **fields) -> FixedBlock:
        start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
        return self.planning.create_fixed_block(FixedBlock(**{
            "label": "Sleep", "category": "sleep", "planned_date": day, "timezone": "UTC", "planned_start": start,
            "planned_end": start + timedelta(hours=7), **fields,
        }))

    def dirty(self) -> list[tuple]:
        return self.sync._engine.store.dirty()

    def close(self) -> None:
        self.sync.stop()
        self.connection.close()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root  # no config/: the built-in reward defaults, identical on every device


@pytest.fixture
def make_device(tmp_path, project_root):
    devices: list[Device] = []

    def make(name: str, transport) -> Device:
        device = Device(tmp_path, name, transport, project_root)
        devices.append(device)
        return device

    yield make
    for device in devices:
        try:
            device.close()
        except Exception:  # noqa: BLE001 - already closed by the test
            pass


@pytest.fixture
def alice_server(server):
    server.register("alice@example.com")
    return server

