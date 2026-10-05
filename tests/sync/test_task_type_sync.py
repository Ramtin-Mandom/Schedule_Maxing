"""Task types and planning snapshots through synchronization (docs/productivity-redesign-plan.md, step 3):
a second device reconstructs the same types, snapshots and tracker analytics from the server; deleted execution
history withdraws its points everywhere and is not resurrected; a server without the "task_types" feature never
erases what a device recorded, and the held records are uploaded once the server supports them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.execution.repository import ExecutionRepository
from app.planning import workflow
from app.planning.models import PLACEMENT_SNAPSHOT_FIELDS, ScheduledTask, derived_task_type_id
from app.productivity.reporting import ProductivityService
from tests.sync.conftest import MON, InProcessTransport

EMAIL = "alice@example.com"


def at(hour: int) -> datetime:
    return datetime(MON.year, MON.month, MON.day, hour, tzinfo=timezone.utc)


def planned(device, name: str, hour: int, **fields):
    task = device.add_task(name, required_date=MON, **fields)
    placement = ScheduledTask(task_id=task.id, user_id=task.user_id, planned_date=MON, timezone="UTC",
                              planned_start=at(hour), planned_end=at(hour + 1), created_at=at(0), updated_at=at(0))
    existing = device.planning.placements_for_date(MON)
    device.planning.replace_placements(MON, MON, [*existing, placement],
                                       expected_versions={p.id: p.version for p in existing})
    return device.planning.get_task(task.id), device.planning.get_placement(placement.id)


def snapshot(placement) -> dict:
    return {name: getattr(placement, name) for name in PLACEMENT_SNAPSHOT_FIELDS}


def tracker(device, as_of: datetime):
    service = ProductivityService(ExecutionRepository(device.connection), history=device.planning, timezone_name="UTC")
    report = service.build_tracker_report(as_of=as_of)
    return report.model_dump(mode="json", include={"general", "types", "completeness"})


def test_a_second_device_reconstructs_types_snapshots_and_analytics_and_deletion_stays_deleted(
        alice_server, make_device) -> None:
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in(EMAIL)
    task, placement = planned(a, "Reading", 9, tags=["deep"], points=3)
    assert task.task_type_id == derived_task_type_id(task.id)
    assert a.sync_now().status == "ok"

    # Edits and a move after the first sync: the original snapshot stays on the tombstone.
    renamed = a.planning.update_task(task.model_copy(update={"name": "Skimming", "points": 8}),
                                     expected_version=task.version)
    moved = workflow.reschedule_placement(a.planning, placement.id, expected_version=placement.version,
                                          planned_date=MON, timezone_name="UTC", planned_start=at(13),
                                          planned_end=at(14))
    execution = a.executions.get_or_create_canonical_execution(renamed, moved.replacement)
    a.executions.start(execution.id)
    a.executions.complete(execution.id)
    assert a.sync_now().status == "ok" and a.dirty() == []
    assert a.sync_now().pushed == 0  # nothing is sent twice

    b.sign_in(EMAIL)
    assert b.sync_now().status == "ok" and b.sync.status().conflicts == 0
    theirs = b.planning.get_task(task.id)
    assert theirs.task_type_id == task.task_type_id and theirs.name == "Skimming"
    assert [(t.id, t.label) for t in b.planning.list_task_types()] == [(task.task_type_id, "Reading")]
    for record in (moved.previous, moved.replacement):
        assert snapshot(b.planning.get_placement(record.id, include_deleted=True)) == snapshot(record)
    assert snapshot(moved.previous)["task_name"] == "Reading" and snapshot(moved.replacement)["task_name"] == "Skimming"

    server_placement = alice_server.get(EMAIL, f"/placements/{moved.replacement.id}")
    assert (server_placement["task_name"], server_placement["task_points"], server_placement["task_tags"]) == (
        "Skimming", 8, ["deep"])
    assert server_placement["task_type_id"] == str(task.task_type_id)

    as_of = datetime.now(timezone.utc)
    mine, yours = tracker(a, as_of), tracker(b, as_of)
    assert mine == yours and mine["general"]["activity"]["known_points"] == 8  # the execution's own snapshot

    # Deleting the history on one device withdraws the award everywhere; a later sync of the other device
    # (which still held the completed record) does not bring it back.
    stored = a.executions.get_execution(execution.id)
    a.executions.delete_execution(execution.id, expected_version=stored.version)
    assert a.sync_now().status == "ok" and b.sync_now().status == "ok" and b.sync_now().status == "ok"
    later = datetime.now(timezone.utc) + timedelta(seconds=0)
    for device in (a, b):
        report = tracker(device, later)
        assert report["general"]["activity"]["completions"] == 0
        assert not report["general"]["highest_point_day"]["available"]
    assert alice_server.get(EMAIL, f"/executions/{execution.id}", include_deleted=True)["deleted_at"] is not None


class OlderServerTransport(InProcessTransport):
    """The same server, announcing no "task_types" feature until `upgraded` is set."""

    upgraded = False

    def capabilities(self, token: str) -> dict:
        answer = super().capabilities(token)
        if not self.upgraded:
            answer["features"] = [name for name in answer["features"] if name != "task_types"]
        return answer


def test_a_server_without_task_types_never_erases_them_and_gets_them_after_its_upgrade(
        alice_server, make_device) -> None:
    transport = OlderServerTransport(alice_server.client)
    device = make_device("a", transport)
    device.sign_in(EMAIL)
    task, placement = planned(device, "Reading", 9, tags=["deep"], points=3)

    report = device.sync_now()
    assert report.status == "ok" and report.held == 1 and "task types" in report.message
    assert ("task_type", str(task.task_type_id)) in [(kind, key) for kind, key, _ in device.dirty()]  # still queued
    for operations in transport.pushed:  # the fields were left out, not sent to a server that cannot keep them
        for operation in operations:
            assert operation["entity_type"] != "task_type"
            assert "task_type_id" not in (operation["payload"] or {}) and "task_name" not in (operation["payload"] or {})
    assert alice_server.get(EMAIL, f"/tasks/{task.id}")["task_type_id"] is None
    # The acknowledged server copies did not downgrade the local records.
    assert device.planning.get_task(task.id).task_type_id == task.task_type_id
    assert snapshot(device.planning.get_placement(placement.id)) == snapshot(placement)

    transport.upgraded = True
    device.sync._capabilities = None  # a new session asks the server again
    upgraded = device.sync_now()
    assert upgraded.status == "ok" and upgraded.held == 0 and device.dirty() == []
    assert device.sync.status().conflicts == 0
    assert alice_server.get(EMAIL, f"/tasks/{task.id}")["task_type_id"] == str(task.task_type_id)
    assert alice_server.get(EMAIL, f"/task-types/{task.task_type_id}")["label"] == "Reading"
    stored = alice_server.get(EMAIL, f"/placements/{placement.id}")
    assert (stored["task_name"], stored["task_points"], stored["task_tags"]) == ("Reading", 3, ["deep"])
    assert snapshot(device.planning.get_placement(placement.id)) == snapshot(placement)
