"""Cloud sync through the desktop's own services (open_app_services + SyncService),
against the in-process backend -- the contract the desktop account prompt builds on:
signing in claims nothing and switches to the account's (empty) workspace; the
association preview writes nothing; only a confirmed association claims the ownerless
records; records created in the account workspace are the account's; sync pushes them
with their ids; and a backend failure never blocks local work."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest

from app.planning.models import Task
from app.planning.scope import OwnerScope
from app.ui import background
from app.ui.app_services import open_app_services
from tests.sync.conftest import PASSWORD, FlakyTransport

MON = date(2024, 6, 3)


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


def add(services, name: str) -> Task:
    result = services.planning_controller.add_or_update_task(
        Task(name=name, category="study", estimated_duration_minutes=30, priority=5))
    assert result.ok, result.error
    return result.value


def names(services) -> set[str]:
    return {task.name for task in services.planning_controller.list_tasks().value}


def owners(services) -> dict[str, str | None]:
    return dict(services.connection.execute("SELECT name, user_id FROM tasks").fetchall())


def test_desktop_sign_in_association_and_sync(tmp_path: Path, alice_server) -> None:
    transport = FlakyTransport(alice_server.client)
    services = open_app_services(tmp_path / "desktop.db", timezone="UTC", project_root=str(tmp_path))
    try:
        services.sync_service.set_transport(transport)
        offline = add(services, "Offline")

        account = services.sync_service.sign_in("alice@example.com", PASSWORD)
        alice = uuid.UUID(account.user_id)
        assert services.switch_workspace().scope == OwnerScope.account(alice)
        assert names(services) == set()  # signing in claimed nothing
        assert owners(services) == {"Offline": None}

        preview = services.sync_service.association_preview()
        assert preview.counts["task"] == 1 and preview.problems == []
        assert owners(services) == {"Offline": None}  # previewing (or walking away) changes nothing

        online = add(services, "Online")
        assert owners(services) == {"Offline": None, "Online": str(alice)}
        preview = services.sync_service.association_preview()  # revalidated after the change
        services.sync_service.associate_local_data(preview.token)
        services.switch_workspace()
        assert names(services) == {"Offline", "Online"}

        transport.fail_push = 1
        assert services.sync_service.sync_now().status == "offline"
        assert services.sync_service.status().pending == 2  # durable, counted once each
        add(services, "Made while the backend is down")  # local work goes on
        assert services.planning_controller.schedule_range(MON, MON).ok

        report = services.sync_service.sync_now()
        assert report.status == "ok", report.message
        on_server = {task["id"] for task in alice_server.get("alice@example.com", "/tasks")["items"]}
        assert {str(offline.id), str(online.id)} <= on_server  # same ids on the server
        status = services.sync_service.status()
        assert status.pending == 0 and status.last_successful_sync_at is not None

        services.sync_service.sign_out()
        assert services.switch_workspace().scope == OwnerScope.ownerless()  # not active any more after sign-out
        assert names(services) == set()  # no account data visible in the ownerless workspace
    finally:
        services.close()
