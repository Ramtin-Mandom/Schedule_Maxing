"""Manual placements across devices (docs/execution-rescheduling.md "Manual placements", docs/sync-protocol.md):
a move made on one device is manual intent on the other, so the other device's generation keeps it; a release
travels back; a generation-superseded attempt reaches the server as a system cancellation (never a skip); and
with a server that does not know manual placements the fields are not sent and a pulled record without them
never erases the local intent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.execution.models import CancelReason, ExecutionStatus
from app.planning import workflow
from app.planning.application import RangeScope
from tests.sync.conftest import MON, InProcessTransport

PLAN_FIELDS = ("origin", "preserved")


@pytest.fixture
def pair(alice_server, make_device):
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    return alice_server, a, b


def generate(device) -> workflow.GenerationOutcome:
    return workflow.generate(device.planning, range_start=MON, range_end=MON, timezone_name="UTC",
                             scope=RangeScope.ELIGIBLE)


def only(device, task):
    [placement] = [p for p in device.planning.placements_for_date(MON) if p.task_id == task.id]
    return placement


def move(device, placement, hour: int):
    start = datetime(MON.year, MON.month, MON.day, hour, tzinfo=timezone.utc)
    return workflow.reschedule_placement(device.planning, placement.id, expected_version=placement.version,
                                         planned_date=MON, timezone_name="UTC", planned_start=start,
                                         planned_end=start + timedelta(minutes=60)).replacement


def test_a_move_is_manual_intent_on_every_device_until_released(pair) -> None:
    server, a, b = pair
    essay = a.add_task("Essay", required_date=MON)
    generate(a)
    moved = move(a, only(a, essay), 15)
    assert a.sync_now().status == "ok"
    on_server = server.get("alice@example.com", f"/placements/{moved.id}")
    assert (on_server["origin"], on_server["preserved"]) == ("manual", True)

    assert b.sync_now().status == "ok"
    copy = only(b, essay)
    assert (copy.id, copy.origin.value, copy.preserved) == (moved.id, "manual", True)
    b.add_task("Other", required_date=MON)
    generate(b)  # B's own full generation keeps A's move
    assert only(b, essay).id == moved.id

    released = b.planning.release_manual_placement(moved.id, expected_version=only(b, essay).version)
    assert b.sync_now().status == "ok" and b.sync.list_conflicts() == []
    assert a.sync_now().status == "ok"
    assert only(a, essay).preserved is False and only(a, essay).version == released.version


def test_a_superseded_attempt_reaches_the_server_as_a_system_cancellation(pair) -> None:
    server, a, _ = pair
    essay = a.add_task("Essay", required_date=MON)
    generate(a)
    execution = a.executions.get_or_create_canonical_execution(essay, only(a, essay))
    assert a.sync_now().status == "ok"
    a.planning.update_task(essay.model_copy(update={"estimated_duration_minutes": 90}), expected_version=essay.version)

    generate(a)
    assert a.sync_now().status == "ok" and a.sync.list_conflicts() == []

    record = server.get("alice@example.com", f"/executions/{execution.id}")
    assert (record["status"], record["cancel_reason"]) == ("cancelled", "superseded")
    local = a.executions.get_execution(execution.id)
    assert (local.status, local.cancel_reason) == (ExecutionStatus.CANCELLED, CancelReason.SUPERSEDED)


class OlderServerTransport(InProcessTransport):
    """A server from before manual placements: no such feature, and its records carry no origin or intent."""

    def capabilities(self, token: str) -> dict:
        found = super().capabilities(token)
        return {**found, "features": [name for name in found["features"] if name != "manual_placements"]}

    def push(self, token: str, operations: list[dict]) -> list[dict]:
        for operation in operations:
            payload = operation.get("payload") or {}
            assert not {"origin", "preserved", "cancel_reason"} & set(payload), operation  # never sent
        return super().push(token, operations)

    def pull(self, token: str, after: int, limit: int):
        page = super().pull(token, after, limit)
        for change in page.changes:
            record = change.get("record") if isinstance(change, dict) else getattr(change, "record", None)
            if isinstance(record, dict):
                for name in (*PLAN_FIELDS, "cancel_reason"):
                    record.pop(name, None)
        return page


def test_an_older_server_is_not_sent_the_fields_and_cannot_erase_local_intent(alice_server, make_device) -> None:
    device = make_device("older", OlderServerTransport(alice_server.client))
    device.sign_in("alice@example.com")
    essay = device.add_task("Essay", required_date=MON)
    plain = device.add_task("Plain", required_date=MON)
    generate(device)
    moved = move(device, only(device, essay), 15)

    assert device.sync_now().status == "ok" and device.sync.list_conflicts() == []

    kept = only(device, essay)
    assert (kept.id, kept.preserved) == (moved.id, True)  # a pulled record without the fields keeps them here
    assert only(device, plain).origin is not None
