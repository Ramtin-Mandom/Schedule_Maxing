"""Every protected query is filtered by the authenticated user: another user's
records cannot be listed, read, changed, deleted, referenced, or observed
through the change log, for every resource type. Another user's record is
indistinguishable from a missing one (404 / invalid_reference), and the same
client id can exist independently for two users without revealing anything."""

from __future__ import annotations

import pytest

from tests.backend.conftest import (
    create,
    editable,
    execution_payload,
    placement_payload,
    seed,
    task_payload,
)

PATHS = ["projects", "tasks", "fixed-blocks", "placements", "preferences", "schedule-generations", "executions"]


@pytest.fixture
def alices_records(client, alice):
    return seed(client, alice)


@pytest.mark.parametrize("path", PATHS)
def test_other_users_cannot_list_read_update_or_delete(client, alice, bob, alices_records, path) -> None:
    record = alices_records[path]

    assert client.get(f"/{path}", params={"include_deleted": True}, headers=bob).json()["items"] == []
    for params in ({}, {"include_deleted": True}):
        response = client.get(f"/{path}/{record['id']}", params=params, headers=bob)
        assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    delete = client.delete(f"/{path}/{record['id']}", params={"base_version": record["version"]}, headers=bob)
    assert delete.status_code == 404
    if path == "executions":
        response = client.post(f"/executions/{record['id']}/actions/start", json={"base_version": 1}, headers=bob)
        feedback = client.post(f"/executions/{record['id']}/feedback", json={"base_version": 1, "note": "x"},
                               headers=bob)
        assert response.status_code == feedback.status_code == 404
    else:
        update = client.put(f"/{path}/{record['id']}", json=editable(record), headers=bob)
        assert update.status_code in (404, 422)  # 422 only where the body references alice's records
        assert "current" not in update.json().get("error", {})

    assert client.get(f"/{path}/{record['id']}", headers=alice).json() == record  # untouched


@pytest.mark.parametrize("path", PATHS)
def test_the_same_client_id_is_independent_per_user(client, alice, bob, alices_records, path) -> None:
    """Bob creating a record with Alice's id neither collides with nor reveals hers."""
    bobs = seed(client, bob)
    record = alices_records[path]
    body = {k: v for k, v in editable(bobs[path]).items() if k not in ("base_version", "sessions")}
    if path == "executions":
        body = execution_payload(historical_reference=True)
    response = client.post(f"/{path}", json={**body, "id": record["id"]}, headers=bob)
    if path in ("preferences", "schedule-generations"):  # bob already has one for this scope/date
        assert response.status_code == 409 and response.json()["error"]["current"]["id"] != record["id"]
    else:
        assert response.status_code == 201, response.text
        assert client.get(f"/{path}/{record['id']}", headers=bob).json()["id"] == record["id"]
    assert client.get(f"/{path}/{record['id']}", headers=alice).json() == record


def test_references_to_another_users_records_are_rejected(client, alice, bob, alices_records) -> None:
    project, task = alices_records["projects"], alices_records["tasks"]
    placement = alices_records["placements"]
    bobs_task = create(client, bob, "tasks", task_payload(name="Bob's"))

    attempts = [
        ("tasks", task_payload(project_id=project["id"])),
        ("tasks", task_payload(dependency_ids=[task["id"]])),
        ("placements", placement_payload(task["id"])),
        ("executions", execution_payload(task["id"], placement["id"])),
        ("executions", execution_payload(bobs_task["id"], placement["id"])),
    ]
    for path, payload in attempts:
        response = client.post(f"/{path}", json=payload, headers=bob)
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_reference", (path, payload)
    moved = client.put(f"/tasks/{bobs_task['id']}", json=editable(bobs_task, dependency_ids=[task["id"]]), headers=bob)
    assert moved.status_code == 422


def test_forged_ownership_fields_are_rejected(client, alice, bob, alices_records) -> None:
    alice_id = client.get("/me", headers=alice).json()["id"]
    for path, payload in [("projects", {"name": "Mine now", "user_id": alice_id}),
                          ("tasks", task_payload(user_id=alice_id)),
                          ("executions", {**execution_payload(historical_reference=True), "user_id": alice_id})]:
        assert client.post(f"/{path}", json=payload, headers=bob).status_code == 422
    assert client.get("/projects", headers=alice).json()["items"] == [alices_records["projects"]]


def test_the_change_log_is_per_user(client, alice, bob, alices_records) -> None:
    create(client, bob, "projects", {"name": "Bob's"})
    alices = client.get("/changes", headers=alice).json()["changes"]
    bobs = client.get("/changes", headers=bob).json()["changes"]
    assert [c["seq"] for c in bobs] == [1] and bobs[0]["record"]["name"] == "Bob's"
    assert {c["entity_id"] for c in alices} >= {r["id"] for r in alices_records.values()}
    assert not {c["entity_id"] for c in alices} & {c["entity_id"] for c in bobs}
