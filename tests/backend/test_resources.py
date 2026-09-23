"""CRUD, server versions, optimistic concurrency (409), soft deletion, server-owned
audit fields, pagination, and relationship policies for every plain resource."""

from __future__ import annotations

import uuid

import pytest

from tests.backend.conftest import (
    block_payload,
    create,
    editable,
    generation_payload,
    placement_payload,
    preference_payload,
    task_payload,
)

RESOURCES = ["projects", "tasks", "fixed-blocks", "placements", "preferences", "schedule-generations"]
EDITS = {
    "projects": {"name": "Renamed"},
    "tasks": {"priority": 9},
    "fixed-blocks": {"category": "rest"},
    "placements": {"score": 7.25},
    "preferences": {"overrides": {"optimizer_mode": "precise_greedy"}},
    "schedule-generations": {"placement_count": 2},
}


def make(client, headers, path: str) -> dict:
    if path == "placements":
        task = create(client, headers, "tasks", task_payload())
        return create(client, headers, path, placement_payload(task["id"]))
    payloads = {"projects": {"name": "Thesis"}, "tasks": task_payload(), "fixed-blocks": block_payload(),
                "preferences": preference_payload(), "schedule-generations": generation_payload()}
    return create(client, headers, path, payloads[path])


@pytest.mark.parametrize("path", RESOURCES)
def test_crud_versions_and_server_owned_audit_fields(client, alice, clock, path) -> None:
    created = make(client, alice, path)
    assert created["version"] == 1 and created["deleted_at"] is None
    assert created["created_at"] == created["updated_at"] == "2026-03-02T12:00:00Z"  # the server clock
    assert client.get(f"/{path}/{created['id']}", headers=alice).json() == created

    clock.advance(minutes=5)
    unchanged = client.put(f"/{path}/{created['id']}", json=editable(created), headers=alice).json()
    assert unchanged == created  # accepted, but nothing changed: no new version

    updated = client.put(f"/{path}/{created['id']}", json=editable(created, **EDITS[path]), headers=alice)
    assert updated.status_code == 200, updated.text
    updated = updated.json()
    assert (updated["version"], updated["created_at"], updated["updated_at"]) == (2, created["created_at"],
                                                                               "2026-03-02T12:05:00Z")

    for forged in ({"version": 99}, {"created_at": "2020-01-01T00:00:00Z"}, {"user_id": str(uuid.uuid4())},
                   {"deleted_at": None}):
        response = client.put(f"/{path}/{created['id']}", json={**editable(updated), **forged}, headers=alice)
        assert response.status_code == 422, forged


@pytest.mark.parametrize("path", RESOURCES)
def test_stale_updates_and_deletes_return_409_and_change_nothing(client, alice, path) -> None:
    created = make(client, alice, path)
    newer = client.put(f"/{path}/{created['id']}", json=editable(created, **EDITS[path]), headers=alice).json()

    stale_update = client.put(f"/{path}/{created['id']}", json=editable(created), headers=alice)
    stale_delete = client.delete(f"/{path}/{created['id']}", params={"base_version": 1}, headers=alice)

    for response in (stale_update, stale_delete):
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "version_conflict"
        assert (error["supplied_version"], error["current_version"]) == (1, 2)
        assert error["current"] == newer
    assert client.get(f"/{path}/{created['id']}", headers=alice).json() == newer


@pytest.mark.parametrize("path", RESOURCES)
def test_soft_deletion_keeps_a_tombstone(client, alice, clock, path) -> None:
    created = make(client, alice, path)
    clock.advance(minutes=1)
    tombstone = client.delete(f"/{path}/{created['id']}", params={"base_version": 1}, headers=alice)
    assert tombstone.status_code == 200
    tombstone = tombstone.json()
    assert tombstone["version"] == 2 and tombstone["deleted_at"] == "2026-03-02T12:01:00Z"

    assert client.get(f"/{path}/{created['id']}", headers=alice).status_code == 404
    assert client.get(f"/{path}/{created['id']}", params={"include_deleted": True}, headers=alice).json() == tombstone
    assert created["id"] not in [r["id"] for r in client.get(f"/{path}", headers=alice).json()["items"]]
    listed = client.get(f"/{path}", params={"include_deleted": True}, headers=alice).json()["items"]
    assert tombstone in listed

    revive = client.put(f"/{path}/{created['id']}", json=editable(created, base_version=2), headers=alice)
    again = client.delete(f"/{path}/{created['id']}", params={"base_version": 2}, headers=alice)
    for response in (revive, again):
        assert response.status_code == 409 and response.json()["error"]["code"] == "deleted"
        assert response.json()["error"]["current"] == tombstone
    content = {k: v for k, v in editable(created).items() if k != "base_version"}
    recreate = client.post(f"/{path}", json={**content, "id": created["id"]}, headers=alice)
    assert recreate.status_code == 409  # an id is never reused, not even after deletion
    assert recreate.json()["error"]["current"] == tombstone


