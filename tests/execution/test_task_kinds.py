"""Task kinds, preferred thirds of the day, points and completions (schema v16).

- Early / Mid / Late divide the day's own schedulable window into three consecutive thirds, and
  outrank every weaker reward in the day engine without becoming a hard constraint.
- A To Do never enters or leaves the scheduler.
- Flexible, fixed and To Do points persist; completing a fixed block or a To Do is an execution
  that carries those points, with no fabricated schedule for a To Do.
- A fixed block saved before blocks could be completed migrates to 0 points and completed, once.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution import db as db_module
from app.execution.db import backfill_fixed_block_completions, get_connection, initialize_schema
from app.execution.direct_completion import complete_directly, direct_execution, reopen_directly
from app.execution.fixed_block_completion import fixed_block_execution, set_fixed_block_outcome
from app.execution.lifecycle import TaskOutcome
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.optimizer import _best_candidate_for_canonical_task, _best_candidate_for_canonical_task_exhaustive
from app.optimizer import generate_day_schedule
from app.planning.models import (
    TODO_PLACEHOLDER_MINUTES,
    FixedBlock,
    PreferredTime,
    Task,
    TaskKind,
    fixed_block_execution_id,
    preferred_third_bounds,
)
from app.planning.preferences import (
    DayWindowSpec,
    OptimizerMode,
    RewardPreferencesOverride,
    day_preferences_to_reward_settings,
)
from app.planning.repository import PlanningRepository
from tests.test_day_engine import DAY, DAY_START, default_prefs, make_day_schedule, make_fixed, make_task

# -----------------------------------------------------------------------------
# Preferred thirds
# -----------------------------------------------------------------------------


def test_the_schedulable_window_divides_into_three_consecutive_thirds() -> None:
    def thirds(start: int, end: int) -> list[tuple[int, int]]:
        return [preferred_third_bounds(preferred, start, end) for preferred in PreferredTime]

    assert thirds(0, 1440) == [(0, 480), (480, 960), (960, 1440)]  # 00:00-08:00, 08:00-16:00, 16:00-24:00
    assert thirds(480, 1380) == [(480, 780), (780, 1080), (1080, 1380)]  # an 08:00-23:00 day, not midnight's thirds
    uneven = thirds(0, 100)  # not divisible by three: still consecutive, covering the whole window
    assert uneven == [(0, 33), (33, 66), (66, 100)]
    assert all(end == following for (_, end), (following, _) in zip(uneven, uneven[1:]))


def _start(output, task: Task) -> int:
    placement = next(p for p in output.placements if p.task_id == task.id)
    return (placement.planned_start - DAY_START) // timedelta(minutes=1)


def tempting_day(preferred: PreferredTime | None):
    """A late fixed block whose neighbours earn a large relation bonus -- far more than the time bonus."""
    prefs = default_prefs(reward=RewardPreferencesOverride(weight_tag_relation=50.0, weight_fragmentation_penalty=0.0))
    task = make_task("Read", duration=60, category="fixed").model_copy(update={"preferred_time": preferred})
    return task, [make_fixed("Evening class", 1000, 1060)], prefs


def test_the_preferred_third_wins_over_better_secondary_bonuses() -> None:
    plain, fixed, prefs = tempting_day(None)
    # Without a preference the bonus wins: the task goes beside the block (the first minute within reach of it).
    assert _start(generate_day_schedule(make_day_schedule([plain], fixed), prefs), plain) == 820

    early, fixed, prefs = tempting_day(PreferredTime.EARLY)
    assert 0 <= _start(generate_day_schedule(make_day_schedule([early], fixed), prefs), early) <= 480 - 60

    for mode in OptimizerMode:  # every engine keeps it there, the repacking ones included
        output = generate_day_schedule(make_day_schedule([early], fixed), default_prefs(
            optimizer_mode=mode, reward=RewardPreferencesOverride(weight_tag_relation=50.0)))
        assert 0 <= _start(output, early) <= 420, mode

    # The window's own thirds, not midnight's: Late of an 08:00-23:00 day starts at 18:00.
    late = make_task("Review", duration=30).model_copy(update={"preferred_time": PreferredTime.LATE})
    shorter = default_prefs(day_window=DayWindowSpec(start_minute=480, end_minute=1380))
    placement = generate_day_schedule(make_day_schedule([late]), shorter).placements[0]
    assert placement.planned_start >= DAY_START + timedelta(hours=18)


def test_a_full_preferred_third_falls_back_instead_of_leaving_the_task_unscheduled() -> None:
    early = make_task("Read", duration=60).model_copy(update={"preferred_time": PreferredTime.EARLY})
    output = generate_day_schedule(make_day_schedule([early], [make_fixed("Sleep", 0, 480)]), default_prefs())
    assert not output.unscheduled and _start(output, early) == 480  # outside, and as close to its third as it fits


def test_the_event_search_and_its_exhaustive_reference_agree_on_preferred_thirds() -> None:
    prefs = default_prefs()
    for preferred in PreferredTime:
        task = make_task("T", duration=45).model_copy(update={"preferred_time": preferred})
        args = (task, [], 1440, 0, OptimizerMode.PRECISE_GREEDY, 0, day_preferences_to_reward_settings(prefs), prefs,
                None)
        assert _best_candidate_for_canonical_task(*args) == _best_candidate_for_canonical_task_exhaustive(*args)


def test_a_todo_never_enters_or_leaves_the_scheduler(planning_repository: PlanningRepository) -> None:
    todo = Task(name="Buy milk", category="errand", kind=TaskKind.TODO, points=15,
                estimated_duration_minutes=TODO_PLACEHOLDER_MINUTES)
    work = make_task("Read", duration=30)
    output = generate_day_schedule(make_day_schedule([todo, work]), default_prefs())
    assert [p.task_id for p in output.placements] == [work.id]
    assert not output.unscheduled and todo.id not in output.tasks

    # ...and the stored ranges the planner reads never contain it.
    planning_repository.insert_task(todo)
    planning_repository.insert_task(work)
    assert [task.id for task in planning_repository.list_tasks_eligible_for_range(DAY, DAY)] == [work.id]
    assert [task.id for task in planning_repository.list_tasks_planned_in_range(DAY, DAY)] == [work.id]
    with pytest.raises(ValueError, match="does not repeat"):
        Task.model_validate({**todo.model_dump(), "recurrence": {"frequency": "daily"}})


# -----------------------------------------------------------------------------
# Points and completion
# -----------------------------------------------------------------------------


@pytest.fixture
def service(repository: ExecutionRepository) -> ExecutionService:
    return ExecutionService(repository, clock=lambda: datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc))


def test_points_of_every_kind_persist_and_completion_awards_them(
    planning_repository: PlanningRepository, service: ExecutionService, connection: sqlite3.Connection,
) -> None:
    flexible = make_task("Read", duration=30).model_copy(update={"points": 40, "preferred_time": PreferredTime.MID})
    todo = Task(name="Buy milk", category="errand", kind=TaskKind.TODO, points=15,
                estimated_duration_minutes=TODO_PLACEHOLDER_MINUTES)
    block = make_fixed("Class", 540, 600).model_copy(update={"points": 30})
    planning_repository.insert_task(flexible)
    planning_repository.insert_task(todo)
    planning_repository.insert_fixed_block(block)

    reread = PlanningRepository(connection)  # a fresh read of what was stored
    assert (reread.get_task(flexible.id).points, reread.get_task(flexible.id).preferred_time) == (40, PreferredTime.MID)
    assert (reread.get_task(todo.id).points, reread.get_task(todo.id).kind) == (15, TaskKind.TODO)
    assert reread.get_fixed_blocks([block.id])[block.id].points == 30

    # A To Do: a completed execution worth its points, with nothing about a schedule made up.
    done = complete_directly(service, todo)
    assert (done.status, done.points, done.planned_duration) == (ExecutionStatus.COMPLETED, 15, 0)
    assert (done.scheduled_task_id, done.canonical_planned_date, done.canonical_planned_start,
            done.canonical_planned_end, done.planned_start) == (None, None, None, None, None)
    assert complete_directly(service, todo).id == done.id  # again: the same completion, no second award
    assert reopen_directly(service, todo.id).status == ExecutionStatus.SCHEDULED  # incomplete stays incomplete
    assert direct_execution(service, todo.id).status == ExecutionStatus.SCHEDULED

    # A fixed block: pending until answered; its one execution carries its points and its own real times.
    assert fixed_block_execution(service, block.id) is None
    completed = set_fixed_block_outcome(service, block, TaskOutcome.COMPLETED)
    assert (completed.id, completed.status, completed.points) == (
        str(fixed_block_execution_id(block.id)), ExecutionStatus.COMPLETED, 30)
    assert (completed.canonical_planned_start, completed.canonical_planned_end, completed.planned_duration) == (
        block.planned_start, block.planned_end, 60)
    assert set_fixed_block_outcome(service, block, TaskOutcome.COMPLETED).version == completed.version  # idempotent
    assert set_fixed_block_outcome(service, block, TaskOutcome.UNCOMPLETED).status == ExecutionStatus.SKIPPED
    assert set_fixed_block_outcome(service, block, TaskOutcome.PENDING).status == ExecutionStatus.SCHEDULED
    assert len([e for e in service.list_executions() if e.task_name == "Class"]) == 1

    # The productivity system counts exactly the completed ones' points.
    set_fixed_block_outcome(service, block, TaskOutcome.COMPLETED)
    complete_directly(service, todo)
    earned = sum(e.points for e in service.list_executions(ExecutionStatus.COMPLETED))
    assert earned == 15 + 30


# -----------------------------------------------------------------------------
# Migration of fixed blocks saved before they had points and completion
# -----------------------------------------------------------------------------


def _insert_old_block(connection: sqlite3.Connection, block: FixedBlock) -> None:
    connection.execute(
        "INSERT INTO fixed_blocks (id, user_id, label, category, planned_date, timezone, planned_start, planned_end, "
        "planned_start_utc, planned_end_utc, created_at, updated_at, version) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (str(block.id), block.label, block.category, block.planned_date.isoformat(), block.timezone,
         block.planned_start.isoformat(), block.planned_end.isoformat(), block.planned_start.isoformat(),
         block.planned_end.isoformat(), block.created_at.isoformat(), block.updated_at.isoformat()))


def test_old_fixed_blocks_migrate_to_zero_points_and_completed_exactly_once(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(str(db_path), isolation_level=None)
    initialize_schema(old, target_version=15)
    sleep, gym = make_fixed("Sleep", 0, 420), make_fixed("Gym", 1020, 1080)
    for block in (sleep, gym):
        _insert_old_block(old, block)
    dirty_before = old.execute("SELECT COUNT(*) FROM sync_dirty").fetchone()[0]
    old.close()

    connection = get_connection(db_path)  # the migrating open
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db_module.LATEST_SCHEMA_VERSION >= 16
        planning, executions = PlanningRepository(connection), ExecutionRepository(connection)
        assert {b.points for b in planning.get_fixed_blocks([sleep.id, gym.id]).values()} == {0}
        for block in (sleep, gym):
            execution = executions.get_execution(str(fixed_block_execution_id(block.id)))
            assert (execution.status, execution.points, execution.task_name) == (ExecutionStatus.COMPLETED, 0, block.label)
            assert (execution.canonical_planned_start, execution.actual_final_end_at) == (
                block.planned_start, block.planned_end)
        assert connection.execute("SELECT COUNT(*) FROM sync_dirty").fetchone()[0] == dirty_before  # nothing queued

        # Again: nothing is added or changed -- not even a block whose state the user changed since.
        service = ExecutionService(executions)
        reopened = set_fixed_block_outcome(service, gym, TaskOutcome.PENDING)
        new = make_fixed("New class", 600, 660).model_copy(update={"points": 25})
        planning.insert_fixed_block(new)
        set_fixed_block_outcome(service, new, TaskOutcome.UNCOMPLETED)
        before = connection.execute("SELECT * FROM executions ORDER BY id").fetchall()
        assert backfill_fixed_block_completions(connection) == 0
        assert [tuple(row) for row in connection.execute("SELECT * FROM executions ORDER BY id")] == [
            tuple(row) for row in before]
        assert executions.get_execution(reopened.id).status == ExecutionStatus.SCHEDULED
        assert planning.get_fixed_blocks([new.id])[new.id].points == 25  # explicit points are never overwritten
    finally:
        connection.close()
    reopened_connection = get_connection(db_path)  # a plain reopen migrates nothing more
    try:
        assert reopened_connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == len(before)
    finally:
        reopened_connection.close()
