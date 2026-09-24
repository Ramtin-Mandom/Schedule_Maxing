"""Fixtures for the local web profile: an in-process backend, transports that
reach it (optionally failing), and "local services" -- a local FastAPI app
over its own temporary SQLite database, with a browser-like client that has
completed the bootstrap exchange. restart() simulates stopping and starting
the local service on the same database."""

from __future__ import annotations

import gc
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.web.local_app import LocalWebConfig, create_local_app
from tests.sync.conftest import PASSWORD, FlakyTransport, InProcessTransport, Server
from tests.sync.conftest import server as server  # noqa: F401 - the in-process backend fixture

BACKEND_URL = "http://backend.test"


@pytest.fixture(autouse=True)
def _collect_tk_garbage_on_this_thread():
    """Earlier desktop (Tk) tests may leave objects whose finalizers must run on the main thread, not in a
    TestClient's event-loop thread (tkinter blocks there); collect them here first."""
    gc.collect()


class Local:
    """One local service and the browser that uses it."""

    def __init__(self, db_path: Path, transport, *, backend_url: str | None = BACKEND_URL, static_dir=None) -> None:
        self.db_path = db_path
        self.transport = transport
        self.backend_url = backend_url
        self.static_dir = static_dir
        self._start()

    def _factory(self, url: str):
        if not url.startswith(("http://", "https://")):
            raise ValueError("the backend URL must start with https:// (or http:// for a local server)")
        self.transport.base_url = url
        return self.transport

    def _start(self) -> None:
        self.config = LocalWebConfig(db_path=self.db_path, timezone="UTC", backend_url=self.backend_url,
                                     extra_hosts=("testserver",), background_sync=False, static_dir=self.static_dir)
        self.app = create_local_app(self.config, transport_factory=self._factory)
        self.client = TestClient(self.app, base_url="http://testserver")
        self.client.__enter__()
        response = self.client.post("/local/session", json={"bootstrap_code": self.config.bootstrap_code})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]

    @property
    def runtime(self):
        return self.app.state.runtime

    def restart(self) -> None:
        self.stop()
        self._start()

    def stop(self) -> None:
        self.client.__exit__(None, None, None)

    # -- requests ---------------------------------------------------------------------

    def get(self, path: str, **params):
        return self.client.get(path, params=params)

    def post(self, path: str, json=None, **kwargs):
        return self.client.post(path, json=json, headers={"X-CSRF-Token": self.csrf}, **kwargs)

    def put(self, path: str, json=None):
        return self.client.put(path, json=json, headers={"X-CSRF-Token": self.csrf})

    def patch(self, path: str, json=None):
        return self.client.patch(path, json=json, headers={"X-CSRF-Token": self.csrf})

    def delete(self, path: str, **params):
        return self.client.delete(path, params=params, headers={"X-CSRF-Token": self.csrf})

    def ok(self, response, status: int = 200) -> dict:
        assert response.status_code == status, response.text
        return response.json() if response.content else {}

    def sign_in(self, email: str) -> dict:
        return self.ok(self.post("/local/account/sign-in", {"email": email, "password": PASSWORD}))

    def create_task(self, name: str = "Study", **fields) -> dict:
        body = {"name": name, "category": "study", "estimated_duration_minutes": 60, "priority": 5, **fields}
        return self.ok(self.post("/tasks", body), 201)

    def task_names(self) -> set[str]:
        return {task["name"] for task in self.ok(self.get("/tasks"))["items"]}

    def sync(self) -> dict:
        return self.ok(self.post("/local/sync"))


@pytest.fixture
def backend(server) -> Server:  # noqa: F811 - the imported fixture
    for email in ("alice@example.com", "bob@example.com"):
        server.register(email)
    return server


@pytest.fixture
def make_local(tmp_path):
    services: list[Local] = []

    def make(name: str = "device", transport=None, **kwargs) -> Local:
        local = Local(tmp_path / f"{name}.db", transport, **kwargs)
        services.append(local)
        return local

    yield make
    for local in services:
        try:
            local.stop()
        except Exception:  # noqa: BLE001 - already stopped by the test
            pass


@pytest.fixture
def transport(backend) -> InProcessTransport:
    return InProcessTransport(backend.client)


@pytest.fixture
def flaky(backend) -> FlakyTransport:
    return FlakyTransport(backend.client)
