"""The desktop's activity state and policy (app/ui/activity.py), and the timers it slows down, without Tk."""

from __future__ import annotations

from app.ui.activity import ACTIVITY_POLICIES, Activity, ActivityState, policy_for
from app.ui.tk_lifecycle import DesktopCollection, tune_customtkinter_loops


class _Root:
    """The after/after_cancel part of a Tk root."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, object]] = []
        self.cancelled: list[object] = []

    def after(self, ms, callback):
        self.scheduled.append((ms, callback))
        return f"after#{len(self.scheduled)}"

    def after_cancel(self, ident) -> None:
        self.cancelled.append(ident)


def test_customtkinter_tracker_loops_are_slowed_down_for_ctk_5_2_2() -> None:
    import customtkinter
    from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import AppearanceModeTracker
    from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

    tune_customtkinter_loops()
    assert customtkinter.__version__ == "5.2.2"  # the private trackers below were checked against this version
    assert AppearanceModeTracker.update_loop_interval == 1000  # was 30 ms; "system" appearance is never used
    assert ScalingTracker.update_loop_interval == 500  # was 100 ms; a monitor DPI change is still seen


def test_the_gc_timer_looks_less_often_but_keeps_collecting() -> None:
    root = _Root()
    collection = DesktopCollection(root)
    try:
        assert root.scheduled[-1][0] == 2000
        collection.set_interval(30000)  # minimized: the pending look stays, the next ones are slower
        assert root.cancelled == []
        collection.collect()
        assert root.scheduled[-1][0] == 30000
        collection.set_interval(2000)  # restored: re-planned at once
        assert root.cancelled == ["after#2"] and root.scheduled[-1][0] == 2000
    finally:
        collection.close()


def test_a_new_window_is_focused_and_reports_only_real_changes() -> None:
    state = ActivityState()
    assert state.activity == Activity.FOCUSED
    assert state.set_focused(True) is None  # no change
    assert state.set_focused(False) == Activity.UNFOCUSED
    assert state.set_shown(False) == Activity.MINIMIZED
    assert state.set_focused(True) is None  # focus while minimized changes nothing visible
    assert state.set_shown(True) == Activity.FOCUSED


def test_minimized_wins_over_focus() -> None:
    state = ActivityState()
    state.set_shown(False)
    assert state.activity == Activity.MINIMIZED
    state.set_focused(False)
    assert state.set_shown(True) == Activity.UNFOCUSED


def test_the_policy_slows_down_and_pauses_work_the_less_the_window_is_used() -> None:
    focused, unfocused, minimized = (policy_for(activity) for activity in Activity)
    assert focused.status_poll_ms == 4000 and focused.sync_interval_s == 60.0
    assert unfocused.status_poll_ms == 15000 and unfocused.sync_interval_s == 300.0
    assert minimized.status_poll_ms is None  # paused: nothing to show
    assert minimized.gc_interval_ms > focused.gc_interval_ms  # still collects, less often
    assert set(ACTIVITY_POLICIES) == set(Activity)
    assert policy_for("minimized") is minimized
