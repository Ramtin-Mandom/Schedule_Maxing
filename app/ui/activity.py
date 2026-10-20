"""
app/ui/activity.py

Whether the desktop window is in use, Tk-free (tests/ui/test_activity.py), and
what that means for the periodic work the app does on its own.

    FOCUSED     the window (or one of its dialogs) has the keyboard focus
    UNFOCUSED   it is visible, but another application is in front
    MINIMIZED   iconified or withdrawn: nothing of it can be seen

ActivityState follows the root window's focus and map events (app/app.py binds
<FocusIn>/<FocusOut>/<Map>/<Unmap>) and reports a change only when the state
really changes. ACTIVITY_POLICIES says, per state, how often the status bar
re-reads the sync status (paused while minimized), how often the Tk-thread
garbage collector looks at allocation counts, and the background sync
interval. Nothing here pauses work the user asked for: Sync now, a running
schedule generation and saves always run immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Activity(str, Enum):
    FOCUSED = "focused"
    UNFOCUSED = "unfocused"
    MINIMIZED = "minimized"


@dataclass(frozen=True)
class ActivityPolicy:
    #: How often the status bar re-reads the (local) sync status; None: not at all.
    status_poll_ms: int | None
    #: How often app/ui/tk_lifecycle.DesktopCollection looks at the allocation counts (it still collects).
    gc_interval_ms: int
    #: The background sync's interval while nothing else asks for a run (app/sync/service.py).
    sync_interval_s: float


ACTIVITY_POLICIES: dict[Activity, ActivityPolicy] = {
    Activity.FOCUSED: ActivityPolicy(status_poll_ms=4000, gc_interval_ms=2000, sync_interval_s=60.0),
    Activity.UNFOCUSED: ActivityPolicy(status_poll_ms=15000, gc_interval_ms=2000, sync_interval_s=300.0),
    Activity.MINIMIZED: ActivityPolicy(status_poll_ms=None, gc_interval_ms=30000, sync_interval_s=300.0),
}


def policy_for(activity: Activity) -> ActivityPolicy:
    return ACTIVITY_POLICIES[Activity(activity)]


class ActivityState:
    """
    The window's activity from its focus and map events. It starts FOCUSED and
    shown (what a window that just opened is); each event method returns the
    new Activity when the state changed, else None.
    """

    def __init__(self) -> None:
        self.focused = True
        self.shown = True

    @property
    def activity(self) -> Activity:
        if not self.shown:
            return Activity.MINIMIZED
        return Activity.FOCUSED if self.focused else Activity.UNFOCUSED

    def set_focused(self, focused: bool) -> Activity | None:
        return self._change(focused=focused)

    def set_shown(self, shown: bool) -> Activity | None:
        """Mapped (shown) or unmapped: iconified or withdrawn."""
        return self._change(shown=shown)

    def _change(self, **values: bool) -> Activity | None:
        before = self.activity
        for name, value in values.items():
            setattr(self, name, bool(value))
        after = self.activity
        return after if after != before else None
