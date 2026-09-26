"""The server enforces the fixed-block write invariants (app/planning/fixed_block_rules.py)
for REST and sync push alike: overlaps, sub-minute times, wrong dates and
blocks outside the user's effective day window are refused and leave the
records and the change log exactly as they were."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from backend import models
from backend.database import session_factory
from tests.backend.conftest import block_payload, create, editable, preference_payload


def change_count(engine) -> int:
    with session_factory(engine)() as session:
        return session.scalar(select(func.count()).select_from(models.ChangeLogEntry))


def blocks(client, headers) -> list[dict]:
    return client.get("/fixed-blocks", headers=headers).json()["items"]


def interval(start: str, end: str, day: str = "2026-03-02", **extra) -> dict:
    return block_payload(planned_date=day, planned_start=f"{day}T{start}:00Z", planned_end=f"{day}T{end}:00Z", **extra)


def test_the_second_of_two_overlapping_posts_is_refused(client, alice, engine) -> None:
    first = client.post("/fixed-blocks", json=interval("09:00", "11:00", label="Lecture"), headers=alice)
    assert first.status_code == 201
    before = change_count(engine)

    second = client.post("/fixed-blocks", json=interval("10:00", "12:00", label="Lab"), headers=alice)

    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == "fixed_block_overlap" and error["conflicting"]["id"] == first.json()["id"]
    assert "current" not in error  # a sync client must not mistake the other block for this record's server copy
    assert change_count(engine) == before and [b["label"] for b in blocks(client, alice)] == ["Lecture"]


def test_adjacent_blocks_and_off_grid_boundaries_are_accepted(client, alice) -> None:
    for start, end in (("09:00", "10:13"), ("10:13", "11:47"), ("11:47", "12:00")):
        assert client.post("/fixed-blocks", json=interval(start, end), headers=alice).status_code == 201
    assert len(blocks(client, alice)) == 3


def test_invalid_intervals_are_refused_with_a_reason(client, alice, engine) -> None:
    before = change_count(engine)
    cases = {
        "sub_minute_precision": block_payload(planned_start="2026-03-02T09:00:30Z", planned_end="2026-03-02T10:00:00Z"),
        "date_mismatch": block_payload(planned_date="2026-03-03"),
        "outside_day_window": block_payload(planned_start="2026-03-02T23:00:00Z", planned_end="2026-03-03T01:00:00Z"),
    }
    for reason, payload in cases.items():
        response = client.post("/fixed-blocks", json=payload, headers=alice)
        assert response.status_code == 422, (reason, response.text)
        assert response.json()["error"]["reason"] == reason
    assert change_count(engine) == before and blocks(client, alice) == []


def test_the_users_date_layer_sets_the_effective_window(client, alice, bob) -> None:
    create(client, alice, "preferences", preference_payload(
        scope="date", date="2026-03-02", overrides={"day_window": {"start_minute": 480, "end_minute": 1320}}))
    early = interval("07:00", "09:00")
    response = client.post("/fixed-blocks", json=early, headers=alice)
    assert response.status_code == 422 and response.json()["error"]["reason"] == "outside_day_window"
    assert client.post("/fixed-blocks", json=early, headers=bob).status_code == 201  # bob's layers are his own
    assert client.post("/fixed-blocks", json=interval("08:00", "09:00"), headers=alice).status_code == 201


def test_an_update_is_judged_against_the_others_but_not_itself(client, alice, engine) -> None:
    first = create(client, alice, "fixed-blocks", interval("09:00", "10:00", label="First"))
    create(client, alice, "fixed-blocks", interval("12:00", "13:00", label="Second"))

    moved = client.put(f"/fixed-blocks/{first['id']}", headers=alice, json=editable(
        first, planned_start="2026-03-02T09:30:00Z", planned_end="2026-03-02T10:30:00Z"))
    assert moved.status_code == 200 and moved.json()["version"] == 2

    before = change_count(engine)
    clash = client.put(f"/fixed-blocks/{first['id']}", headers=alice,
                       json=editable(moved.json(), planned_end="2026-03-02T12:30:00Z"))
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "fixed_block_overlap"
    assert change_count(engine) == before


def test_a_historical_block_keeps_its_interval_through_a_relabel(client, alice, engine) -> None:
    user_id = uuid.UUID(client.get("/me", headers=alice).json()["id"])
    record_id = uuid.uuid4()
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with session_factory(engine)() as session:  # stored before the rules existed: seconds in its times
        session.add(models.FixedBlock(
            user_id=user_id, id=record_id, label="Old", category="fixed", planned_date=datetime(2026, 3, 2).date(),
            timezone="UTC", planned_start=datetime(2026, 3, 2, 9, 0, 15, tzinfo=timezone.utc),
            planned_end=datetime(2026, 3, 2, 10, tzinfo=timezone.utc), created_at=now, updated_at=now, version=1))
        session.commit()
    stored = client.get(f"/fixed-blocks/{record_id}", headers=alice).json()

    relabelled = client.put(f"/fixed-blocks/{record_id}", headers=alice, json=editable(stored, label="Seminar"))
    assert relabelled.status_code == 200 and relabelled.json()["planned_start"] == stored["planned_start"]
    moved = client.put(f"/fixed-blocks/{record_id}", headers=alice,
                       json=editable(relabelled.json(), planned_end="2026-03-02T10:30:00Z"))
    assert moved.status_code == 422 and moved.json()["error"]["reason"] == "sub_minute_precision"


def test_sync_push_cannot_bypass_the_invariants(client, alice, engine) -> None:
    create(client, alice, "fixed-blocks", interval("09:00", "11:00"))
    before = change_count(engine)
    operations = [
        {"op_id": str(uuid.uuid4()), "entity_type": "fixed_block", "entity_id": str(uuid.uuid4()), "kind": "create",
         "payload": interval("10:00", "12:00")},
        {"op_id": str(uuid.uuid4()), "entity_type": "fixed_block", "entity_id": str(uuid.uuid4()), "kind": "create",
         "payload": block_payload(planned_start="2026-03-02T13:00:01Z", planned_end="2026-03-02T14:00:00Z")},
    ]
    results = client.post("/sync/push", json={"operations": operations}, headers=alice).json()["results"]

    assert [r["status"] for r in results] == ["conflict", "rejected"]
    assert results[0]["error"]["code"] == "fixed_block_overlap" and "current" not in results[0]["error"]
    assert results[1]["error"]["reason"] == "sub_minute_precision"
    assert change_count(engine) == before and len(blocks(client, alice)) == 1
