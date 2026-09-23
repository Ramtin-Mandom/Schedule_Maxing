"""The change log: every accepted mutation -- creates, updates, tombstones, compound
changes, execution actions -- writes entries in the same transaction, with
per-user, gap-free, strictly increasing sequence numbers; rejected or failed
mutations write nothing and allocate nothing."""

from __future__ import annotations

import pytest

from backend import models
from backend.mutations import Mutator
from tests.backend.conftest import create, editable, execution_payload, placement_payload, task_payload


def changes(client, headers, after: int = 0, limit: int | None = None) -> dict:
    params = {"after": after, **({"limit": limit} if limit else {})}
    return client.get("/changes", params=params, headers=headers).json()


def test_every_accepted_mutation_is_logged_with_its_record(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload())
    updated = client.put(f"/tasks/{task['id']}", json=editable(task, priority=8), headers=alice).json()
    client.put(f"/tasks/{task['id']}", json=editable(updated), headers=alice)  # no-op: not logged
    placement = create(client, alice, "placements", placement_payload(task["id"]))
    execution = create(client, alice, "executions", execution_payload(task["id"], placement["id"]))
    started = client.post(f"/executions/{execution['id']}/actions/start", json={"base_version": 1},
                          headers=alice).json()

    log = changes(client, alice)["changes"]
    assert [c["seq"] for c in log] == [1, 2, 3, 4, 5]
    assert [(c["entity_type"], c["operation"], c["version"]) for c in log] == [
        ("task", "upsert", 1), ("task", "upsert", 2), ("placement", "upsert", 1),
        ("execution", "upsert", 1), ("execution", "upsert", 2),
    ]
    assert [c["record"] for c in log] == [task, updated, placement, execution, started]
    assert log[4]["record"]["sessions"] == started["sessions"]  # the aggregate, sessions included


def test_compound_deletes_log_every_tombstone_in_one_transaction(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload())
    first = create(client, alice, "placements", placement_payload(task["id"]))
    second = create(client, alice, "placements", placement_payload(
        task["id"], planned_start="2026-03-02T14:00:00Z", planned_end="2026-03-02T15:00:00Z"))

    tombstone = client.delete(f"/tasks/{task['id']}", params={"base_version": 1}, headers=alice).json()

    tail = changes(client, alice, after=3)["changes"]
    assert sorted((c["entity_type"], c["operation"]) for c in tail) == [
        ("placement", "delete"), ("placement", "delete"), ("task", "delete")
    ]
    assert tail[-1]["record"] == tombstone and tail[-1]["recorded_at"] == tombstone["deleted_at"]
    assert {c["entity_id"] for c in tail if c["entity_type"] == "placement"} == {first["id"], second["id"]}
    assert all(c["record"]["deleted_at"] is not None for c in tail)


def test_rejected_mutations_write_nothing_and_allocate_nothing(client, alice, engine) -> None:
    task = create(client, alice, "tasks", task_payload())
    create(client, alice, "tasks", task_payload(name="Dependent", dependency_ids=[task["id"]]))

    assert client.delete(f"/tasks/{task['id']}", params={"base_version": 1}, headers=alice).status_code == 409
    stale = {**editable(task, priority=2), "base_version": 7}
    assert client.put(f"/tasks/{task['id']}", json=stale, headers=alice).status_code == 409
    assert client.post("/tasks", json=task_payload(project_id=task["id"]), headers=alice).status_code == 422

    assert changes(client, alice)["cursor"] == 2
    project = create(client, alice, "projects", {"name": "Next"})
    assert changes(client, alice, after=2)["changes"][0]["seq"] == 3 and project


def test_a_failure_inside_a_mutation_rolls_back_the_record_the_log_and_the_counter(
    client, alice, engine, monkeypatch
) -> None:
    task = create(client, alice, "tasks", task_payload())
    create(client, alice, "placements", placement_payload(task["id"]))
    original_log = Mutator.log
    calls = {"count": 0}

    def fail_on_second_entry(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:  # the placement tombstone was logged, the task's is not yet
            raise RuntimeError("storage failure (injected)")
        return original_log(self, *args, **kwargs)

    monkeypatch.setattr(Mutator, "log", fail_on_second_entry)
    with pytest.raises(RuntimeError, match="injected"):
        client.delete(f"/tasks/{task['id']}", params={"base_version": 1}, headers=alice)
    monkeypatch.undo()

    assert client.get(f"/tasks/{task['id']}", headers=alice).json() == task
    assert len(client.get("/placements", headers=alice).json()["items"]) == 1
    assert changes(client, alice)["cursor"] == 2
    with engine.connect() as connection:
        assert connection.execute(models.User.__table__.select()).first().change_seq == 2


def test_the_feed_is_paged_by_sequence_number(client, alice) -> None:
    for index in range(5):
        create(client, alice, "projects", {"name": f"P{index}"})
    first = changes(client, alice, limit=2)
    second = changes(client, alice, after=first["cursor"], limit=2)
    last = changes(client, alice, after=second["cursor"], limit=2)
    empty = changes(client, alice, after=last["cursor"], limit=2)

    assert [c["seq"] for c in first["changes"] + second["changes"] + last["changes"]] == [1, 2, 3, 4, 5]
    assert (first["has_more"], second["has_more"], last["has_more"]) == (True, True, False)
    assert empty == {"changes": [], "cursor": 5, "has_more": False}


def test_equal_timestamps_never_affect_ordering(client, alice) -> None:
    """Many changes at one identical server instant are still totally ordered by seq, never by time."""
    for index in range(4):
        create(client, alice, "projects", {"name": f"P{index}"})  # the fake clock does not move
    log = changes(client, alice)["changes"]
    assert len({c["recorded_at"] for c in log}) == 1
    assert [c["seq"] for c in log] == [1, 2, 3, 4]
    assert [c["record"]["name"] for c in log] == ["P0", "P1", "P2", "P3"]
