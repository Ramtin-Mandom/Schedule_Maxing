"""POST /sync/push: idempotent operations through the shared mutation path,
op-id reuse detection, atomic groups vs. independent operations, recorded
conflicts/rejections, user scoping, and request bounds."""

from __future__ import annotations

import uuid

from tests.backend.conftest import create, execution_payload, placement_payload, task_payload


def op(entity_type: str, kind: str, entity_id=None, base_version=None, payload=None, **extra) -> dict:
    return {"op_id": str(uuid.uuid4()), "entity_type": entity_type, "entity_id": str(entity_id or uuid.uuid4()),
            "kind": kind, "base_version": base_version, "payload": payload, **extra}


def push(client, headers, *operations) -> list[dict]:
    response = client.post("/sync/push", json={"operations": list(operations)}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def feed(client, headers) -> list[dict]:
    return client.get("/changes", params={"limit": 500}, headers=headers).json()["changes"]


def test_operations_apply_through_the_shared_mutation_path(client, alice) -> None:
    task_id = uuid.uuid4()
    create_task = op("task", "create", task_id, payload=task_payload())
    [created] = push(client, alice, create_task)
    assert created["status"] == "applied" and created["record"]["version"] == 1

    rename = op("task", "update", task_id, base_version=1, payload=task_payload(name="Renamed"))
    delete = op("task", "delete", task_id, base_version=2)
    updated, deleted = push(client, alice, rename, delete)
    assert updated["record"]["version"] == 2 and deleted["record"]["deleted_at"] is not None
    assert [(c["operation"], c["version"]) for c in feed(client, alice)] == [("upsert", 1), ("upsert", 2), ("delete", 3)]


def test_a_retry_after_a_lost_response_changes_nothing(client, alice) -> None:
    task_id, execution_id = uuid.uuid4(), uuid.uuid4()
    batch = [
        op("task", "create", task_id, payload=task_payload()),
        op("execution", "create", execution_id, payload=execution_payload(str(task_id))),
        op("execution", "action", execution_id, base_version=1, action="start", payload={}),
    ]
    first = push(client, alice, *batch)
    log_before = feed(client, alice)

    retried = push(client, alice, *batch)  # the client never saw `first`

    assert retried == first
    assert feed(client, alice) == log_before  # no new change-log entries, versions, or sessions
    execution = client.get(f"/executions/{execution_id}", headers=alice).json()
    assert execution["version"] == 2 and len(execution["sessions"]) == 1


def test_reusing_an_op_id_for_a_different_operation_is_refused(client, alice) -> None:
    create_task = op("task", "create", payload=task_payload())
    push(client, alice, create_task)
    forged = {**create_task, "payload": task_payload(name="Something else")}

    [result] = push(client, alice, forged)

    assert result["status"] == "rejected" and result["error"]["code"] == "op_id_reused"
    assert client.get(f"/tasks/{create_task['entity_id']}", headers=alice).json()["name"] == "Study"


def test_a_group_is_all_or_nothing(client, alice) -> None:
    task_id, execution_id = uuid.uuid4(), uuid.uuid4()
    push(client, alice, op("task", "create", task_id, payload=task_payload()),
         op("execution", "create", execution_id, payload=execution_payload(str(task_id))))
    group = str(uuid.uuid4())
    start = op("execution", "action", execution_id, base_version=1, action="start", payload={}, group=group)
    invalid = op("execution", "action", execution_id, base_version=2, action="resume", payload={}, group=group)
    before = feed(client, alice)

    started, resumed = push(client, alice, start, invalid)

    assert resumed["status"] == "conflict" and resumed["error"]["code"] == "invalid_transition"
    assert started["status"] == "conflict" and started["error"]["code"] == "group_failed"
    assert client.get(f"/executions/{execution_id}", headers=alice).json()["status"] == "scheduled"
    assert feed(client, alice) == before
    assert push(client, alice, start, invalid) == [started, resumed]  # the recorded outcome, stable on retry


def test_independent_operations_succeed_or_fail_individually(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload())
    results = push(
        client, alice,
        op("project", "create", payload={"name": "First"}),
        op("task", "update", task["id"], base_version=5, payload=task_payload()),  # stale
        op("placement", "create", payload=placement_payload(str(uuid.uuid4()))),  # missing task
        op("project", "create", payload={"name": "Last"}),
    )
    assert [r["status"] for r in results] == ["applied", "conflict", "rejected", "applied"]
    assert results[1]["error"]["current"] == task and results[1]["error"]["current_version"] == 1
    assert results[2]["error"]["code"] == "invalid_reference"
    assert [c["seq"] for c in feed(client, alice)] == [1, 2, 3]  # no gap from the failed operations


def test_rejections_are_recorded_even_though_their_mutation_rolled_back(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload())
    create(client, alice, "tasks", task_payload(name="Dependent", dependency_ids=[task["id"]]))
    delete = op("task", "delete", task["id"], base_version=1)

    [refused] = push(client, alice, delete)
    [again] = push(client, alice, delete)

    assert refused == again and refused["status"] == "conflict" and refused["error"]["code"] == "in_use"
    assert client.get(f"/tasks/{task['id']}", headers=alice).json() == task


def test_push_is_scoped_to_the_token_user(client, alice, bob) -> None:
    alices = create(client, alice, "tasks", task_payload())
    shared_op_id = str(uuid.uuid4())
    results = push(
        client, bob,
        op("task", "update", alices["id"], base_version=1, payload=task_payload(name="Hijacked")),
        op("placement", "create", payload=placement_payload(alices["id"])),
        {**op("project", "create", payload={"name": "Bob's"}), "op_id": shared_op_id},
    )
    assert [r["status"] for r in results] == ["rejected", "rejected", "applied"]
    assert results[0]["error"]["code"] == "not_found"
    assert client.get(f"/tasks/{alices['id']}", headers=alice).json() == alices
    # op ids are per user: Alice may use the same id for her own operation.
    [mine] = push(client, alice, {**op("project", "create", payload={"name": "Alice's"}), "op_id": shared_op_id})
    assert mine["status"] == "applied" and mine["record"]["name"] == "Alice's"


def test_requests_are_bounded_and_well_formed(client, alice) -> None:
    too_many = [op("project", "create", payload={"name": str(i)}) for i in range(201)]
    assert client.post("/sync/push", json={"operations": too_many}, headers=alice).status_code == 422
    group = str(uuid.uuid4())
    split = [op("project", "create", payload={"name": "a"}, group=group), op("project", "create", payload={"name": "b"}),
             op("project", "create", payload={"name": "c"}, group=group)]
    assert client.post("/sync/push", json={"operations": split}, headers=alice).status_code == 422
    assert client.post("/sync/push", json={"operations": [op("task", "update", payload={})]},
                       headers=alice).status_code == 422  # an update needs base_version
    assert client.post("/sync/push", json={"operations": [op("project", "create", payload={"name": "x"})]}).status_code == 401