def test_client_ids_are_kept_and_duplicates_conflict(client, alice) -> None:
    chosen = str(uuid.uuid4())
    created = create(client, alice, "tasks", task_payload(id=chosen))
    assert created["id"] == chosen
    duplicate = client.post("/tasks", json=task_payload(id=chosen, name="Other"), headers=alice)
    assert duplicate.status_code == 409 and duplicate.json()["error"]["current"] == created


def test_collections_are_bounded_and_paginated(client, alice) -> None:
    ids = sorted(create(client, alice, "projects", {"name": f"P{i}"})["id"] for i in range(5))
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/projects", params=params, headers=alice).json()
        assert len(page["items"]) <= 2
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids
    assert client.get("/projects", params={"limit": 501}, headers=alice).status_code == 422
    assert client.get("/projects", params={"limit": 0}, headers=alice).status_code == 422
    assert client.get("/projects", params={"cursor": "%%%"}, headers=alice).status_code == 422


# -----------------------------------------------------------------------------
# Content validation and relationship policies
# -----------------------------------------------------------------------------


def test_content_is_validated_with_the_canonical_rules(client, alice) -> None:
    bad = [
        ("tasks", task_payload(priority=11)),
        ("tasks", task_payload(deadline="2026-03-03T17:00:00")),  # naive
        ("tasks", task_payload(recurrence={"frequency": "daily", "weekdays": [1]})),
        ("fixed-blocks", block_payload(planned_end="2026-03-01T00:00:00Z")),
        ("fixed-blocks", block_payload(timezone="Mars/Olympus")),
        ("preferences", preference_payload(scope="date")),
        ("schedule-generations", generation_payload(planned_date="2026-04-01")),
    ]
    for path, payload in bad:
        assert client.post(f"/{path}", json=payload, headers=alice).status_code == 422, (path, payload)


def test_task_fields_round_trip_exactly(client, alice) -> None:
    project = create(client, alice, "projects", {"name": "P"})
    dependency = create(client, alice, "tasks", task_payload(name="First"))
    payload = task_payload(
        project_id=project["id"], tags=["a,b", "c"], required=True, required_date="2026-03-03",
        preferred_dates=["2026-03-02", "2026-03-04"], preferred_time_window={"start_minute": 613, "end_minute": 1440},
        dependency_ids=[dependency["id"]], deadline="2026-03-05T17:00:00-05:00",
        recurrence={"frequency": "weekly", "interval": 2, "weekdays": [4, 0], "day_of_month": None,
                    "end_date": "2026-06-01", "count": None},
    )
    task = create(client, alice, "tasks", payload)
    assert task["deadline"] == "2026-03-05T17:00:00-05:00"  # the original offset is kept
    assert task["recurrence"]["weekdays"] == [0, 4] and task["dependency_ids"] == [dependency["id"]]
    assert {k: task[k] for k in ("tags", "preferred_dates", "preferred_time_window")} == {
        k: payload[k] for k in ("tags", "preferred_dates", "preferred_time_window")
    }


def test_task_references_must_be_live_records_of_the_caller(client, alice) -> None:
    project = create(client, alice, "projects", {"name": "P"})
    dependency = create(client, alice, "tasks", task_payload(name="Dep"))
    client.delete(f"/projects/{project['id']}", params={"base_version": 1}, headers=alice)

    for payload in (task_payload(project_id=project["id"]), task_payload(project_id=str(uuid.uuid4())),
                    task_payload(dependency_ids=[str(uuid.uuid4())])):
        response = client.post("/tasks", json=payload, headers=alice)
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_reference"
    task = create(client, alice, "tasks", task_payload(dependency_ids=[dependency["id"]]))
    self_dependency = client.put(f"/tasks/{task['id']}", json=editable(task, dependency_ids=[task["id"]]), headers=alice)
    assert self_dependency.status_code == 422


