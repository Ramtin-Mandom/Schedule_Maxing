"""Milestone 5 in direct PostgreSQL mode (the server schema; PostgreSQL with
BACKEND_TESTS_ON_POSTGRES=1): plan and optimize, view without writing, work with a
pause, skip, cancel, leave overdue and move through the desktop controllers, then sign
out and in again and read the same outcomes, sessions, lineage and known-answer
analytics from the database."""

from __future__ import annotations

from datetime import date, timedelta

from app.planning.application import RangeScope
from app.planning.models import Task
from app.ui.execution_controller import ExecutionController
from app.ui.planning_controller import PlanningController
from app.ui.productivity_controller import ProductivityController
from backend import models
from backend.database import session_factory
from tests.direct.conftest import PASSWORD, account

MON = date(2026, 3, 2)


def ok(result):
    assert result.ok, result.error
    return result.value


def controllers(session, clock, project_root):
    planning = PlanningController(service=session.planning_service(), timezone="UTC", project_root=project_root)
    executions = ExecutionController(session.execution_service(), sync_state=lambda _id: "server", clock=clock)
    productivity = ProductivityController(session.productivity_service("UTC"), executions, storage="server", clock=clock)
    return planning, executions, productivity


def summary(report) -> tuple:
    return (report.occurrence_count, (report.due_completion.numerator, report.due_completion.denominator),
            report.due_skip.numerator, report.due_outcomes.cancelled, report.due_outcomes.overdue_unattempted,
            report.reschedules.reschedule_events, report.duration.median_signed_error_minutes)


def test_the_direct_workflow_survives_signing_out_and_in(backend, engine, clock, tmp_path) -> None:
    alice = account(backend, "alice@example.com")
    planning, executions, productivity = controllers(alice, clock, str(tmp_path))
    tasks = {name: ok(planning.add_or_update_task(Task(user_id=alice.user_id, name=name, category="study",
                                                       estimated_duration_minutes=60, priority=5, required_date=MON)))
             for name in ("Finish", "Skip", "Cancel", "Untouched", "Move")}
    ok(planning.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE))
    service = alice.planning_service()
    placements = {p.task_id: p for p in service.placements_for_date(MON)}
    for task in tasks.values():
        ok(executions.describe(placements[task.id], None))
    with session_factory(engine)() as session:
        assert session.query(models.Execution).count() == 0  # viewing wrote nothing

    finish = placements[tasks["Finish"].id]
    clock.now = finish.planned_start
    started = ok(executions.perform(tasks["Finish"], finish, "start", None))
    clock.advance(minutes=20)
    paused = ok(executions.perform(tasks["Finish"], finish, "pause", started))
    clock.advance(minutes=10)
    resumed = ok(executions.perform(tasks["Finish"], finish, "resume", paused))
    clock.advance(minutes=20)
    ok(executions.perform(tasks["Finish"], finish, "complete", resumed))
    ok(executions.perform(tasks["Skip"], placements[tasks["Skip"].id], "skip", None))
    ok(executions.perform(tasks["Cancel"], placements[tasks["Cancel"].id], "cancel", None))
    move = placements[tasks["Move"].id]
    latest = max(p.planned_end for p in placements.values())
    target = latest + timedelta(hours=1)
    ok(planning.reschedule_placement(move.id, expected_version=move.version, planned_date=MON,
                                     start_minute=target.hour * 60 + target.minute, duration_minutes=60))
    ok(planning.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE))  # regenerate with history protected

    clock.now = latest + timedelta(hours=6)
    expected = (5, (1, 4), 1, 1, 2, 1, -20.0)
    assert summary(ok(productivity.build_schedule_cohort_report(MON, MON))) == expected
    assert productivity.storage_copy().summary.startswith("Execution history is stored in the server database")

    alice.sign_out()
    again = backend.sign_in(email="alice@example.com", password=PASSWORD)
    planning, executions, productivity = controllers(again, clock, str(tmp_path))
    assert summary(ok(productivity.build_schedule_cohort_report(MON, MON))) == expected
    restored = ok(executions.find_execution_for_placement(finish.id))
    assert restored.status.value == "completed" and restored.actual_active_duration_minutes == 40.0
    assert len(again.execution_service().list_sessions(restored.id)) == 2
    original = again.planning_service().get_placement(move.id, include_deleted=True)
    assert original.removal_reason.value == "rescheduled" and original.planned_start == move.planned_start
    view = ok(executions.describe(finish, restored))
    assert view.sync_text == "Saved on the server." and view.status_text == "Completed"
