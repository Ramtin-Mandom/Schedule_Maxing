"""
The desktop window's activity (app/ui/activity.py) applied by app/app.py, and
the status bar redrawing only what changed (real windows).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from app.ui.activity import Activity
from tests.ui.test_desktop_app import close_app, dialogs, open_app, pump  # noqa: F401 - fixture


@pytest.fixture
def app(tmp_path, dialogs):  # noqa: F811 - the fixture imported above
    window = open_app(tmp_path / "app.db", tmp_path, build_pages=False)
    yield window
    close_app(window)


@pytest.fixture
def window_manager(tmp_path):
    """
    A real minimize needs a window manager. On an X display without one (CI's xvfb-run), Openbox runs for
    this test only (installed by .github/workflows/ci.yml); where one already runs, Openbox exits at once.
    """
    if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY") or shutil.which("openbox") is None:
        yield
        return
    ready = tmp_path / "openbox-started"
    manager = subprocess.Popen(["openbox", "--startup", f"touch {shlex.quote(str(ready))}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while not ready.exists() and manager.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield
    finally:
        if manager.poll() is None:
            manager.terminate()
            manager.wait(timeout=10)


def test_minimizing_pauses_the_status_poll_and_restoring_reads_once(app, monkeypatch) -> None:
    reads = []
    original = app.refresh_status
    monkeypatch.setattr(app, "refresh_status", lambda: (reads.append(1), original())[1])
    assert app._status_poll is not None

    app._activity_changed(app.activity.set_shown(False))  # what <Unmap> of the iconified window does
    assert app.activity.activity == Activity.MINIMIZED
    assert app._status_poll is None and app._collection.interval_ms == 30000
    assert app.services.sync_service.activity == "minimized"
    app._poll_status()  # a poll that was already due does not schedule another
    assert app._status_poll is None
    reads.clear()

    app._activity_changed(app.activity.set_shown(True))
    assert reads == [1] and app._status_poll is not None  # one read now, then polling again
    assert app._collection.interval_ms == 2000 and app.services.sync_service.activity == "focused"
    pump(app)


def test_another_application_in_front_slows_the_status_poll(app, monkeypatch) -> None:
    scheduled = []
    original_after = app.after
    monkeypatch.setattr(app, "after", lambda ms, *args: (scheduled.append(ms), original_after(ms, *args))[1])
    app._activity_changed(app.activity.set_focused(False))
    assert scheduled[-1] == 15000 and app.services.sync_service.activity == "unfocused"
    app._activity_changed(app.activity.set_focused(True))
    assert scheduled[-1] == 4000


def test_the_window_iconified_for_real_is_minimized(window_manager, app) -> None:  # the manager starts first
    app.deiconify()
    pump(app)
    app.iconify()
    pump(app, lambda: app.activity.activity == Activity.MINIMIZED, timeout=5)
    app.deiconify()
    pump(app, lambda: app.activity.activity != Activity.MINIMIZED, timeout=5)
    assert app._status_poll is not None


def test_the_status_bar_redraws_only_what_changed(app, monkeypatch) -> None:
    bar = app.shell.status_bar
    bar.show("Offline", can_sync=False, syncing=False)
    calls = []
    monkeypatch.setattr(bar.label, "configure", lambda **kwargs: calls.append(("label", kwargs)))
    monkeypatch.setattr(bar.sync_button, "configure", lambda **kwargs: calls.append(("button", kwargs)))
    bar.show("Offline", can_sync=False, syncing=False)
    assert calls == []  # the 4-second poll usually says the same thing
    bar.show("Signed in", can_sync=False, syncing=False)
    assert calls == [("label", {"text": "Signed in"})]
    bar.show("Signed in", can_sync=True, syncing=False)
    assert calls[-1] == ("button", {"state": "normal", "text": "Sync now"})
