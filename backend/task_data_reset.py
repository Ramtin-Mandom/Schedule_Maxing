"""
backend/task_data_reset.py

"Reset All Task Data" on the server: POST /me/task-data/reset (and the
direct desktop storage, which calls reset_task_data itself).

What is removed -- every live record of the authenticated user that is
task/schedule/execution data, in one Mutator (the user's change-log lock,
one transaction; any failure rolls all of it back):

    executions (their work sessions stay attached to the tombstone),
    placements (removal_reason "reset"), schedule generation records,
    fixed blocks, tasks (with their tags, dates, dependencies and recurrence
    rows), and projects.

What is kept: the account itself (credentials, profile) and scheduling
SETTINGS -- the preference layers, including default and per-date day
windows and engines (docs: they configure how days are scheduled; they are
not task data, and resetting tasks must not silently change the working
day). Other users' records are never read: every query is scoped by the
user id resolved from the access token.

How: each record becomes an ordinary versioned tombstone logged in the
change log, exactly like any other delete, so every other device of the
account removes its copy on its next pull, a stale queued update or delete
from another device is answered with "deleted" (never a revival), and a
replayed old operation answers its recorded outcome. The server keeps
tombstones and revision history, as everywhere in this backend
(docs/execution-rescheduling.md: "the server never purges history rows");
after a reset they appear in no read, list, sync snapshot or analytics.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select

from app.planning.models import PlacementRemovalReason
from backend import models
from backend.executions import EXECUTIONS
from backend.mutations import Mutator
from backend.resources import FIXED_BLOCKS, GENERATIONS, PLACEMENTS, PROJECTS, TASKS, Strict

#: (entity type, spec, model) in a foreign-key-safe order: what refers to a record goes before it.
RESET_ORDER = (
    ("execution", EXECUTIONS, models.Execution),
    ("placement", PLACEMENTS, models.Placement),
    ("schedule_generation", GENERATIONS, models.ScheduleGeneration),
    ("fixed_block", FIXED_BLOCKS, models.FixedBlock),
    ("task", TASKS, models.Task),
    ("project", PROJECTS, models.Project),
)


class ResetTaskDataIn(Strict):
    #: Must be true: the request itself states the intent (the client asks the user to confirm first).
    confirm: bool


class ResetTaskDataOut(BaseModel):
    #: How many live records of each type were removed.
    removed: dict[str, int]
    #: The account's change-log position right after the reset: a device that wiped its own copy continues here.
    cursor: int


def reset_task_data(mutator: Mutator) -> dict:
    removed: dict[str, int] = {}
    for entity_type, spec, model in RESET_ORDER:
        rows = list(mutator.session.scalars(
            select(model).where(model.user_id == mutator.user_id, model.deleted_at.is_(None)).order_by(model.id)
        ))
        for row in rows:
            if entity_type == "placement":
                row.removal_reason, row.superseded_by_id = PlacementRemovalReason.RESET.value, None
            mutator.tombstone(spec, row)
        removed[entity_type] = len(rows)
    return ResetTaskDataOut(removed=removed, cursor=mutator.change_seq).model_dump(mode="json")
