"""Local storage runs off the Tk thread: blocked storage never blocks a UI callback, stale results never win.

Storage calls are held with events (no sleeps): each test proves its UI callback returned while the storage call
was still blocked, then releases it and awaits the result with pump().
"""
from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from app.ui import background
from app.ui.background import ControllerResult, run_in_background
from tests.ui.test_desktop_app import (
    WEDNESDAY, close_app, fill_form, finish_closing, open_app, pump, stored_tasks, tree_names,
)
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display


class Gate:
    """Holds every call of `target.name` until released; optionally answers the first call itself."""

    def __init__(self, monkeypatch, target, name: str, *, first=None) -> None:
        self.entered, self.release, self.calls = threading.Event(), threading.Event(), 0
        original, lock = getattr(target, name), threading.Lock()

        def held(*args, **kwargs):
            with lock:
                self.calls += 1
                call = self.calls
            self.entered.set()
            assert self.release.wait(20), "the test never released the storage call"
            return first if first is not None and call == 1 else original(*args, **kwargs)

        monkeypatch.setattr(target, name, held)

    def open(self) -> None:
        self.release.set()


def registry():
    return background.current_registry()


def test_rapid_date_changes_return_at_once_and_only_the_newest_date_is_drawn(tmp_path: Path, dialogs, monkeypatch):
    app = open_app(tmp_path / "dates.db", tmp_path)
    day = app.pages["day"]
    gate = Gate(monkeypatch, app.services.planning_controller, "load_range")
    try:
        drawn, render = [], day._render
        monkeypatch.setattr(day, "_render", lambda snapshot: (drawn.append(snapshot.day), render(snapshot))[1])
        day.shift_date(1)  # returns although the read is blocked
        assert gate.entered.wait(10) and registry().active >= 1
        day.shift_date(1)
        day.shift_date(1)
        # The Tk thread owns the date: it moved three days without waiting for storage, and nothing stale is shown.
        assert day.page_controller.anchor_date == WEDNESDAY + timedelta(days=3)
        assert day.snapshot.day == WEDNESDAY and drawn == []
        app.update()  # the event loop keeps running while storage is blocked
        assert drawn == [] and not day._busy
        gate.open()
        pump(app)
        # The overtaken reads ran in other workers, but their results were discarded, not drawn.
        assert drawn == [WEDNESDAY + timedelta(days=3)]
        assert day.snapshot.day == WEDNESDAY + timedelta(days=3)
        assert day.start_date_var.get() == (WEDNESDAY + timedelta(days=3)).isoformat() and dialogs.errors == []
    finally:
        gate.open()
        close_app(app)


def test_a_blocked_save_keeps_the_page_busy_refuses_a_second_and_a_failure_keeps_the_form(tmp_path, dialogs,
                                                                                         monkeypatch):
    db_path = tmp_path / "save.db"
    app = open_app(db_path, tmp_path)
    day = app.pages["day"]
    try:
        failing = Gate(monkeypatch, app.services.planning_controller, "add_or_update_task",
                       first=ControllerResult.failure("The disk is full."))
        fill_form(day, name="Kept in the form")
        day.form.submit_button.invoke()  # returns while the save is blocked
        assert failing.entered.wait(10) and day._busy
        assert day.form.name_field.variable.get() == "Kept in the form"
        day.form.submit_button.invoke()  # a second save while the first is running is refused, not queued
        day.shift_date(1)
        assert len(dialogs.infos) == 2 and day.page_controller.anchor_date == WEDNESDAY and failing.calls == 1
        failing.open()
        pump(app)
        assert not day._busy and "disk is full" in day.form.notice.text
        assert day.form.name_field.variable.get() == "Kept in the form" and stored_tasks(db_path) == []

        day.form.submit_button.invoke()  # the same form, saved for real this time
        pump(app)
        assert tree_names(day) == ["Kept in the form"] and [task.name for task in stored_tasks(db_path)] == [
            "Kept in the form"]
    finally:
        failing.open()
        close_app(app)


def test_a_workspace_change_drops_blocked_reads_and_the_destroyed_page_is_never_touched(tmp_path, dialogs,
                                                                                       monkeypatch):
    app = open_app(tmp_path / "workspace.db", tmp_path)
    old_day, old_settings = app.pages["day"], app.pages["settings"]
    gate = Gate(monkeypatch, app.services.planning_controller, "load_range")
    try:
        drawn = []
        monkeypatch.setattr(old_day, "_render", lambda snapshot: drawn.append(snapshot))
        monkeypatch.setattr(old_settings, "_loaded", lambda result: drawn.append(result))
        old_day.reload()
        old_settings.on_show()
        assert gate.entered.wait(10)
        app.apply_workspace()  # new controllers and pages; the old pages are destroyed while their reads run
        new_day = app.pages["day"]
        assert new_day is not old_day and not old_day.winfo_exists()
        gate.open()
        pump(app)
        assert drawn == [] and new_day.snapshot is not None and new_day.snapshot.day == WEDNESDAY
        assert registry().outstanding == 0 and dialogs.errors == []
    finally:
        gate.open()
        close_app(app)


