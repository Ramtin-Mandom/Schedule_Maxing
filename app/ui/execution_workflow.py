"""
app/ui/execution_workflow.py

The Tk-free presentation rules of the Day page's Execute tab (Milestone 5):
what a saved placement's state is *now*, which actions are legal, and the
planned/actual/active texts -- derived from the persisted placement, its
execution (if any) and an explicit `now`, never stored.

    - Actions come from the lifecycle's transition table
      (app/execution/lifecycle.py); a placement with no execution behaves as
      `scheduled` (its execution is created only when the user acts).
      Reschedule is offered only while nothing has started
      (docs/execution-rescheduling.md); otherwise the reason is given.
    - Overdue is derived, not a status: a not-started placement whose planned
      end has passed. In-progress or paused work past its planned end keeps
      its state and is marked as running late.
    - Planned interval and estimate, actual first start / final end, and
      active (session) minutes are separate lines; times are shown in the
      plan's own timezone, which is named.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.execution.lifecycle import TRANSITIONS
from app.execution.models import ExecutionStatus, TaskExecution
from app.planning.models import ScheduledTask
from app.planning.occurrence import HISTORY_PROTECTED_STATUSES
from app.planning.time import elapsed_minutes

#: The Execute tab's actions, in button order. "complete" is shown as Finish.
ACTIONS = ("start", "pause", "resume", "complete", "skip", "cancel", "reschedule")
ACTION_LABELS = {"start": "Start", "pause": "Pause", "resume": "Resume", "complete": "Finish", "skip": "Skip",
                 "cancel": "Cancel", "reschedule": "Reschedule..."}

STATUS_LABELS = {
    ExecutionStatus.SCHEDULED: "Not started",
    ExecutionStatus.IN_PROGRESS: "In progress",
    ExecutionStatus.PAUSED: "Paused",
    ExecutionStatus.COMPLETED: "Completed",
    ExecutionStatus.SKIPPED: "Skipped",
    ExecutionStatus.CANCELLED: "Cancelled",
}

#: How the state of a record relative to the server is worded (SyncService.record_sync_state).
SYNC_TEXT = {
    "local_only": "Saved on this device only (no account).",
    "pending": "Saved on this device; not yet confirmed by the server.",
    "conflict": "Conflicts with the server's copy -- resolve it on the Account page.",
    "synced": "Confirmed by the server.",
    "server": "Saved on the server.",
}

RESCHEDULE_BLOCKED = ("Started or finished work stays in history and cannot be moved. To do it again, add "
                      "and schedule the task anew.")


@dataclass(frozen=True)
class ExecutionItemView:
    #: The lifecycle status label ("Not started", "In progress", ...).
    status_text: str
    #: Upcoming / due / overdue / running late -- derived from time and the persisted outcome.
    timing_text: str
    overdue: bool
    late: bool
    #: The legal actions, in ACTIONS order.
    actions: tuple[str, ...]
    #: Why Reschedule is not offered (None when it is).
    reschedule_blocked: str | None
    planned_text: str
    actual_text: str
    active_text: str
    sync_text: str | None


def _clock(instant: datetime, tz: str, planned_day) -> str:
    local = instant.astimezone(ZoneInfo(tz))
    text = local.strftime("%H:%M")
    return text if local.date() == planned_day else f"{local:%a %b} {local.day} {text}"


def legal_actions(status: ExecutionStatus) -> tuple[str, ...]:
    lifecycle = [action for action in ACTIONS[:-1] if status in TRANSITIONS[action][0]]
    if status.value not in HISTORY_PROTECTED_STATUSES:
        lifecycle.append("reschedule")
    return tuple(lifecycle)


def describe_item(
    placement: ScheduledTask,
    execution: TaskExecution | None,
    *,
    now: datetime,
    active_minutes: float | None = None,
    sync_state: str | None = None,
) -> ExecutionItemView:
    status = execution.status if execution is not None else ExecutionStatus.SCHEDULED
    tz, day = placement.timezone, placement.planned_date
    minutes = round(elapsed_minutes(placement.planned_start, placement.planned_end))

    overdue = status == ExecutionStatus.SCHEDULED and placement.planned_end <= now
    late = status in (ExecutionStatus.IN_PROGRESS, ExecutionStatus.PAUSED) and placement.planned_end < now
    if overdue:
        timing = f"Overdue: planned to end at {_clock(placement.planned_end, tz, day)} and not started."
    elif late:
        behind = round(elapsed_minutes(placement.planned_end, now))
        timing = f"Running past its planned end ({_clock(placement.planned_end, tz, day)}) by {behind} min."
    elif status == ExecutionStatus.SCHEDULED:
        timing = "Due now." if placement.planned_start <= now else "Upcoming."
    else:
        timing = STATUS_LABELS[status] + "."

    if execution is None or execution.actual_first_start_at is None:
        actual = "Not started." if status in (ExecutionStatus.SCHEDULED, ExecutionStatus.SKIPPED,
                                              ExecutionStatus.CANCELLED) or execution is None else "Start not recorded."
        if execution is not None and execution.actual_final_end_at is not None:
            actual = f"{STATUS_LABELS[status]} at {_clock(execution.actual_final_end_at, tz, day)} without starting."
    else:
        actual = f"First started {_clock(execution.actual_first_start_at, tz, day)}"
        if execution.actual_final_end_at is not None:
            actual += f"; ended {_clock(execution.actual_final_end_at, tz, day)} ({STATUS_LABELS[status].lower()})"
        actual += "."

    if execution is not None and status == ExecutionStatus.COMPLETED and \
            execution.actual_active_duration_minutes is not None:
        active = f"Active time: {execution.actual_active_duration_minutes:g} min (pauses excluded)."
    elif active_minutes is not None and execution is not None and execution.actual_first_start_at is not None:
        active = f"Active time so far: {active_minutes:g} min (pauses excluded)."
    else:
        active = "Active time: none recorded."

    return ExecutionItemView(
        status_text=STATUS_LABELS[status], timing_text=timing, overdue=overdue, late=late,
        actions=legal_actions(status),
        reschedule_blocked=None if status.value not in HISTORY_PROTECTED_STATUSES else RESCHEDULE_BLOCKED,
        planned_text=(f"Planned {_clock(placement.planned_start, tz, day)}-{_clock(placement.planned_end, tz, day)} "
                      f"on {day:%a %b} {day.day} ({minutes} min estimate), {tz}."),
        actual_text=actual, active_text=active,
        sync_text=SYNC_TEXT.get(sync_state) if sync_state else None,
    )
