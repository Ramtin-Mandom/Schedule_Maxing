"""POST /me/task-data/reset: every live task/schedule/execution record of the authenticated user
becomes a logged tombstone in one transaction (another user's data, the account and its
preference settings untouched), a failure part-way rolls everything back, the request needs an
explicit confirmation and a token -- never a user id from the body -- and a stale operation from
another device cannot bring a record back."""

from __future__ import annotations

import pytest

from backend import mutations
from tests.backend.conftest import create, seed

ENTITIES = ("projects", "tasks", "fixed-blocks", "placements", "schedule-generations", "executions")


def live(client, headers, path: str) -> list[dict]:
    return client.get(f"/{path}", params={"limit": 500}, headers=headers).json()["items"]


def reset(client, headers, **body):
    return client.post("/me/task-data/reset", json={"confirm": True, **body} if not body else body, headers=headers)


def test_reset_removes_only_the_callers_task_data_and_keeps_account_and_settings(client, alice, bob) -> None:
    mine, theirs = seed(client, alice), seed(client, bob)
    response = reset(client, alice)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed"] == {"execution": 1, "placement": 1, "schedule_generation": 1, "fixed_block": 1,
                               "task": 1, "project": 1}
    for path in ENTITIES:
        assert live(client, alice, path) == [], path
        assert [item["id"] for item in live(client, bob, path)] == [theirs[path]["id"]], path  # untouched
    assert [item["id"] for item in live(client, alice, "preferences")] == [mine["preferences"]["id"]]  # settings stay
    assert client.get("/me", headers=alice).status_code == 200  # the account and its sign-in stay

    tombstone = client.get(f"/placements/{mine['placements']['id']}", params={"include_deleted": True},
                           headers=alice).json()
    assert tombstone["deleted_at"] is not None and tombstone["removal_reason"] == "reset"
    changes = client.get("/changes", params={"after": 0, "limit": 500}, headers=alice).json()
    assert changes["cursor"] == body["cursor"]  # a device that wiped its copy continues exactly here
    deletes = {(change["entity_type"], change["operation"]) for change in changes["changes"][-6:]}
    assert deletes == {(entity, "delete") for entity in body["removed"]}  # other devices learn every removal

    again = reset(client, alice).json()
    assert set(again["removed"].values()) == {0}  # nothing left: idempotent


def test_a_stale_operation_from_another_device_cannot_revive_a_reset_record(client, alice) -> None:
    records = seed(client, alice)
    task = records["tasks"]
    reset(client, alice)
    stale = client.put(f"/tasks/{task['id']}", json={
        "name": "Revived?", "category": "study", "estimated_duration_minutes": 60, "priority": 5,
        "base_version": task["version"]}, headers=alice)
    assert stale.status_code == 409 and stale.json()["error"]["code"] in ("deleted", "version_conflict")
    assert live(client, alice, "tasks") == []


def test_the_request_needs_confirmation_and_a_token_not_a_user_id(client, alice, bob) -> None:
    seed(client, alice)
    assert reset(client, alice, confirm=False).status_code == 422
    assert client.post("/me/task-data/reset", json={}, headers=alice).status_code == 422
    bob_id = client.get("/me", headers=bob).json()["id"]
    forged = client.post("/me/task-data/reset", json={"confirm": True, "user_id": bob_id}, headers=alice)
    assert forged.status_code == 422  # no other fields are accepted: the account is always the token's
    assert client.post("/me/task-data/reset", json={"confirm": True}).status_code == 401
    assert len(live(client, alice, "tasks")) == 1


def test_a_failure_part_way_rolls_everything_back(client, alice, monkeypatch) -> None:
    seed(client, alice)
    create(client, alice, "tasks", {"name": "Second", "category": "study", "estimated_duration_minutes": 30,
                                   "priority": 3})
    original = mutations.Mutator.tombstone
    calls = {"count": 0}

    def failing(self, spec, row):
        calls["count"] += 1
        if spec.entity_type == "task":
            raise RuntimeError("storage failure during the reset")
        return original(self, spec, row)

    monkeypatch.setattr(mutations.Mutator, "tombstone", failing)
    with pytest.raises(RuntimeError):
        reset(client, alice)
    monkeypatch.setattr(mutations.Mutator, "tombstone", original)
    assert calls["count"] > 1  # executions, placements... were already tombstoned inside the transaction
    for path in ENTITIES:
        assert live(client, alice, path), path  # ...and all of it was rolled back