def test_a_slow_status_read_never_blocks_the_ui_or_piles_up(tmp_path, dialogs, monkeypatch):
    app = open_app(tmp_path / "status.db", tmp_path)
    try:
        shown, show = [], app.shell.status_bar.show
        monkeypatch.setattr(app.shell.status_bar, "show", lambda text, **state: (shown.append(text), show(text, **state))[1])
        gate = Gate(monkeypatch, app.account_controller, "connection")
        before = registry().active
        app.refresh_status()  # returns while the read is blocked
        assert gate.entered.wait(10)
        for _ in range(25):  # timer ticks and other requests while the read is slow
            app.refresh_status()
        app._poll_status()
        app.update()
        assert gate.calls == 1 and registry().active == before + 1 and shown == []  # one request, no backlog
        gate.open()
        pump(app)
        assert gate.calls == 2 and not app._status_reading  # everything asked meanwhile became one more read
        assert shown and all("unavailable" not in text for text in shown)
    finally:
        gate.open()
        close_app(app)


def test_a_status_read_for_the_previous_workspace_is_rejected_and_read_again(tmp_path, dialogs, monkeypatch):
    app = open_app(tmp_path / "status-workspace.db", tmp_path)
    try:
        shown, show = [], app.shell.status_bar.show
        monkeypatch.setattr(app.shell.status_bar, "show", lambda text, **state: (shown.append(text), show(text, **state))[1])
        gate = Gate(monkeypatch, app.account_controller, "connection",
                    first=ControllerResult.failure("the previous workspace"))
        app.refresh_status()
        assert gate.entered.wait(10)
        app.services.switch_workspace()  # the read in flight now belongs to another workspace
        gate.open()
        pump(app)
        assert gate.calls == 2 and shown and not any("previous workspace" in text for text in shown)
    finally:
        gate.open()
        close_app(app)


@pytest.mark.parametrize("kind", ["status", "storage", "computation", "sync"])
def test_closing_never_blocks_and_storage_closes_only_after_the_work_is_done(tmp_path, dialogs, monkeypatch, kind):
    db_path = tmp_path / f"close-{kind}.db"
    app = open_app(db_path, tmp_path)
    day, loop_released = app.pages["day"], threading.Event()
    try:
        if kind == "status":
            gate = Gate(monkeypatch, app.account_controller, "connection")
            app.refresh_status()
        elif kind == "storage":
            gate = Gate(monkeypatch, app.services.planning_controller, "add_or_update_task")
            fill_form(day, name="Saved while closing")
            day.form.submit_button.invoke()
        elif kind == "computation":
            gate = Gate(monkeypatch, day.page_controller, "make_schedule_for")
            day.make_schedule()
        else:  # the sync loop thread is in the middle of a run
            gate = Gate(monkeypatch, app.account_controller, "sync_now")
            app.sync_now()
            loop = threading.Thread(target=loop_released.wait, daemon=True)
            loop.start()
            app.services.sync_service._thread = loop
        assert gate.entered.wait(10)

        app._on_close()  # returns while the work is still blocked
        assert not app._closed and not app.services.closed and app.state() == "withdrawn"
        assert run_in_background(app, lambda: None, lambda _result: None) is False  # new work is refused
        app._on_close()  # idempotent
        for _ in range(5):  # further polls: storage stays open for the work that still uses it
            app._finish_close()
            app.update()
        assert not app._closed and not app.services.closed and app.winfo_exists()

        gate.open()
        if kind == "sync":
            with registry()._condition:
                assert registry()._condition.wait_for(lambda: registry()._active == 0, timeout=10)
            app._finish_close()
            assert not app.services.closed  # the job is done, but the sync loop thread is still alive
            loop_released.set()
        finish_closing(app)
        assert app._closed and app.services.closed and registry().active == 0
        if kind == "storage":  # an accepted write is finished, never dropped, before the database closes
            assert [task.name for task in stored_tasks(db_path)] == ["Saved while closing"]
    finally:
        loop_released.set()
        if not app._closed:
            close_app(app)


def test_settings_rows_are_kept_when_revisited_and_only_a_changed_row_is_rebuilt(tmp_path, dialogs):
    app = open_app(tmp_path / "rows.db", tmp_path)
    try:
        app.show_page("settings")
        pump(app)
        editor = app.pages["settings"].editor
        total, frames = len(editor.rows), dict(editor._frames)
        assert editor.rows_built == total > 3
        key = "reward.weight_importance"
        stored = editor.value_of(key)
        editor.set_input(key, "typed but not saved")
        editor.show_error(key, "an earlier error")
        app.show_page("day")
        app.show_page("settings")
        pump(app)
        # The same widgets, showing what a rebuild would: stored values, no stale error.
        assert editor.rows_built == 0 and editor._frames == frames
        assert editor.value_of(key) == stored and editor.error_labels[key].cget("text") == ""

        editor.set_input(key, "7")
        editor.buttons[key]["save"].invoke()
        pump(app)
        assert 1 <= editor.rows_built < total and editor._frames[key] is not frames[key]
        assert editor.rows[key].state == "set" and editor.value_of(key) != stored
        assert all(frame.winfo_exists() for frame in editor._frames.values())
    finally:
        close_app(app)
