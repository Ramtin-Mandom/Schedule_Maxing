"""Milestone 5 end to end through the desktop controllers and two devices, against the
FastAPI backend (PostgreSQL with BACKEND_TESTS_ON_POSTGRES=1 and a disposable
TEST_DATABASE_URL, else SQLite): plan and optimize, look without writing, work with a
pause across a restart, skip / cancel / leave overdue / move, regenerate, then compare
outcomes, sessions, lineage and known-answer analytics on a second device and after
signing out and in again. Offline work stays visibly pending until the server confirms
it, and a conflict never disappears by itself."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import RangeScope
from app.productivity.reporting import ProductivityService
from app.ui.execution_controller import ExecutionController
from tests.sync.conftest import MON, InProcessTransport

EMAIL = "alice@example.com"
NAMES = ("Finish", "Skip", "Cancel", "Untouched", "Move")


class Clock:
    """The execution clock: the work happened on MON (the plan's date), before today."""

    def __init__(self) -> None:
        self.now = datetime(MON.year, MON.month, MON.day, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def ok(result):
    assert result.ok, result.error
    return result.value


def executions(device, clock) -> ExecutionController:
    return ExecutionController(ExecutionService(ExecutionRepository(device.connection), clock),
                               sync_state=lambda execution_id: device.sync.record_sync_state("execution", execution_id),
                               clock=clock)


def report(device):
    service = ProductivityService(ExecutionRepository(device.connection), history=device.planning, timezone_name="UTC")
    return service.build_schedule_cohort_report(start_date=MON, end_date=MON)


def summary(result) -> dict:
    return {
        "occurrences": result.occurrence_count, "completion": (result.due_completion.numerator,
                                                               result.due_completion.denominator),
        "skip": (result.due_skip.numerator, result.due_skip.denominator), "cancelled": result.due_outcomes.cancelled,
        "unattempted": result.due_outcomes.overdue_unattempted, "moves": result.reschedules.reschedule_events,
        "moved": result.reschedules.reschedule_rate.numerator,
        "duration": (result.duration.pairs, result.duration.median_signed_error_minutes),
    }


def placement_of(device, task_id):
    return next(p for p in device.planning.placements_for_date(MON) if p.task_id == task_id)


def test_the_milestone_workflow_across_restart_devices_and_sign_in(alice_server, make_device) -> None:
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in(EMAIL)
    clock = Clock()

    tasks = {name: a.add_task(name, required_date=MON) for name in NAMES}
    ok(a.controller.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE))
    ctl = executions(a, clock)
    for task in tasks.values():  # looking at every item writes nothing
        placement = placement_of(a, task.id)
        assert ok(ctl.describe(placement, ok(ctl.find_execution_for_placement(placement.id)))).status_text == "Not started"
    assert a.connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0

    finish = placement_of(a, tasks["Finish"].id)
    clock.now = finish.planned_start
    started = ok(ctl.perform(tasks["Finish"], finish, "start", None))
    clock.now += timedelta(minutes=20)
    paused = ok(ctl.perform(tasks["Finish"], finish, "pause", started))
    a.reopen()  # the app restarts while the work is paused
    ctl = executions(a, clock)
    assert ok(ctl.find_execution_for_placement(finish.id)) == paused
    clock.now += timedelta(minutes=10)
    resumed = ok(ctl.perform(tasks["Finish"], finish, "resume", paused))
    clock.now += timedelta(minutes=20)
    done = ok(ctl.perform(tasks["Finish"], finish, "complete", resumed))
    assert done.actual_active_duration_minutes == 40.0

    ok(ctl.perform(tasks["Skip"], placement_of(a, tasks["Skip"].id), "skip", None))
    ok(ctl.perform(tasks["Cancel"], placement_of(a, tasks["Cancel"].id), "cancel", None))
    move = placement_of(a, tasks["Move"].id)
    latest = max(p.planned_end for p in a.planning.placements_for_date(MON))
    target = latest + timedelta(minutes=60)
    ok(a.controller.reschedule_placement(move.id, expected_version=move.version, planned_date=MON,
                                         start_minute=target.hour * 60 + target.minute, duration_minutes=60))
    assert "not yet confirmed" in ok(ctl.describe(finish, done)).sync_text  # pending until the server confirms

    ok(a.controller.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE))  # regenerate: history protected
    assert placement_of(a, tasks["Finish"].id).id == finish.id
    expected = {"occurrences": 5, "completion": (1, 4), "skip": (1, 4), "cancelled": 1, "unattempted": 2,
                "moves": 1, "moved": 1, "duration": (1, -20.0)}
    assert summary(report(a)) == expected  # moves and regeneration never multiply the occurrences

    assert a.sync_now().status == "ok"
    assert ok(ctl.describe(finish, ok(ctl.get_execution(done.id)))).sync_text == "Confirmed by the server."
    b.sign_in(EMAIL)
    assert b.sync_now().status == "ok"
    assert summary(report(b)) == expected
    remote = b.executions.find_execution_for_placement(finish.id)
    assert [(s.started_at, s.ended_at) for s in b.executions.list_sessions(remote.id)] == [
        (s.started_at, s.ended_at) for s in a.executions.list_sessions(done.id)]
    original_move = b.planning.get_placement(move.id, include_deleted=True)
    assert original_move.removal_reason.value == "rescheduled" and original_move.planned_start == move.planned_start

    # A conflict: B starts the untouched work offline while A skips it; it stays until resolved.
    untouched = placement_of(a, tasks["Untouched"].id)
    b_ctl = executions(b, clock)
    ok(b_ctl.perform(b.planning.get_task(tasks["Untouched"].id), untouched, "start", None))
    ok(ctl.perform(tasks["Untouched"], untouched, "skip", None))
    assert a.sync_now().status == "ok"
    b.sync_now()
    b.sync_now()
    assert len(b.sync.list_conflicts()) >= 1  # still there after another synchronization
    assert b.executions.find_execution_for_placement(untouched.id).status.value == "in_progress"  # not overwritten
    for conflict in b.sync.list_conflicts():
        b.sync.resolve_conflict(conflict.id, "accept_remote")
    assert b.sync_now().status == "ok" and b.sync.list_conflicts() == []
    assert b.executions.find_execution_for_placement(untouched.id).status.value == "skipped"

    after_skip = {**expected, "skip": (2, 4), "unattempted": 1}
    assert summary(report(b)) == after_skip
    a.sync.sign_out()
    a.reopen(keep_session=False)
    a.sign_in(EMAIL)
    assert a.sync_now().status == "ok"
    assert summary(report(a)) == after_skip  # signing out and in again restores the same history
