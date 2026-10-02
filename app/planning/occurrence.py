"""
app/planning/occurrence.py

Occurrence and supersession identity for placements (Milestone 3, made
recurrence-aware in docs/recurrence.md), defined before any rescheduling
cleanup relies on it.

An *occurrence* is one intended performance of a task. A placement
(ScheduledTask) schedules exactly one occurrence, identified by
occurrence_key(placement, task):

    - an ordinary (non-recurring) task has exactly one occurrence, so its
      key is (task_id, None): every placement of it is the same occurrence;
    - a materialized occurrence of a recurring series (Task.series_id set)
      is the series' original slot: (series_id, occurrence_slot). The key
      depends only on the occurrence task, never on where its placement is,
      so moving it -- to another time or another date -- keeps its identity,
      and the date it left can never produce its work again;
    - a placement of a series definition itself (Task.recurrence set) can
      only be a *legacy* one, saved before series were expanded: its date was
      its occurrence, so its key is (series_id, planned_date) -- exactly the
      key of the occurrence later materialized for that slot. That identity
      mapping lets the materialized occurrence's placement supersede the
      legacy placement instead of duplicating its work, while the legacy
      placement and the executions recorded against it keep their ids, task
      id and snapshots. A legacy placement can still only move within its own
      date (another date would be another slot).

A new placement *supersedes* an older active placement exactly when both
have the same occurrence key. Rescheduling a range
(PlanningService.reschedule_range) uses this to remove obsolete placements
*outside* the range: only placements superseded by a placement the run
actually produced, never those of tasks the run left unplaced, never other
occurrences of a series, and never a placement whose execution has already
started or finished (see HISTORY_PROTECTED_STATUSES) -- that placement is
history, not an obsolete plan.

An explicit reschedule (app.planning.workflow.reschedule_placement) keeps
the occurrence: the replacement is the same task's placement. The same
statuses make a placement immovable.
"""

from __future__ import annotations

import uuid
from datetime import date as date_

from app.planning.models import ScheduledTask, Task

OccurrenceKey = tuple[uuid.UUID, date_ | None]

#: Execution statuses that make a placement historical: rescheduling never
#: supersedes (removes) it outside the rescheduled range.
HISTORY_PROTECTED_STATUSES = frozenset({"in_progress", "paused", "completed", "skipped", "cancelled"})


def occurrence_key(placement: ScheduledTask, task: Task) -> OccurrenceKey:
    if placement.task_id != task.id:
        raise ValueError(f"placement {placement.id} belongs to task {placement.task_id}, not {task.id}")
    return task_occurrence_key(task, placement.planned_date)


def task_occurrence_key(task: Task, planned_date: date_ | None = None) -> OccurrenceKey:
    """The occurrence a placement of `task` dated `planned_date` schedules (planned_date matters for legacy only)."""
    if task.series_id is not None:
        return (task.series_id, task.occurrence_slot)
    if task.recurrence is not None:
        return (task.id, planned_date)
    return (task.id, None)
