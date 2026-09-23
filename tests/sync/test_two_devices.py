"""Two independent desktop databases synchronizing through one in-process backend:
explicit association, offline work, push/pull without echo, tombstones,
relationship ordering, executions with sessions, legacy history, preferences,
categories, and schedule freshness re-evaluated locally."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.planning.application import RangeScope
from app.planning.models import Project
from app.planning.preferences import OptimizerMode, PreferenceOverrides
from app.planning.service import DayResultStatus
from tests.sync.conftest import MON, InProcessTransport

SUN = MON + timedelta(days=6)


@pytest.fixture
def pair(alice_server, make_device):
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    return alice_server, a, b


def test_offline_records_reach_a_second_device_after_explicit_association(pair) -> None:
    server, a, b = pair
    project = a.planning.create_project(Project(name="Thesis"))
    first = a.add_task("Read", project_id=project.id)
    second = a.add_task("Summarize", dependency_ids=[first.id], required_date=MON)
    block = a.add_block()

    a.sign_in("alice@example.com", associate=False)
    assert a.sync_now().status == "ok"
    assert server.get("alice@example.com", "/tasks")["items"] == []  # nothing uploaded without association

    claimed = a.sync.associate_local_data()
    assert claimed["task"] == 2 and claimed["project"] == 1 and claimed["fixed_block"] == 1
    report = a.sync_now()
    assert report.status == "ok" and report.pushed == 4

    b.sign_in("alice@example.com")
    assert b.sync_now().status == "ok"
    assert b.planning.get_task(second.id).dependency_ids == [first.id]
    assert b.planning.get_project(project.id).name == "Thesis"
    assert b.planning.fixed_blocks_for_date(MON)[0].category == "sleep" and block.id
    assert b.dirty() == []  # applying pulled records produced no outbound changes
    assert b.sync_now().pushed == 0


def test_offline_edits_coalesce_and_tombstones_propagate(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    task = a.add_task("Draft")
    a.sync_now()
    b.sync_now()

    stored = a.planning.get_task(task.id)
    for priority in (6, 7, 8):  # three offline edits
        stored = a.planning.update_task(stored.model_copy(update={"priority": priority}), expected_version=stored.version)
    a.sync_now()
    remote = server.get("alice@example.com", f"/tasks/{task.id}")
    assert (remote["priority"], remote["version"]) == (8, 2)  # one update, based on the acknowledged version 1

    b.sync_now()
    assert b.planning.get_task(task.id).priority == 8
    b_task = b.planning.get_task(task.id)
    b.planning.delete_task(task.id, expected_version=b_task.version)
    b.sync_now()
    a.sync_now()
    assert a.planning.get_task(task.id) is None
    assert server.get("alice@example.com", f"/tasks/{task.id}", include_deleted=True)["deleted_at"] is not None

    scratch = a.add_task("Created and deleted offline")
    a.planning.delete_task(scratch.id, expected_version=a.planning.get_task(scratch.id).version)
    a.sync_now()
    assert server.get("alice@example.com", f"/tasks/{scratch.id}").get("error", {}).get("code") == "not_found"


def test_executions_sessions_feedback_and_legacy_history(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    task = a.add_task("Worked on")
    [placement] = a.planning.replace_placements(MON, MON, [
        _placement(task.id, MON, 9),
    ]).placements
    execution = a.executions.get_or_create_canonical_execution(task, placement)
    a.sync_now()

    started = a.executions.start(execution.id)
    paused = a.executions.pause(execution.id, expected_version=started.version)
    resumed = a.executions.resume(execution.id, expected_version=paused.version)
    completed = a.executions.complete(execution.id, expected_version=resumed.version)
    a.executions.record_feedback(execution.id, expected_version=completed.version, focus_rating=4, note="good")
    legacy = a.executions.create_execution(task_name="Old", category="study", tag="", planned_date=1,
                                           planned_start=540, planned_end=600, planned_duration=60, priority=5)
    a.connection.execute("UPDATE executions SET id = 'legacy-7' WHERE id = ?", (legacy.id,))  # a pre-UUID id
    a.connection.execute("INSERT INTO execution_wire_ids (execution_id, wire_id) VALUES "
                         "('legacy-7', '00000000-0000-4000-8000-000000000007')")
    assert a.sync_now().status == "ok"

    remote = server.get("alice@example.com", f"/executions/{execution.id}")
    local_sessions = a.executions.list_sessions(execution.id)
    assert remote["status"] == "completed" and remote["focus_rating"] == 4 and remote["note"] == "good"
    assert [_utc(s["started_at"]) for s in remote["sessions"]] == [_utc(s.started_at) for s in local_sessions]
    legacy_remote = server.get("alice@example.com", "/executions/00000000-0000-4000-8000-000000000007")
    assert legacy_remote["legacy_id"] == "legacy-7"

    b.sign_in("alice@example.com")
    b.sync_now()
    pulled = b.executions.get_execution(execution.id)
    assert pulled.status.value == "completed" and len(b.executions.list_sessions(execution.id)) == 2
    assert b.executions.get_execution("legacy-7").task_name == "Old"  # the legacy id is kept, never reminted


def test_preferences_categories_and_schedule_freshness(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    a.controller.set_engine_mode(OptimizerMode.ADHD_FRIENDLY)
    a.controller.set_date_overrides(MON, PreferenceOverrides(category_multipliers={"study": 2.0, "work": None}))
    a.add_block(category="sleep")
    a.add_task("Study", required_date=MON)
    assert a.controller.schedule_range(MON, SUN, scope=RangeScope.ELIGIBLE).ok
    a.sync_now()

    b.sign_in("alice@example.com")
    b.sync_now()
    assert b.controller.resolve_preferences(MON).value.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
    assert b.controller.date_preferences(MON).value.overrides.category_multipliers == {"study": 2.0, "work": None}
    # B recomputes freshness from its own inputs: identical here, so the synced schedule is current...
    states = b.controller.day_states([MON, MON + timedelta(days=1)]).value
    assert {state.status for state in states.values()} == {DayResultStatus.GENERATED}
    # ...and a local edit on B makes it stale, whatever the other device's record said.
    task = b.planning.list_tasks()[0]
    b.planning.update_task(task.model_copy(update={"priority": 9}), expected_version=task.version)
    assert b.controller.day_state(MON).value.status == DayResultStatus.STALE


def test_rescheduling_cleanup_and_outside_dependencies_sync(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    floating = a.add_task("Floating")
    daily = a.add_task("Daily", recurrence={"frequency": "daily"})
    week = a.controller.schedule_range(MON, SUN, scope=RangeScope.ELIGIBLE).value
    first_day = week.allocation.assignments[floating.id]
    a.sync_now()
    b.sync_now()

    other = MON + timedelta(days=3) if first_day != MON + timedelta(days=3) else MON + timedelta(days=4)
    rerun = b.controller.schedule_range(other, other, scope=RangeScope.ELIGIBLE).value
    assert rerun.superseded_ids  # B moved the floating task; its old placement was superseded
    b.sync_now()
    a.sync_now()
    a_floating = [p for p in a.planning.list_placements() if p.task_id == floating.id]
    assert [p.planned_date for p in a_floating] == [other]  # the cleanup reached A: no double booking
    daily_dates = {p.planned_date for p in a.planning.list_placements() if p.task_id == daily.id}
    assert len(daily_dates) >= 2  # distinct occurrences of a recurring template stay

    # A dependency completed on B and synced satisfies a dependent scheduled next week on A.
    prerequisite = b.add_task("Prerequisite", required_date=MON)
    assert b.controller.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE).ok
    [done] = [p for p in b.planning.placements_for_date(MON) if p.task_id == prerequisite.id]
    execution = b.executions.get_or_create_canonical_execution(prerequisite, done)
    b.executions.start(execution.id)
    completed = b.executions.complete(execution.id)
    b.sync_now()
    a.sync_now()
    finished = completed.actual_final_end_at.date()  # the real completion instant (the device clock)
    next_monday = finished + timedelta(days=7 - finished.weekday())
    dependent = a.add_task("Dependent", required_date=next_monday, dependency_ids=[prerequisite.id])
    run = a.controller.schedule_range(next_monday, next_monday + timedelta(days=6), scope=RangeScope.ELIGIBLE).value
    assert run.allocation.assignments.get(dependent.id) == next_monday


def _placement(task_id, day: date, hour: int):
    from app.planning.models import ScheduledTask

    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return ScheduledTask(task_id=task_id, planned_date=day, timezone="UTC", planned_start=start,
                         planned_end=start + timedelta(hours=1))


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
