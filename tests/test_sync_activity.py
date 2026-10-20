"""
The background sync's activity-aware cadence (app/sync/service.py set_activity, _idle):
slower while the desktop window is not focused, no pending looks while it is
minimized, a sync on regaining focus when the last one is old, and a local edit
pushed a short debounce after it. The protocol and the backoff are unchanged.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.execution.db import get_connection, transaction, transaction_state_for
from app.sync.service import SyncService


class _Transport:
    base_url = "http://127.0.0.1:9"


@pytest.fixture
def service(tmp_path):
    connection = get_connection(tmp_path / "sync.db")
    sync = SyncService(connection, _Transport(), interval=60.0, background_interval=300.0, pending_poll=0.05,
                       edit_debounce=0.2)
    yield sync
    sync.stop(timeout=1)
    connection.close()


def _timed_idle(sync: SyncService, delay: float, **kwargs) -> float:
    started = time.monotonic()
    sync._idle(delay, **kwargs)
    return time.monotonic() - started


def test_the_interval_follows_the_activity_and_the_backoff_does_not(service) -> None:
    assert service.next_delay() == 60.0
    service.set_activity("unfocused")
    assert service.next_delay() == 300.0
    service.set_activity("minimized")
    assert service.next_delay() == 300.0
    service.consecutive_failures = 2
    assert service.next_delay() == 10.0  # backoff_base * 2: the same whatever the activity
    with pytest.raises(ValueError):
        service.set_activity("asleep")


def test_minimized_never_looks_at_the_pending_count(service, monkeypatch) -> None:
    looks = []
    monkeypatch.setattr(service, "_pending_now", lambda: looks.append(1) or 5)
    service._pending_seen = 0
    service.set_activity("minimized")
    service._nudge.clear()
    assert _timed_idle(service, 0.3) >= 0.29
    assert looks == []  # a changed count would have ended the wait: it was never asked
    service.set_activity("unfocused")
    service._nudge.clear()
    assert _timed_idle(service, 5.0) < 1.0 and looks  # visible again: the next look sees the new work


def test_regaining_focus_syncs_now_only_when_the_last_success_is_old(service) -> None:
    service.set_activity("unfocused")
    service._wake.clear()
    service._last_success = time.monotonic()  # just synchronized
    service.set_activity("focused")
    assert not service._wake.is_set()
    service.set_activity("unfocused")
    service._last_success = time.monotonic() - 61  # older than the focused interval
    service.set_activity("focused")
    assert service._wake.is_set()
    assert _timed_idle(service, 30.0) < 0.5  # the waiting loop starts the sync at once


def test_a_change_of_activity_re_plans_the_loops_own_wait(service) -> None:
    service._interval = 0.3  # focused: 0.3 s from the start of the wait
    service._last_success = time.monotonic()  # recent: regaining focus re-plans instead of syncing at once
    service.set_activity("minimized")
    service._nudge.clear()
    finished = []
    waiter = threading.Thread(target=lambda: finished.append(_timed_idle(service, service.next_delay(), replan=True)))
    waiter.start()
    time.sleep(0.1)
    service.set_activity("focused")  # 300 s -> 0.3 s, counted from the start
    waiter.join(5)
    assert not service._wake.is_set()
    assert finished and 0.25 <= finished[0] < 1.0


def test_a_local_commit_is_pushed_after_the_debounce(service, monkeypatch) -> None:
    monkeypatch.setattr(service, "_pending_now", lambda: 1)  # the edit is pending
    service._pending_seen = 0
    service._pending_poll = 10.0  # only the edit can end the wait early
    service._local_commit()
    service._nudge.clear()
    waited = _timed_idle(service, 30.0)
    assert 0.15 <= waited < 2.0  # edit_debounce (0.2 s) after the commit, not the 10 s poll


def test_the_commit_listener_is_registered_only_while_the_loop_runs(service, monkeypatch) -> None:
    state = transaction_state_for(service._connection)
    monkeypatch.setattr(service, "_run", lambda initial_delay=0.0: None)
    service.start()
    assert service._local_commit in state._listeners
    with transaction(service._connection) as connection:
        connection.execute("INSERT INTO local_settings (name, value) VALUES ('probe', 'x')")
    assert service._edited_at is not None
    service.stop(timeout=1)
    assert service._local_commit not in state._listeners


def test_the_first_run_can_wait_for_the_window_and_wake_ends_the_wait(service, monkeypatch) -> None:
    runs = []
    monkeypatch.setattr(service, "sync_now", lambda: runs.append(time.monotonic()))
    started = time.monotonic()
    service.start(initial_delay=30.0)
    time.sleep(0.2)
    assert runs == []
    service.wake()  # e.g. Sync now
    deadline = time.monotonic() + 5
    while not runs and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runs and runs[0] - started < 5
