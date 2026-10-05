"""The Execute tab's Tk-free rules (app/ui/execution_workflow.py, ExecutionController.perform,
PlanningController.reschedule_placement) and the Productivity page's controller additions
(schedule cohort, history, storage wording): legal actions per state, derived overdue and
late states, explanations for refused moves and conflicts, no writes on viewing, no
duplicates on repeated actions, and analytics that follow the persisted outcome."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.models import ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.productivity.reporting import ProductivityService
from app.ui.execution_controller import ExecutionController
from app.ui.execution_workflow import RESCHEDULE_BLOCKED, describe_item
from app.ui.planning_controller import PlanningController
from app.ui.productivity_controller import ProductivityController

TZ = "America/Vancouver"
MON = date(2026, 3, 2)


def at(hour: int, minute: int = 0) -> datetime:  # local Vancouver time on MON (UTC-8)
    return datetime(2026, 3, 2, tzinfo=timezone.utc) + timedelta(hours=hour + 8, minutes=minute)


class Clock:
    def __init__(self) -> None:
        self.now = at(7)

    def __call__(self) -> datetime:
        return self.now


class Stack:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        self.connection = get_connection(tmp_path / "app.db")
        self.planning_service = PlanningService(PlanningRepository(self.connection), self.clock)
        self.execution_service = ExecutionService(ExecutionRepository(self.connection), self.clock)
        self.planning = PlanningController(service=self.planning_service, timezone=TZ, project_root=str(tmp_path))
        self.executions = ExecutionController(self.execution_service, clock=self.clock)
        self.productivity = ProductivityController(
            ProductivityService(ExecutionRepository(self.connection), clock=self.clock, history=self.planning_service,
                                timezone_name=TZ),
            self.executions, clock=self.clock)

    def planned(self, name: str = "Study", hour: int = 9, minutes: int = 60):
        task = self.planning_service.create_task(Task(name=name, category="study", estimated_duration_minutes=minutes,
                                                      priority=5))
        placement = ScheduledTask(task_id=task.id, planned_date=MON, timezone=TZ, planned_start=at(hour),
                                  planned_end=at(hour) + timedelta(minutes=minutes), created_at=self.clock.now,
                                  updated_at=self.clock.now)
        existing = self.planning_service.placements_for_date(MON)
        self.planning_service.replace_placements(MON, MON, [*existing, placement],
                                                 expected_versions={p.id: p.version for p in existing})
        return task, self.planning_service.get_placement(placement.id)

    def count(self, table: str) -> int:
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def value(self, result):
        assert result.ok, result.error
        return result.value


@pytest.fixture
def stack(tmp_path: Path):
    s = Stack(tmp_path)
    yield s
    s.connection.close()


def test_legal_actions_and_derived_states(stack: Stack) -> None:
    task, placement = stack.planned()
    upcoming = describe_item(placement, None, now=at(8))
    assert upcoming.actions == ("start", "complete", "skip", "cancel", "reschedule")  # done without timing too
    assert upcoming.timing_text == "Upcoming."
    assert "America/Vancouver" in upcoming.planned_text and "60 min estimate" in upcoming.planned_text
    overdue = describe_item(placement, None, now=at(11))
    assert overdue.overdue and overdue.status_text == "Not started" and overdue.timing_text.startswith("Overdue")

    execution = stack.execution_service.get_or_create_canonical_execution(task, placement)
    stack.clock.now = at(9, 5)
    running = stack.execution_service.start(execution.id)
    late = describe_item(placement, running, now=at(10, 30))
    assert late.late and not late.overdue and late.status_text == "In progress"  # keeps its lifecycle state
    assert "by 30 min" in late.timing_text and late.actions == ("pause", "complete", "skip", "cancel")
    assert late.reschedule_blocked == RESCHEDULE_BLOCKED and "First started 09:05" in late.actual_text

    stack.clock.now = at(9, 25)
    paused = stack.execution_service.pause(execution.id)
    assert describe_item(placement, paused, now=at(9, 30)).actions == ("resume", "complete", "skip", "cancel")
    stack.clock.now = at(9, 40)
    stack.execution_service.resume(execution.id)
    stack.clock.now = at(9, 50)
    done = stack.execution_service.complete(execution.id)
    finished = describe_item(placement, done, now=at(12))
    assert finished.actions == () and finished.reschedule_blocked == RESCHEDULE_BLOCKED
    assert finished.active_text == "Active time: 30 min (pauses excluded)."
    assert "ended 09:50 (completed)" in finished.actual_text


def test_viewing_writes_nothing_and_repeated_actions_never_duplicate(stack: Stack) -> None:
    task, placement = stack.planned()
    assert stack.value(stack.executions.find_execution_for_placement(placement.id)) is None
    stack.value(stack.executions.describe(placement, None))
    assert stack.count("executions") == 0 and stack.count("sync_dirty") == 3  # only the task, its type and the placement

    stack.clock.now = at(9)
    started = stack.value(stack.executions.perform(task, placement, "start", None))
    second = stack.executions.perform(task, placement, "start", None)  # a second click from the same view
    assert not second.ok and "not possible any more" in second.error
    stale = stack.executions.perform(task, placement, "pause", started.model_copy(update={"version": 1}))
    assert not stale.ok and "changed elsewhere" in stale.error
    assert stack.count("executions") == 1 and stack.count("work_sessions") == 1
    assert stack.value(stack.executions.get_execution(started.id)).status == ExecutionStatus.IN_PROGRESS


def test_cancel_and_skip_work_on_a_placement_without_an_execution(stack: Stack) -> None:
    task_a, first = stack.planned("A", 9)
    task_b, second = stack.planned("B", 11)
    cancelled = stack.value(stack.executions.perform(task_a, first, "cancel", None))
    skipped = stack.value(stack.executions.perform(task_b, second, "skip", None, feedback={"note": "not today"}))
    assert cancelled.status == ExecutionStatus.CANCELLED and skipped.note == "not today"


def test_reschedule_moves_unstarted_work_and_explains_refusals(stack: Stack) -> None:
    task, placement = stack.planned("Move me", 9)
    moved = stack.value(stack.planning.reschedule_placement(
        placement.id, expected_version=placement.version, planned_date=MON, start_minute=14 * 60,
        duration_minutes=60))
    assert moved.replacement.planned_start == at(14) and moved.previous.planned_start == at(9)
    stale = stack.planning.reschedule_placement(placement.id, expected_version=placement.version, planned_date=MON,
                                                start_minute=16 * 60, duration_minutes=60)
    assert not stale.ok and "moved, changed or removed elsewhere" in stale.error

    other_task, other = stack.planned("Blocker", 12)
    overlap = stack.planning.reschedule_placement(moved.replacement.id, expected_version=1, planned_date=MON,
                                                  start_minute=12 * 60 + 30, duration_minutes=60)
    assert not overlap.ok and overlap.error.startswith("It cannot be moved there")

    stack.clock.now = at(12)
    stack.value(stack.executions.perform(other_task, other, "start", None))
    protected = stack.planning.reschedule_placement(other.id, expected_version=other.version, planned_date=MON,
                                                    start_minute=17 * 60, duration_minutes=60)
    assert not protected.ok and "stays in history" in protected.error and "in progress" in protected.error


def test_analytics_and_history_follow_the_persisted_actions(stack: Stack) -> None:
    task, placement = stack.planned("Same name", 9)
    duplicate, other = stack.planned("Same name", 11)
    stack.clock.now = at(13)
    before = stack.value(stack.productivity.schedule_cohort_for_last(1))
    assert (before.due_completion.numerator, before.due_completion.denominator) == (0, 2)

    stack.clock.now = at(13, 5)
    started = stack.value(stack.executions.perform(task, placement, "start", None))
    stack.clock.now = at(13, 35)
    stack.value(stack.executions.perform(task, placement, "complete", started))
    after = stack.value(stack.productivity.schedule_cohort_for_last(1))
    assert (after.due_completion.numerator, after.due_completion.denominator) == (1, 2)

    page = stack.value(stack.productivity.history(1))
    assert len(page.entries) == 2 and page.timezone == TZ
    labels = [entry.label for entry in page.entries]
    assert all("[" in label for label in labels) and len(set(labels)) == 2  # duplicate names stay distinct
    done = next(entry for entry in page.entries if entry.status == "completed")
    assert done.execution.actual_active_duration_minutes == 30.0 and len(done.sessions) == 1
    only_overdue = stack.value(stack.productivity.history(1, status="overdue"))
    assert [entry.key for entry in only_overdue.entries] == [other.id]


def test_history_keeps_moved_and_removed_work(stack: Stack) -> None:
    task, placement = stack.planned("Moved", 9)
    removed_task, removed = stack.planned("Removed", 11)
    moved = stack.value(stack.planning.reschedule_placement(
        placement.id, expected_version=placement.version, planned_date=MON, start_minute=15 * 60,
        duration_minutes=60))
    stack.planning_service.delete_task(removed_task.id, expected_version=removed_task.version)
    stack.clock.now = at(18)
    page = stack.value(stack.productivity.history(1))
    by_status = {entry.status: entry for entry in page.entries}
    assert [p.id for p in by_status["overdue"].plan_lineage] == [placement.id, moved.replacement.id]
    assert by_status["removed"].name == "Removed (deleted task)"
    from app.ui.history_model import detail_text

    text = detail_text(by_status["overdue"])
    assert "Original plan: Mon Mar 2 09:00" in text and ": moved" in text and "Times shown in America/Vancouver" in text


@pytest.mark.parametrize(("mode", "phrase"), [("device", "this device only"), ("account", "synchronized"),
                                              ("server", "server database")])
def test_history_wording_follows_the_storage_mode(stack: Stack, mode, phrase) -> None:
    controller = ProductivityController(ProductivityService(ExecutionRepository(stack.connection)), stack.executions,
                                        storage=mode)
    copy = controller.storage_copy()
    assert phrase in copy.summary and "local" not in copy.reset_button.lower()
    assert ("permanently" in copy.reset_message) == (mode == "device")
