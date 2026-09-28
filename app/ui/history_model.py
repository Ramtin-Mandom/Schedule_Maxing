"""
app/ui/history_model.py

Tk-free execution-history browsing for the desktop (Milestone 5): every
intended occurrence of a date range -- with its original plan, outcome,
actual times, work sessions and move lineage -- plus the placements that
were removed from the plan in that range, so history stays reachable after a
task or placement leaves the current schedule. Built from one ScheduleHistory
and its schedule-cohort report (ProductivityService.schedule_history_and_report);
nothing here reads storage or writes anything.

Identity: entries are keyed by placement id and grouped by nothing else; two
tasks with the same name stay two entries (their labels add the time and a
short id when names collide).
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.execution.models import TaskExecution, WorkSession
from app.planning.history import ScheduleHistory
from app.planning.models import ScheduledTask
from app.productivity.schedule_cohort import CohortOccurrence, OccurrenceState, ScheduleCohortReport, lineage

#: History status filter values (display), besides "any".
STATUSES = ("completed", "skipped", "cancelled", "in_progress", "paused", "overdue", "upcoming", "removed")
STATUS_LABELS = {"completed": "Completed", "skipped": "Skipped", "cancelled": "Cancelled", "in_progress": "In progress",
                 "paused": "Paused", "overdue": "Overdue (not started)", "upcoming": "Upcoming", "removed": "Removed"}
REASON_LABELS = {"rescheduled": "moved", "regenerated": "replaced by a regeneration", "deleted": "deleted",
                 "task_deleted": "its task was deleted", "reset": "removed by a reset"}


@dataclass(frozen=True)
class HistoryEntry:
    key: uuid.UUID
    task_id: uuid.UUID
    name: str
    label: str
    local_date: date
    status: str
    category: str | None
    #: Oldest plan first; the last is the current (or final) placement.
    plan_lineage: list[ScheduledTask]
    execution: TaskExecution | None
    sessions: tuple[WorkSession, ...]
    occurrence: CohortOccurrence | None


@dataclass(frozen=True)
class HistoryPage:
    timezone: str
    start_date: date
    end_date: date
    entries: list[HistoryEntry]
    categories: list[str]
    total: int


def _status(occurrence: CohortOccurrence) -> str:
    if occurrence.state == OccurrenceState.NOT_STARTED:
        return "overdue" if occurrence.due else "upcoming"
    return occurrence.state.value


def build_history_page(
    history: ScheduleHistory,
    report: ScheduleCohortReport,
    *,
    status: str | None = None,
    category: str | None = None,
) -> HistoryPage:
    tz = report.timezone
    entries: list[HistoryEntry] = []
    for occurrence in report.occurrences:
        item = history.executions.get(occurrence.placement_id)
        entries.append(_entry(history, occurrence.placement_id, occurrence.task_id, occurrence.local_date,
                              _status(occurrence), occurrence.category, item, occurrence))
    counted = {p.id for entry in entries for p in entry.plan_lineage}
    for placement in history.placements.values():
        in_range = history.start_utc <= placement.planned_start < history.end_utc
        if not in_range or placement.id in counted or placement.deleted_at is None or placement.superseded_by_id:
            continue  # removed from the plan without a successor: still browsable
        item = history.executions.get(placement.id)
        category_value = placement.task_category or (item.execution.category if item else None)
        entries.append(_entry(history, placement.id, placement.task_id,
                              placement.planned_start.astimezone(ZoneInfo(tz)).date(), "removed", category_value,
                              item, None))

    names = Counter(entry.name for entry in entries)
    labelled = []
    for entry in entries:
        current = entry.plan_lineage[-1]
        when = f"{entry.local_date:%a %b} {entry.local_date.day} {current.planned_start.astimezone(ZoneInfo(tz)):%H:%M}"
        label = f"{when}  {entry.name}"
        if names[entry.name] > 1:
            label += f" [{str(entry.task_id)[:8]}]"
        labelled.append(HistoryEntry(**{**entry.__dict__, "label": f"{label} -- {STATUS_LABELS[entry.status]}"}))

    categories = sorted({entry.category for entry in labelled if entry.category is not None})
    selected = [entry for entry in labelled
                if (status is None or entry.status == status) and (category is None or entry.category == category)]
    selected.sort(key=lambda entry: (entry.plan_lineage[-1].planned_start, str(entry.key)))
    return HistoryPage(timezone=tz, start_date=report.start_date, end_date=report.end_date, entries=selected,
                       categories=categories, total=len(labelled))


def _entry(history, placement_id, task_id, local_date, status, category, item, occurrence) -> HistoryEntry:
    task = history.tasks.get(task_id)
    if task is not None:
        name = task.name + (" (deleted task)" if task.deleted_at is not None else "")
    elif item is not None:
        name = item.execution.task_name
    else:
        name = "(unknown task)"
    return HistoryEntry(
        key=placement_id, task_id=task_id, name=name, label=name, local_date=local_date, status=status,
        category=category, plan_lineage=lineage(history, placement_id),
        execution=item.execution if item else None, sessions=item.sessions if item else (), occurrence=occurrence,
    )


def _time(instant: datetime | None, tz: str) -> str:
    if instant is None:
        return "--"
    local = instant.astimezone(ZoneInfo(tz))
    return f"{local:%a %b} {local.day} {local:%H:%M}"


def detail_text(entry: HistoryEntry) -> str:
    """A plain-text detail view: original plan, lineage, outcome, actual times and sessions."""
    first, current = entry.plan_lineage[0], entry.plan_lineage[-1]
    tz = current.timezone
    lines = [entry.name, f"Status: {STATUS_LABELS[entry.status]}", f"Category (when planned): {entry.category or 'unknown'}",
             f"Original plan: {_time(first.planned_start, first.timezone)}-{_time(first.planned_end, first.timezone)} "
             f"({first.timezone})"]
    if len(entry.plan_lineage) > 1 or current.deleted_at is not None:
        lines.append("Plan history:")
        for placement in entry.plan_lineage:
            reason = REASON_LABELS.get(placement.removal_reason.value, placement.removal_reason.value) \
                if placement.removal_reason else ("removed (reason unknown)" if placement.deleted_at else "current")
            lines.append(f"  {_time(placement.planned_start, placement.timezone)}-"
                         f"{_time(placement.planned_end, placement.timezone)}: {reason}")
    execution = entry.execution
    if execution is None:
        lines.append("Outcome: no execution recorded.")
    else:
        lines.append(f"Estimate when started: {execution.planned_duration} min")
        lines.append(f"First start: {_time(execution.actual_first_start_at, tz)}; "
                     f"final end: {_time(execution.actual_final_end_at, tz)}")
        if execution.actual_active_duration_minutes is not None:
            lines.append(f"Active time: {execution.actual_active_duration_minutes:g} min (pauses excluded)")
        for index, session in enumerate(entry.sessions, start=1):
            start = datetime.fromisoformat(session.started_at)
            end = datetime.fromisoformat(session.ended_at) if session.ended_at else None
            lines.append(f"  Session {index}: {_time(start, tz)} - {_time(end, tz) if end else 'open'}")
    lines.append(f"Times shown in {tz}.")
    return "\n".join(lines)
