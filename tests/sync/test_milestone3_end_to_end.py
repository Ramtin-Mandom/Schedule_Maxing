"""Milestone 3 end to end: two desktop devices (separate temporary SQLite
databases) and one in-process backend, through a complete workflow --
registration and explicit association, every record type, offline work and
restarts, idempotent retries, stale writes and explicit conflict resolution,
deletion without resurrection, stable ids and CSV round trips, schedule
freshness across restart and sync, rescheduling cleanup, outside-range
dependencies, distinct occurrences, rollback, and cross-user isolation."""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path

import pytest

from app.planning.application import RangeScope
from app.planning.csv_canonical import parse_canonical_csv
from app.planning.csv_export import export_planning_csv
from app.planning.models import Project
from app.planning.preferences import OptimizerMode, PreferenceOverrides
from app.planning.service import DayResultStatus
from tests.sync.conftest import MON, PASSWORD, FlakyTransport, InProcessTransport

SUN = MON + timedelta(days=6)


def _ids(path: Path) -> set[tuple[str, str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return {(row["record_type"], row["id"], row["version"]) for row in csv.DictReader(file)}


def test_two_devices_complete_workflow(server, make_device, tmp_path, monkeypatch) -> None:
    server.register("alice@example.com")
    laptop = make_device("laptop", FlakyTransport(server.client))
    phone = make_device("phone", InProcessTransport(server.client))

    # 1. Offline work before any account: every record type.
    project = laptop.planning.create_project(Project(name="Thesis"))
    prerequisite = laptop.add_task("Read papers", project_id=project.id, required_date=MON)
    writing = laptop.add_task("Write", dependency_ids=[prerequisite.id], required_date=MON + timedelta(days=1))
    floating = laptop.add_task("Floating")
    daily = laptop.add_task("Daily review", recurrence={"frequency": "daily"})
    laptop.add_block(category="sleep")
    laptop.controller.set_engine_mode(OptimizerMode.ADHD_FRIENDLY)
    laptop.controller.set_date_overrides(MON, PreferenceOverrides(category_multipliers={"study": 1.5}))
    week = laptop.controller.schedule_range(MON, SUN, scope=RangeScope.ELIGIBLE).value
    [placement] = [p for p in laptop.planning.placements_for_date(MON) if p.task_id == prerequisite.id]
    execution = laptop.executions.get_or_create_canonical_execution(prerequisite, placement)
    started = laptop.executions.start(execution.id)
    laptop.executions.pause(execution.id, expected_version=started.version)

    # 2. Register/sign in, and upload only after the explicit association; a lost response is retried.
    laptop.sign_in("alice@example.com", associate=False)
    assert laptop.sync_now().pushed == 0
    laptop.sync.associate_local_data()
    laptop.transport.lose_push_response = 1
    assert laptop.sync_now().status == "offline"
    laptop.reopen()  # restart with the operations still pending in the durable outbox
    assert laptop.sync_now().status == "ok"
    server_log = server.changes("alice@example.com")
    assert len({(c["entity_type"], c["entity_id"]) for c in server_log}) == len(server_log)  # nothing twice

    # 3. The second device pulls everything; its schedule is current by its own recomputation.
    phone.sign_in("alice@example.com")
    phone.sync_now()
    assert phone.planning.get_task(writing.id).dependency_ids == [prerequisite.id]
    assert phone.executions.get_execution(execution.id).status.value == "paused"
    assert phone.controller.resolve_preferences(MON).value.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
    assert phone.planning.fixed_blocks_for_date(MON)[0].category == "sleep"
    assert phone.controller.day_state(MON).value.status == DayResultStatus.GENERATED
    phone.reopen()
    assert phone.controller.day_state(MON).value.status == DayResultStatus.GENERATED  # also after a restart

    # 4. Stable ids and CSV round trips: both devices export the same records, and a fresh import matches.
    laptop_csv = export_planning_csv(laptop.planning, tmp_path / "laptop.csv").path
    phone_csv = export_planning_csv(phone.planning, tmp_path / "phone.csv").path
    assert {(kind, record_id) for kind, record_id, _ in _ids(laptop_csv)} == {
        (kind, record_id) for kind, record_id, _ in _ids(phone_csv)
    }
    fresh = make_device("fresh", None)
    fresh.planning.apply_record_batch(parse_canonical_csv(phone_csv.read_text(encoding="utf-8")))
    assert {t.id for t in fresh.planning.list_tasks()} == {t.id for t in laptop.planning.list_tasks()}

    # 5. Concurrent offline edits: a stale write becomes a conflict; the decision is explicit and audited.
    def edit(device, task_id, **changes):
        stored = device.planning.get_task(task_id)
        return device.planning.update_task(stored.model_copy(update=changes), expected_version=stored.version)

    edit(laptop, floating.id, name="Floating (laptop)")
    edit(phone, floating.id, name="Floating (phone)")
    phone.executions.resume(execution.id)
    laptop.sync_now()
    phone.sync_now()
    [conflict] = phone.sync.list_conflicts()
    assert conflict.local_record["name"] == "Floating (phone)" and conflict.remote_record["name"] == "Floating (laptop)"
    phone.sync.resolve_conflict(conflict.id, "keep_local")
    phone.sync_now()
    laptop.sync_now()
    assert laptop.planning.get_task(floating.id).name == "Floating (phone)"
    assert laptop.executions.get_execution(execution.id).status.value == "in_progress"  # unrelated work went through

    # 6. Deletion propagates and is never resurrected by a stale edit.
    laptop.planning.delete_task(daily.id, expected_version=laptop.planning.get_task(daily.id).version)
    edit(phone, daily.id, priority=9)
    laptop.sync_now()
    phone.sync_now()
    [deleted_conflict] = phone.sync.list_conflicts()
    with pytest.raises(Exception, match="deleted"):
        phone.sync.resolve_conflict(deleted_conflict.id, "keep_local")
    phone.sync.resolve_conflict(deleted_conflict.id, "accept_remote")
    assert phone.planning.get_task(daily.id) is None
    assert server.get("alice@example.com", f"/tasks/{daily.id}", include_deleted=True)["deleted_at"] is not None

    # 7. Rescheduling on the phone supersedes the laptop's old placement of the floating task (no double booking).
    first_day = week.allocation.assignments.get(floating.id, MON)
    other = MON + timedelta(days=5) if first_day != MON + timedelta(days=5) else MON + timedelta(days=4)
    assert phone.controller.schedule_range(other, other, scope=RangeScope.ELIGIBLE).value.superseded_ids
    phone.sync_now()
    laptop.sync_now()
    assert [p.planned_date for p in laptop.planning.list_placements() if p.task_id == floating.id] == [other]
    assert laptop.controller.day_state(first_day).value.status in (DayResultStatus.STALE, DayResultStatus.ALLOCATED)

    # 8. Outside-range dependency: completed on the phone, it satisfies a dependent the laptop schedules later.
    completed = phone.executions.complete(execution.id)
    phone.sync_now()
    laptop.sync_now()
    finished = completed.actual_final_end_at.date()
    later = finished + timedelta(days=7 - finished.weekday())
    dependent = laptop.add_task("Follow-up", dependency_ids=[prerequisite.id], required_date=later)
    run = laptop.controller.schedule_range(later, later + timedelta(days=6), scope=RangeScope.ELIGIBLE).value
    assert run.allocation.assignments.get(dependent.id) == later

    # 9. Rollback: a failed local compound change leaves nothing to push.
    before = laptop.dirty()
    original = laptop.planning._repository.insert_task
    calls = {"n": 0}

    def fail_second(task):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected")
        original(task)

    monkeypatch.setattr(laptop.planning._repository, "insert_task", fail_second)
    from app.planning.models import Task

    with pytest.raises(RuntimeError):
        laptop.planning.save_tasks([Task(name=f"T{i}", category="c", estimated_duration_minutes=5, priority=1)
                                    for i in range(2)])
    monkeypatch.undo()
    assert laptop.dirty() == before

    # 10. A later edit on the laptop makes the phone's synced schedule stale after the next pull.
    phone.sync_now()
    edit(laptop, prerequisite.id, priority=10)
    laptop.sync_now()
    phone.sync_now()
    assert phone.controller.day_state(MON).value.status == DayResultStatus.STALE


def test_another_user_never_receives_or_reaches_the_first_users_records(server, make_device) -> None:
    server.register("alice@example.com")
    server.register("mallory@example.com")
    alice = make_device("alice", InProcessTransport(server.client))
    mallory = make_device("mallory", InProcessTransport(server.client))
    alice.sign_in("alice@example.com")
    secret = alice.add_task("Private")
    alice.sync_now()

    mallory.sign_in("mallory@example.com")
    mallory.sync_now()
    assert mallory.planning.list_tasks() == []
    # Even a forged operation naming Alice's record id cannot touch it.
    forged = [{"op_id": "00000000-0000-4000-8000-00000000abcd", "entity_type": "task", "entity_id": str(secret.id),
               "kind": "update", "base_version": 1, "payload": {
                   "name": "Owned", "category": "study", "estimated_duration_minutes": 5, "priority": 1}}]
    [result] = mallory.transport.push(mallory.sync._token, forged)
    assert result["status"] == "rejected" and result["error"]["code"] == "not_found"
    assert server.get("alice@example.com", f"/tasks/{secret.id}")["name"] == "Private"
    assert mallory.sync.sign_in("mallory@example.com", PASSWORD).user_id != alice.sync.account.user_id
