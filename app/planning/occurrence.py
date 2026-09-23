"""
app/planning/occurrence.py

Occurrence and supersession identity for placements (Milestone 3), defined
before any rescheduling cleanup relies on it.

An *occurrence* is one intended performance of a task. A placement
(ScheduledTask) schedules exactly one occurrence, identified by
occurrence_key(placement, task):

    - an ordinary (non-recurring) task has exactly one occurrence, so its
      key is (task_id, None): every placement of it is the same occurrence;
    - a recurring template (Task.recurrence set) is model-only -- nothing
      expands it into per-date occurrence tasks yet -- so each *date* it is
      placed on is its own occurrence: (task_id, planned_date). Placements
      of one template on different dates are intentional, distinct
      occurrences and never supersede each other.

A new placement *supersedes* an older active placement exactly when both
have the same occurrence key. Rescheduling a range
(PlanningService.reschedule_range) uses this to remove obsolete placements
*outside* the range: only placements superseded by a placement the run
actually produced, never those of tasks the run left unplaced, never other
occurrences of a recurring template, and never a placement whose execution
has already started or finished (see HISTORY_PROTECTED_STATUSES) -- that
placement is history, not an obsolete plan.
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
    return (task.id, placement.planned_date if task.recurrence is not None else None)
