"""
app/planning/execution_facts.py

ExecutionFact: the part of one execution row that planning reads about a
task's history (whether and when it was completed, skipped or cancelled),
without loading full TaskExecution records. Produced by the local and
server planning repositories (execution_facts_for_tasks) and read by
PlanningService.execution_statuses_for_tasks (series and project views) and
the task-data reset preview.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExecutionFact:
    task_id: uuid.UUID
    scheduled_task_id: uuid.UUID | None
    status: str
    finished_at: datetime | None
    updated_at: str
