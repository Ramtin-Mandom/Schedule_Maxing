"""The desktop app and the optional local web service side by side
(docs/desktop-web-boundaries.md): they never share one database at the same time --
whichever starts second is refused, clearly, before touching anything -- and they apply
the same workspace rule, so after a restart both work in the device's active account
instead of one of them failing to create records in it."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.execution.instance_lock import DatabaseInUseError
from app.planning.models import Task
from app.planning.scope import OwnerScope
from app.ui import background
from app.ui.app_services import open_app_services
from app.web import __main__ as launcher
from app.web.local_app import LocalWebConfig, create_local_app


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


def open_desktop(db: Path, tmp_path: Path):
    return open_app_services(db, timezone="UTC", project_root=str(tmp_path))


def test_the_desktop_and_the_local_web_service_never_share_a_database(tmp_path: Path, capsys) -> None:
    db = tmp_path / "shared.db"
    desktop = open_desktop(db, tmp_path)
    try:
        with pytest.raises(DatabaseInUseError, match="the desktop app"):
            create_local_app(LocalWebConfig(db_path=db, background_sync=False))
        assert launcher.main(["--db-path", str(db), "--port", "8799"]) == 1  # refused before serving anything
        assert "already open in another Schedule Maxing process" in capsys.readouterr().err
        assert desktop.planning_controller.list_tasks().ok  # the desktop is unaffected
    finally:
        desktop.close()

    web = create_local_app(LocalWebConfig(db_path=db, background_sync=False))
    try:
        with pytest.raises(DatabaseInUseError, match="the local web service"):
            open_desktop(db, tmp_path)
    finally:
        web.state.runtime.close()

    open_desktop(db, tmp_path).close()  # free again once the service stopped


def test_both_clients_work_in_the_devices_active_account_after_a_restart(make_local, transport, tmp_path) -> None:
    local = make_local(transport=transport)
    local.create_task("Offline")
    local.sign_in("alice@example.com")
    preview = local.ok(local.get("/local/association/preview"))
    local.ok(local.post("/local/association", {"confirmation": preview["token"]}))
    alice = local.runtime.sync.account.user_id

    local.restart()  # the token was in memory only; Alice stays the device's active account
    session = local.ok(local.get("/local/session"))
    assert session["workspace"]["scope"] == "account"
    assert session["workspace"]["account"]["signed_in"] is False
    assert session["workspace"]["account"]["auth_required"] is True
    local.create_task("Created while signed out")  # previously refused: new rows are stamped with Alice
    assert local.task_names() == {"Offline", "Created while signed out"}
    local.stop()

    desktop = open_desktop(local.db_path, tmp_path)
    try:
        assert desktop.workspace.scope == OwnerScope.account(alice)
        saved = desktop.planning_controller.add_or_update_task(
            Task(name="From the desktop", category="study", estimated_duration_minutes=30, priority=5))
        assert saved.ok and str(saved.value.user_id) == alice
        assert {t.name for t in desktop.planning_controller.list_tasks().value} == {
            "Offline", "Created while signed out", "From the desktop"}
    finally:
        desktop.close()
