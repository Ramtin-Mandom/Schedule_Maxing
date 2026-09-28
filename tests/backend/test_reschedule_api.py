"""The server side of explicit rescheduling and placement provenance
(docs/execution-rescheduling.md): the planning REST endpoint and its
reconciliation path, the sync "reschedule" action (atomic, idempotent per
op_id, with every changed record in the result), removal provenance on
deletes and cascades, the category snapshot, and user isolation."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from backend import models
from backend.database import session_factory
from tests.backend.conftest import account

DAY = "2026-03-02"


def iso(hour: int, minute: int = 0) -> str:
    return f"{DAY}T{hour:02d}:{minute:02d}:00+00:00"


def post(client, path: str, body: dict, headers: dict, status: int = 201) -> dict:
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def make_placement(client, headers, hour: int = 9, **task_fields) -> tuple[dict, dict]:
    task = post(client, "/tasks", {"name": task_fields.pop("name", "Study"), "category": "study",
                                   "estimated_duration_minutes": 60, "priority": 5, **task_fields}, headers)
    placement = post(client, "/placements", {"task_id": task["id"], "planned_date": DAY, "timezone": "UTC",
                                             "planned_start": iso(hour), "planned_end": iso(hour + 1)}, headers)
    return task, placement


def make_execution(client, headers, task: dict, placement: dict) -> dict:
    return post(client, "/executions", {
        "task_id": task["id"], "scheduled_task_id": placement["id"], "task_name": task["name"], "category": "study",
        "planned_duration": 60, "priority": 5, "canonical_planned_date": DAY, "canonical_timezone": "UTC",
        "canonical_planned_start": placement["planned_start"], "canonical_planned_end": placement["planned_end"],
    }, headers)


def move(placement: dict, hour: int, **extra) -> dict:
    return {"base_version": placement["version"], "planned_date": DAY, "timezone": "UTC",
            "planned_start": iso(hour), "planned_end": iso(hour + 1), **extra}


def counts(engine) -> tuple[int, int, int, int]:
    with session_factory(engine)() as session:
        return tuple(session.scalar(select(func.count()).select_from(table)) for table in (
            models.Placement, models.Execution, models.ChangeLogEntry, models.RecordRevision))


# -----------------------------------------------------------------------------
# REST
# -----------------------------------------------------------------------------


def test_rest_reschedule_is_atomic_and_a_lost_response_is_reconciled_by_reading(client, engine) -> None:
    alice = account(client, "alice@example.com")
    task, placement = make_placement(client, alice)
    execution = make_execution(client, alice, task, placement)
    replacement_id = str(uuid.uuid4())

    moved = post(client, f"/planning/placements/{placement['id']}/reschedule",
                 move(placement, 14, replacement_id=replacement_id), alice, 200)
    previous, replacement = moved["previous"], moved["replacement"]
    assert previous["deleted_at"] and previous["planned_start"].startswith(f"{DAY}T09:00")
    assert previous["removal_reason"] == "rescheduled" and previous["superseded_by_id"] == replacement_id
    assert replacement["id"] == replacement_id and replacement["task_category"] == "study"
    assert moved["cancelled_execution_id"] == execution["id"]
    cancelled = client.get(f"/executions/{execution['id']}", headers=alice).json()
    assert cancelled["status"] == "cancelled" and cancelled["version"] == 2 and cancelled["sessions"] == []

    # The feed carries the move in one commit: the tombstone first, then the replacement, then the attempt.
    changes = client.get("/changes", params={"limit": 500}, headers=alice).json()["changes"][-3:]
    assert [(c["entity_type"], c["entity_id"], c["operation"]) for c in changes] == [
        ("placement", placement["id"], "delete"), ("placement", replacement_id, "upsert"),
        ("execution", execution["id"], "upsert")]

    # A retry after a lost response is not replayed: it is a conflict whose `current` shows the client's own move.
    before = counts(engine)
    retry = client.post(f"/planning/placements/{placement['id']}/reschedule",
                        json=move(placement, 14, replacement_id=replacement_id), headers=alice)
    assert retry.status_code == 409
    error = retry.json()["error"]
    assert error["code"] == "deleted" and error["current"]["superseded_by_id"] == replacement_id
    assert counts(engine) == before  # nothing duplicated: no placement, execution, version or change entry


def test_rest_reschedule_refusals_change_nothing(client, engine) -> None:
    alice = account(client, "alice@example.com")
    task, placement = make_placement(client, alice)
    make_placement(client, alice, hour=12, name="Other")
    execution = make_execution(client, alice, task, placement)
    before = counts(engine)

    overlap = client.post(f"/planning/placements/{placement['id']}/reschedule", json=move(placement, 12), headers=alice)
    assert overlap.status_code == 409
    assert overlap.json()["error"]["code"] == "reschedule_rejected"
    assert overlap.json()["error"]["reason"] == "overlaps_placement"
    assert overlap.json()["error"]["current"]["id"] == placement["id"]

    stale = client.post(f"/planning/placements/{placement['id']}/reschedule",
                        json={**move(placement, 14), "base_version": placement["version"] + 1}, headers=alice)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"

    version = client.get(f"/executions/{execution['id']}", headers=alice).json()["version"]
    post(client, f"/executions/{execution['id']}/actions/start", {"base_version": version}, alice, 200)
    started = counts(engine)
    protected = client.post(f"/planning/placements/{placement['id']}/reschedule", json=move(placement, 14),
                            headers=alice)
    assert protected.status_code == 409
    assert protected.json()["error"]["code"] == "history_protected"
    assert protected.json()["error"]["reason"] == "in_progress"
    assert counts(engine) == started and started[0] == before[0]


def test_another_users_placement_cannot_be_moved_or_referenced(client) -> None:
    alice = account(client, "alice@example.com")
    bob = account(client, "bob@example.com")
    _, placement = make_placement(client, alice)
    _, own = make_placement(client, bob)

    response = client.post(f"/planning/placements/{placement['id']}/reschedule", json=move(placement, 14), headers=bob)
    assert response.status_code == 404
    taken = client.post(f"/planning/placements/{own['id']}/reschedule",
                        json=move(own, 14, replacement_id=own["id"]), headers=bob)
    assert taken.status_code == 422  # a move always makes a new placement
    assert client.get(f"/placements/{placement['id']}", headers=alice).json()["deleted_at"] is None


def test_removal_provenance_of_deletes_and_cascades_and_the_category_snapshot(client) -> None:
    alice = account(client, "alice@example.com")
    task, placement = make_placement(client, alice)
    assert placement["task_category"] == "study"  # snapshotted on create
    other_task, other = make_placement(client, alice, hour=12)

    rejected = client.post("/placements", json={"task_id": task["id"], "planned_date": DAY, "timezone": "UTC",
                                                "planned_start": iso(15), "planned_end": iso(16),
                                                "removal_reason": "deleted"}, headers=alice)
    assert rejected.status_code == 422

    renamed = client.put(f"/tasks/{task['id']}", headers=alice, json={
        "name": "Study", "category": "reading", "estimated_duration_minutes": 60, "priority": 5,
        "base_version": task["version"]}).json()
    assert client.get(f"/placements/{placement['id']}", headers=alice).json()["task_category"] == "study"
    changed = client.put(f"/placements/{placement['id']}", headers=alice, json={
        "task_id": task["id"], "planned_date": DAY, "timezone": "UTC", "planned_start": iso(9), "planned_end": iso(10),
        "task_category": "reading", "base_version": placement["version"]})
    assert changed.status_code == 422  # the snapshot never changes
    assert renamed["category"] == "reading"

    deleted = client.delete(f"/placements/{placement['id']}", params={"base_version": placement["version"]},
                            headers=alice).json()
    assert deleted["removal_reason"] == "deleted" and deleted["superseded_by_id"] is None
    gone = client.delete(f"/tasks/{other_task['id']}", params={"base_version": other_task["version"]}, headers=alice)
    assert gone.status_code == 200
    assert client.get(f"/placements/{other['id']}", params={"include_deleted": True},
                      headers=alice).json()["removal_reason"] == "task_deleted"


# -----------------------------------------------------------------------------
# Sync push
# -----------------------------------------------------------------------------


def reschedule_op(placement: dict, hour: int, replacement_id: str, **extra) -> dict:
    return {"op_id": str(uuid.uuid4()), "entity_type": "placement", "entity_id": placement["id"], "kind": "action",
            "action": "reschedule", "base_version": placement["version"], "payload": {
                "replacement_id": replacement_id, "planned_date": DAY, "timezone": "UTC",
                "planned_start": iso(hour), "planned_end": iso(hour + 1), "at": iso(11, 30), **extra}}


def test_a_synced_reschedule_is_one_unit_and_its_retry_replays_every_record(client, clock, engine) -> None:
    alice = account(client, "alice@example.com")
    task, placement = make_placement(client, alice)
    execution = make_execution(client, alice, task, placement)
    clock.advance(minutes=30)
    op = reschedule_op(placement, 14, str(uuid.uuid4()), task_category="study")

    first = client.post("/sync/push", json={"operations": [op]}, headers=alice).json()["results"][0]
    assert first["status"] == "applied"
    assert first["record"]["id"] == placement["id"] and first["record"]["removal_reason"] == "rescheduled"
    related = {item["entity_type"]: item["record"] for item in first["related"]}
    assert related["placement"]["id"] == op["payload"]["replacement_id"]
    assert related["execution"]["id"] == execution["id"] and related["execution"]["status"] == "cancelled"
    assert related["execution"]["actual_final_end_at"].startswith(f"{DAY}T11:30")  # the device's time of the move

    before = counts(engine)
    clock.advance(minutes=5)
    replay = client.post("/sync/push", json={"operations": [op]}, headers=alice).json()["results"][0]
    assert replay == first and counts(engine) == before  # recorded outcome: no second write of any kind

    again = client.post("/sync/push", json={"operations": [{**op, "op_id": str(uuid.uuid4())}]},
                        headers=alice).json()["results"][0]
    assert again["status"] == "conflict" and again["error"]["code"] == "deleted"
    assert again["error"]["current"]["superseded_by_id"] == op["payload"]["replacement_id"]
    assert counts(engine)[:3] == before[:3]


def test_a_synced_move_of_started_work_is_a_conflict_and_changes_nothing(client, engine) -> None:
    alice = account(client, "alice@example.com")
    task, placement = make_placement(client, alice)
    execution = make_execution(client, alice, task, placement)
    post(client, f"/executions/{execution['id']}/actions/start", {"base_version": 1}, alice, 200)
    before = counts(engine)

    result = client.post("/sync/push", json={"operations": [reschedule_op(placement, 14, str(uuid.uuid4()))]},
                         headers=alice).json()["results"][0]
    assert result["status"] == "conflict" and result["error"]["code"] == "history_protected"
    assert result["error"]["current"]["id"] == placement["id"] and result["error"]["current"]["deleted_at"] is None
    after = counts(engine)
    assert after[:3] == before[:3]  # no placement, execution or change-log entry (only the outcome's snapshot)


def test_synced_deletes_carry_their_removal_reason(client) -> None:
    alice = account(client, "alice@example.com")
    bob = account(client, "bob@example.com")
    task, placement = make_placement(client, alice)
    successor = post(client, "/placements", {"task_id": task["id"], "planned_date": DAY, "timezone": "UTC",
                                             "planned_start": iso(13), "planned_end": iso(14)}, alice)
    _, bobs = make_placement(client, bob)

    def delete(payload, record=placement) -> dict:
        op = {"op_id": str(uuid.uuid4()), "entity_type": "placement", "entity_id": record["id"], "kind": "delete",
              "base_version": record["version"], "payload": payload}
        return client.post("/sync/push", json={"operations": [op]}, headers=alice).json()["results"][0]

    assert delete({"removal_reason": "rescheduled", "superseded_by_id": successor["id"]})["status"] == "rejected"
    assert delete({"removal_reason": "regenerated", "superseded_by_id": bobs["id"]})["error"]["code"] == \
        "invalid_reference"  # another user's placement is never a successor
    applied = delete({"removal_reason": "regenerated", "superseded_by_id": successor["id"]})
    assert applied["status"] == "applied"
    assert (applied["record"]["removal_reason"], applied["record"]["superseded_by_id"]) == ("regenerated", successor["id"])

    unknown = delete(None, successor)  # an older client sends no reason: it stays unknown, never guessed
    assert unknown["status"] == "applied" and unknown["record"]["removal_reason"] is None


def test_history_uploads_are_tombstones_with_a_valid_successor(client) -> None:
    alice = account(client, "alice@example.com")
    bob = account(client, "bob@example.com")
    task, successor = make_placement(client, alice, hour=14)
    _, bobs = make_placement(client, bob)

    def upload(**payload) -> dict:
        op = {"op_id": str(uuid.uuid4()), "entity_type": "placement", "entity_id": str(uuid.uuid4()), "kind": "create",
              "payload": {"task_id": task["id"], "planned_date": DAY, "timezone": "UTC", "planned_start": iso(9),
                          "planned_end": iso(10), **payload}}
        return client.post("/sync/push", json={"operations": [op]}, headers=alice).json()["results"][0]

    history = upload(removal_reason="rescheduled", superseded_by_id=successor["id"], task_category="study")
    assert history["status"] == "applied" and history["record"]["deleted_at"] is not None
    assert history["record"]["removal_reason"] == "rescheduled" and history["record"]["task_category"] == "study"
    assert upload(removal_reason="deleted", superseded_by_id=successor["id"])["status"] == "rejected"
    assert upload(removal_reason="regenerated", superseded_by_id=bobs["id"])["error"]["code"] == "invalid_reference"
    assert upload(removal_reason="regenerated")["status"] == "rejected"  # a history upload names its successor
    live = [p["id"] for p in client.get("/placements", headers=alice).json()["items"]]
    assert live == [successor["id"]]  # nothing live was created
