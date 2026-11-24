"""Milestone 5 in direct PostgreSQL mode (the server schema; PostgreSQL with
BACKEND_TESTS_ON_POSTGRES=1): plan and optimize, view without writing, work with a
pause, skip, cancel, leave overdue and move through the shared services, then sign
out and in again and read the same outcomes, sessions, lineage and known-answer
analytics from the database."""

from __future__ import annotations

from datetime import date, timedelta

from app.planning.application import RangeScope
from app.planning.models import Task
from app.ui.planning_controller import PlanningController
from backend import models
from backend.database import session_factory
from tests.direct.conftest import PASSWORD, account

MON = date(2026, 3, 2)


def ok(result):
    assert result.ok, result.error
    return result.value


def services(session, project_root):
    planning = PlanningController(service=session.planning_service(), timezone="UTC", project_root=project_root)
    return planning, session.execution_service(), session.productivity_service("UTC")


def summary(report) -> tuple:
    return (report.occurrence_count, (report.due_completion.numerator, report.due_completion.denominator),
            report.due_skip.numerator, report.due_outcomes.cancelled, report.due_outcomes.overdue_unattempted,
            report.reschedules.reschedule_events, report.duration.median_signed_error_minutes)


def test_the_direct_workflow_survives_signing_out_and_in(backend, engine, clock, tmp_path) -> None:
    alice = account(backend, "alice@example.com")
    planning, executions, productivity = services(alice, str(tmp_path))
    tasks = {name: ok(planning.add_or_update_task(Task(user_id=alice.user_id, name=name, category="study",
                                                       estimated_duration_minutes=60, required_date=MON)))
             for name in ("Finish", "Skip", "Cancel", "Untouched", "Move")}
    ok(planning.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE))
    service = alice.planning_service()
    placements = {p.task_id: p for p in service.placements_for_date(MON)}
    for task in tasks.values():
        assert executions.find_execution_for_placement(placements[task.id].id) is None
    with session_factory(engine)() as session:
        assert session.query(models.Execution).count() == 0  # viewing wrote nothing

    finish = placements[tasks["Finish"].id]
    clock.now = finish.planned_start
    attempt = executions.get_or_create_canonical_execution(tasks["Finish"], finish)
    started = executions.start(attempt.id, expected_version=attempt.version)
    clock.advance(minutes=20)
    paused = executions.pause(started.id, expected_version=started.version)
    clock.advance(minutes=10)
    resumed = executions.resume(paused.id, expected_version=paused.version)
    clock.advance(minutes=20)
    executions.complete(resumed.id, expected_version=resumed.version)
    skipped = executions.get_or_create_canonical_execution(tasks["Skip"], placements[tasks["Skip"].id])
    executions.skip(skipped.id, expected_version=skipped.version)
    cancelled = executions.get_or_create_canonical_execution(tasks["Cancel"], placements[tasks["Cancel"].id])
    executions.cancel(cancelled.id, expected_version=cancelled.version)
    move = placements[tasks["Move"].id]
    latest = max(p.planned_end for p in placements.values())
    target = latest + timedelta(hours=1)
    ok(planning.reschedule_placement(move.id, expected_version=move.version, planned_date=MON,
                                     start_minute=target.hour * 60 + target.minute, duration_minutes=60))
    ok(planning.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE))  # regenerate with history protected

    clock.now = latest + timedelta(hours=6)
    expected = (5, (1, 4), 1, 1, 2, 1, -20.0)
    assert summary(productivity.build_schedule_cohort_report(start_date=MON, end_date=MON)) == expected

    alice.sign_out()
    again = backend.sign_in(email="alice@example.com", password=PASSWORD)
    _, executions, productivity = services(again, str(tmp_path))
    assert summary(productivity.build_schedule_cohort_report(start_date=MON, end_date=MON)) == expected
    restored = executions.find_execution_for_placement(finish.id)
    assert restored.status.value == "completed" and restored.actual_active_duration_minutes == 40.0
    assert len(again.execution_service().list_sessions(restored.id)) == 2
    original = again.planning_service().get_placement(move.id, include_deleted=True)
    assert original.removal_reason.value == "rescheduled" and original.planned_start == move.planned_start
