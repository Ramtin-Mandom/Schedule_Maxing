"""Execution history across devices (Milestone 5): sync state distinguishes
pending local work, server-confirmed work, and conflicts, and a second
device (or the same one after signing out and in again) restores the same outcomes,
sessions, lineage and schedule-cohort answers from the server."""

from __future__ import annotations

from datetime import datetime, timezone

from app.planning import workflow
from app.planning.models import ScheduledTask
from app.productivity.reporting import ProductivityService
from app.execution.repository import ExecutionRepository
from tests.sync.conftest import MON, InProcessTransport

EMAIL = "alice@example.com"


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(MON.year, MON.month, MON.day, hour, minute, tzinfo=timezone.utc)


def cohort(device):
    service = ProductivityService(ExecutionRepository(device.connection), history=device.planning, timezone_name="UTC")
    return service.build_schedule_cohort_report(start_date=MON, end_date=MON)


def planned(device, name: str, hour: int):
    task = device.add_task(name, required_date=MON)
    placement = ScheduledTask(task_id=task.id, user_id=task.user_id, planned_date=MON, timezone="UTC",
                              planned_start=at(hour), planned_end=at(hour + 1))
    existing = device.planning.placements_for_date(MON)
    device.planning.replace_placements(MON, MON, [*existing, placement],
                                       expected_versions={p.id: p.version for p in existing})
    return task, device.planning.get_placement(placement.id)


def test_sync_state_and_a_second_device_restores_the_same_history(alice_server, make_device) -> None:
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    offline = make_device("offline", InProcessTransport(alice_server.client))
    task, placement = planned(offline, "Alone", 9)
    execution = offline.executions.get_or_create_canonical_execution(task, placement)
    assert offline.sync.record_sync_state("execution", execution.id) == "local_only"

    a.sign_in(EMAIL)
    task, placement = planned(a, "Study", 9)
    assert a.sync_now().status == "ok"  # the original plan reaches the server before it is moved
    moved = workflow.reschedule_placement(a.planning, placement.id, expected_version=placement.version,
                                          planned_date=MON, timezone_name="UTC", planned_start=at(13),
                                          planned_end=at(14)).replacement
    attempt = a.executions.get_or_create_canonical_execution(task, moved)
    started = a.executions.start(attempt.id, expected_version=attempt.version)
    done = a.executions.complete(started.id, expected_version=started.version)
    assert a.sync.record_sync_state("execution", done.id) == "pending"
    assert a.sync_now().status == "ok"
    assert a.sync.record_sync_state("execution", done.id) == "synced"

    b.sign_in(EMAIL)
    assert b.sync_now().status == "ok"
    restored = b.executions.find_execution_for_placement(moved.id)
    assert restored.status == done.status and restored.actual_active_duration_minutes == \
        a.executions.get_execution(done.id).actual_active_duration_minutes
    assert [(s.started_at, s.ended_at) for s in b.executions.list_sessions(restored.id)] == [
        (s.started_at, s.ended_at) for s in a.executions.list_sessions(done.id)]
    assert b.planning.get_placement(placement.id, include_deleted=True).superseded_by_id == moved.id
    projection = lambda report: (report.due_completion.model_dump(), report.reschedules.model_dump(),  # noqa: E731
                                 report.duration.model_dump(), [o.state for o in report.occurrences])
    assert projection(cohort(b)) == projection(cohort(a))
    assert b.sync.record_sync_state("execution", restored.id) == "synced"

    # Sign out and in again on A after a restart: the same server-confirmed history is there.
    a.sync.sign_out()
    a.reopen(keep_session=False)
    a.sign_in(EMAIL)
    assert a.sync_now().status == "ok"
    assert projection(cohort(a)) == projection(cohort(b))


def test_a_conflicting_execution_stays_in_conflict_until_resolved(alice_server, make_device) -> None:
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in(EMAIL)
    b.sign_in(EMAIL)
    task, placement = planned(a, "Race", 9)
    execution = a.executions.get_or_create_canonical_execution(task, placement)
    a.sync_now()
    b.sync_now()
    a.executions.start(execution.id)
    b.executions.start(execution.id)
    a.sync_now()
    b.sync_now()
    assert b.sync.record_sync_state("execution", execution.id) == "conflict"
    [conflict] = b.sync.list_conflicts()
    b.sync.resolve_conflict(conflict.id, "accept_remote")
    b.sync_now()
    assert b.sync.record_sync_state("execution", execution.id) == "synced"
    assert len(b.executions.list_sessions(execution.id)) == 1
