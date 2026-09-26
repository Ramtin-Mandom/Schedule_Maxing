"""The desktop Account page and sync indicator as real widgets, against an in-process
backend and a temporary database: configuring the backend, registration and sign-in with
field errors and no duplicate submissions, the explicit association (cancel changes nothing,
confirm claims), Sync now from the status bar, a real conflict decided on the page, sign-out
rebuilding the pages for the ownerless workspace, and closing the window while a network
call is still running. Skipped without a display (tests/ui/test_desktop_app.py)."""

from __future__ import annotations

import gc
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from app.planning.models import Task
from app.planning.scope import OwnerScope
from app.ui import background
from tests.sync.conftest import PASSWORD, InProcessTransport, make_device as make_device  # noqa: F401
from tests.sync.conftest import project_root as project_root  # noqa: F401 - used by make_device
from tests.ui.test_desktop_app import Dialogs, fill_form, tree_names
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display

WEDNESDAY = date(2024, 6, 5)


@pytest.fixture
def dialogs(monkeypatch) -> Dialogs:
    recorder = Dialogs()
    recorder.install(monkeypatch)
    previous = background.current_registry()
    yield recorder
    background.install_registry(previous)


class Backend:
    def __init__(self, transport) -> None:
        self.transport = transport
        self.logins = 0
        real_login = transport.login

        def counting_login(email, password):
            self.logins += 1
            return real_login(email, password)

        transport.login = counting_login

    def factory(self, url: str):
        if not url.startswith(("http://", "https://")):
            raise ValueError("the backend URL must start with https:// (or http:// for a local server)")
        self.transport.base_url = url
        return self.transport


def open_app(db_path: Path, tmp_path: Path, backend: Backend):
    from app.app import ScheduleOptimizerApp

    app = ScheduleOptimizerApp(db_path=str(db_path), timezone="UTC", project_root=str(tmp_path), today=WEDNESDAY,
                               transport_factory=backend.factory, background_sync=False)
    app.geometry("1440x880+30+30")
    pump(app)
    return app