def test_deletion_policies_protect_relationships(client, alice) -> None:
    project = create(client, alice, "projects", {"name": "P"})
    dependency = create(client, alice, "tasks", task_payload(name="Dep", project_id=project["id"]))
    dependent = create(client, alice, "tasks", task_payload(name="Uses dep", dependency_ids=[dependency["id"]]))

    in_use = client.delete(f"/projects/{project['id']}", params={"base_version": 1}, headers=alice)
    blocked = client.delete(f"/tasks/{dependency['id']}", params={"base_version": 1}, headers=alice)
    assert in_use.json()["error"]["code"] == blocked.json()["error"]["code"] == "in_use"

    client.delete(f"/tasks/{dependent['id']}", params={"base_version": 1}, headers=alice)
    assert client.delete(f"/tasks/{dependency['id']}", params={"base_version": 1}, headers=alice).status_code == 200
    assert client.delete(f"/projects/{project['id']}", params={"base_version": 1}, headers=alice).status_code == 200


def test_deleting_a_task_tombstones_its_placements_but_not_history(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload())
    placement = create(client, alice, "placements", placement_payload(task["id"]))
    execution = create(client, alice, "executions", {
        "task_id": task["id"], "scheduled_task_id": placement["id"], "task_name": "Study", "category": "study",
        "planned_duration": 60, "priority": 5,
    })

    client.delete(f"/tasks/{task['id']}", params={"base_version": 1}, headers=alice)

    assert client.get(f"/placements/{placement['id']}", headers=alice).status_code == 404
    tombstone = client.get(f"/placements/{placement['id']}", params={"include_deleted": True}, headers=alice).json()
    assert tombstone["version"] == 2 and tombstone["deleted_at"] is not None
    history = client.get(f"/executions/{execution['id']}", headers=alice).json()
    assert history == execution  # untouched: snapshot, references, version


def test_a_history_linked_placement_cannot_move_to_another_task(client, alice) -> None:
    task, other = create(client, alice, "tasks", task_payload()), create(client, alice, "tasks", task_payload(name="B"))
    placement = create(client, alice, "placements", placement_payload(task["id"]))
    moved = client.put(f"/placements/{placement['id']}", json=editable(placement, task_id=other["id"]), headers=alice)
    assert moved.status_code == 200  # no history yet
    create(client, alice, "executions", {"task_id": other["id"], "scheduled_task_id": placement["id"],
                                         "task_name": "B", "category": "study", "planned_duration": 60, "priority": 5})
    back = client.put(f"/placements/{placement['id']}", json=editable(moved.json(), task_id=task["id"]), headers=alice)
    assert back.status_code == 409 and back.json()["error"]["code"] == "in_use"


def test_one_live_preference_layer_per_scope_and_one_schedule_record_per_date(client, alice) -> None:
    user_layer = create(client, alice, "preferences", preference_payload())
    date_layer = create(client, alice, "preferences", preference_payload(scope="date", date="2026-03-02"))
    assert user_layer["overrides"]["category_multipliers"] == {"study": 2.0, "work": None}  # explicit null kept
    assert user_layer["overrides"]["optimizer_mode"] == "adhd_friendly"
    assert date_layer["date"] == "2026-03-02"

    duplicate = client.post("/preferences", json=preference_payload(), headers=alice)
    assert duplicate.status_code == 409 and duplicate.json()["error"]["current"] == user_layer
    move = client.put(f"/preferences/{user_layer['id']}", json=editable(user_layer, scope="date", date="2026-03-03"),
                      headers=alice)
    assert move.status_code == 422

    client.delete(f"/preferences/{user_layer['id']}", params={"base_version": 1}, headers=alice)
    assert client.post("/preferences", json=preference_payload(), headers=alice).status_code == 201

    record = create(client, alice, "schedule-generations", generation_payload())
    assert client.post("/schedule-generations", json=generation_payload(), headers=alice).status_code == 409
    assert record["engine_mode"] == "precise_greedy"
