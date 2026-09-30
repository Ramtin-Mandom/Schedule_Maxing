"""
backend/days.py

Date-level views of the user's SCHEDULED work on the server (hosted API):

    GET  /days/summary?start_date=&end_date=   every date's aggregates and status class, in one
                                                request (a Month view makes one call, not one per date)
    POST /days/{date}/outcome                  every live placement of that date to completed,
                                                uncompleted or pending, in one transaction

Both use the shared rules: app/productivity/day_summary.py (what counts,
the classification precedence) and backend/outcomes.py (outcome changes as
execution lifecycle actions). Everything is scoped to the authenticated
user: placements, tasks and executions are read with the user's id, never
an id from the request body.
"""

from __future__ import annotations

import uuid
from datetime import date as date_
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.lifecycle import TaskOutcome
from app.productivity.day_summary import summarize_range
from backend import models
from backend.errors import ApiError
from backend.executions import to_task_execution
from backend.mutations import Mutator
from backend.outcomes import set_placements_outcome
from backend.planning_repository import ServerPlanningRepository
from backend.resources import Strict

#: The longest range one summary request may cover (a year, so a yearly view is still one call).
MAX_SUMMARY_DAYS = 366


class DayOutcomeIn(Strict):
    outcome: TaskOutcome


class DayOutcomeOut(BaseModel):
    date: date_
    outcome: TaskOutcome
    changed: list[uuid.UUID]
    unchanged: list[uuid.UUID]
    #: Cancelled attempts, which are never reopened.
    skipped: list[uuid.UUID]
    summary: dict


def check_range(start_date: date_, end_date: date_) -> list[date_]:
    if end_date < start_date:
        raise ApiError(422, "validation_error", "end_date must not be before start_date.")
    days = (end_date - start_date).days + 1
    if days > MAX_SUMMARY_DAYS:
        raise ApiError(422, "validation_error", f"A summary covers at most {MAX_SUMMARY_DAYS} days.")
    return [start_date + timedelta(days=offset) for offset in range(days)]


def day_summaries(session: Session, user_id: uuid.UUID, clock, start_date: date_, end_date: date_) -> list[dict]:
    """The aggregates of every date in the range, from three queries (placements, tasks, executions)."""
    days = check_range(start_date, end_date)
    repository = ServerPlanningRepository(session, user_id, clock)
    placements = repository.list_placements(start_date, end_date)
    tasks = repository.get_tasks({placement.task_id for placement in placements}, include_deleted=True)
    executions = {}
    ids = [placement.id for placement in placements]
    if ids:
        for row in session.scalars(select(models.Execution).where(
            models.Execution.user_id == user_id,
            models.Execution.scheduled_task_id.in_(ids),
            models.Execution.deleted_at.is_(None),
        )):
            executions[row.scheduled_task_id] = to_task_execution(row)
    summaries = summarize_range(days, placements, tasks, executions)
    return [summaries[day].as_dict() for day in days]


def set_day_outcome(mutator: Mutator, day: date_, request: DayOutcomeIn) -> dict:
    """Every live placement of `day` to the outcome, in the caller's one mutation (one transaction)."""
    repository = ServerPlanningRepository(mutator.session, mutator.user_id, lambda: mutator.now, mutator=mutator)
    placements = repository.list_placements(day, day)
    result = set_placements_outcome(mutator, [placement.id for placement in placements], request.outcome)
    mutator.session.flush()
    [summary] = day_summaries(mutator.session, mutator.user_id, lambda: mutator.now, day, day)
    return DayOutcomeOut(date=day, outcome=request.outcome, changed=list(result.changed),
                         unchanged=list(result.unchanged), skipped=list(result.skipped),
                         summary=summary).model_dump(mode="json")