def pump(app, until=lambda: True, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        app.update()
        if until() and background.current_registry().active == 0:
            app.update()
            return
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the UI")
        time.sleep(0.01)


def close(app) -> None:
    app._on_close()
    gc.collect()


def owners(app) -> dict:
    return dict(app.services.connection.execute("SELECT name, user_id FROM tasks").fetchall())


def test_connect_register_sign_in_associate_sync_and_sign_out(tmp_path: Path, dialogs, server) -> None:
    backend = Backend(InProcessTransport(server.client))
    app = open_app(tmp_path / "desk.db", tmp_path, backend)
    try:
        day = app.pages["day"]
        fill_form(day, name="Offline plan")
        day.form.submit_button.invoke()
        app.show_page("account")
        page = app.pages["account"]
        assert page.headline.cget("text").startswith("Offline")
        assert page.submit_button.cget("state") == "disabled"  # nothing to sign in to yet
        assert "no backend configured" in app.shell.status_bar.label.cget("text")

        page.backend_field.variable.set("http://backend.test")
        page.save_backend_button.invoke()
        pump(app, lambda: "backend" not in page.busy)
        assert page.view.state == "signed_out" and "Signed out" in app.shell.status_bar.label.cget("text")

        page.set_mode("register")
        page.email_field.variable.set("alice")
        page.password_field.variable.set("short")
        page.submit_button.invoke()
        pump(app, lambda: "account" not in page.busy)
        assert page.email_field.error and page.password_field.error  # shown next to the fields
        assert page.password_field.get() == ""  # never kept
        page.email_field.variable.set("alice@example.com")
        page.password_field.variable.set(PASSWORD)
        page.submit_button.invoke()
        pump(app, lambda: "account" not in page.busy)
        assert "Account created" in page.account_notice.text and page.mode == "sign_in"

        old_day = app.pages["day"]
        page.password_field.variable.set(PASSWORD)
        page.submit_account()
        page.submit_account()  # a double submission sends once
        pump(app, lambda: "account" not in page.busy)
        pump(app, lambda: "preview" not in page.busy and "conflicts" not in page.busy)
        assert backend.logins == 1
        assert page.signed_in_label.cget("text") == "Signed in as a***@e***"
        pump(app, lambda: "profile" not in page.busy)
        assert "Plan: Normal" in page.profile_label.cget("text")
        assert "alice@example.com" not in page.profile_label.cget("text")
        assert "nothing on this device was uploaded or claimed" in page.account_notice.text.lower()
        assert app.services.workspace.scope.user_id is not None
        assert app.pages["day"] is not old_day and tree_names(app.pages["day"]) == []  # rebuilt: account workspace
        assert "1 record(s) on this device have no account" in page.association_label.cget("text")

        page.review_association()
        page.dialog.cancel()
        assert "Nothing was changed" in page.association_notice.text and owners(app)["Offline plan"] is None
        page.review_association()
        page.dialog.primary_button.invoke()
        pump(app, lambda: "associate" not in page.busy)
        assert "Associated 1 record(s)" in page.association_notice.text
        assert owners(app)["Offline plan"] is not None
        assert tree_names(app.pages["day"]) == ["Offline plan"]

        app.shell.status_bar.sync_button.invoke()
        pump(app, lambda: "Synchronizing" not in app.shell.status_bar.label.cget("text"))
        assert [task["name"] for task in server.get("alice@example.com", "/tasks")["items"]] == ["Offline plan"]
        page.refresh()
        assert "Changes waiting to be sent: 0" in page.sync_lines.cget("text")
        assert "last sync just now" in app.shell.status_bar.label.cget("text")

        page.sign_out_button.invoke()
        pump(app, lambda: "account" not in page.busy)
        assert app.services.workspace.scope == OwnerScope.ownerless()
        assert tree_names(app.pages["day"]) == []  # the account's records are not shown signed out
        assert dialogs.errors == []
    finally:
        close(app)


def test_a_real_conflict_is_compared_and_decided_on_the_page(tmp_path: Path, dialogs, alice_server,
                                                              make_device) -> None:
    backend = Backend(InProcessTransport(alice_server.client))
    app = open_app(tmp_path / "desk.db", tmp_path, backend)
    try:
        account = app.account_controller
        assert account.configure_backend("http://backend.test").ok
        assert account.sign_in("alice@example.com", PASSWORD).ok
        app.apply_workspace()
        essay = app.services.planning_controller.add_or_update_task(
            Task(name="Essay", category="study", estimated_duration_minutes=30, priority=5)).value
        assert account.sync_now().value.status == "ok"

        other = make_device("other", InProcessTransport(alice_server.client))
        other.sign_in("alice@example.com")
        other.sync_now()
        theirs = other.planning.get_task(essay.id)
        other.planning.update_task(theirs.model_copy(update={"name": "Essay (other device)"}),
                                   expected_version=theirs.version)
        other.sync_now()
        mine = app.services.planning_controller.get_task(essay.id).value
        app.services.planning_controller.add_or_update_task(mine.model_copy(update={"name": "Essay (this device)"}),
                                                            expected_version=mine.version)
        assert account.sync_now().value.conflicts == 1

        app.show_page("account")
        page = app.pages["account"]
        pump(app, lambda: not page.busy)
        [row] = page.conflict_tree.get_children()
        page.conflict_tree.selection_set(row)
        pump(app)
        rows = {page.difference_tree.item(item, "values")[0]: page.difference_tree.item(item, "values")
                for item in page.difference_tree.get_children()}
        assert rows["name"][1:] == ("Essay (this device)", "Essay (other device)")
        assert "based on server version" in page.detail_title.cget("text")
        assert page.choice_buttons["keep_local"].cget("state") == "normal"
        page.choice_buttons["keep_local"].invoke()
        pump(app, lambda: not page.busy)
        assert "Resolved" in page.conflict_notice.text and page.conflict_tree.get_children() == ()
        assert account.sync_now().value.status == "ok"
        assert [t["name"] for t in alice_server.get("alice@example.com", "/tasks")["items"]] == ["Essay (this device)"]
    finally:
        close(app)


def test_closing_during_a_network_call_is_clean(tmp_path: Path, dialogs, alice_server) -> None:
    backend = Backend(InProcessTransport(alice_server.client))
    app = open_app(tmp_path / "desk.db", tmp_path, backend)
    entered, release = threading.Event(), threading.Event()
    real_login = backend.transport.login

    def slow_login(email, password):
        entered.set()
        release.wait(timeout=10)
        return real_login(email, password)

    backend.transport.login = slow_login
    assert app.account_controller.configure_backend("http://backend.test").ok
    page = app.pages["account"]
    page.refresh()
    page.email_field.variable.set("alice@example.com")
    page.password_field.variable.set(PASSWORD)
    page.submit_account()
    assert entered.wait(timeout=5)
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    close(app)  # waits for the worker (bounded), then closes the database
    assert time.monotonic() - started < 10
    assert app.services.closed and background.current_registry().active == 0
    assert app.services.sync_service.signed_in  # the worker finished its sign-in before the database closed
