"""The Day board's outcomes on the server: POST /placements/{id}/outcome moves one of the
caller's placements between pending, completed and uncompleted through its execution's
lifecycle actions (created on first use, never twice), in one transaction; preconditions and
another user's ids are refused without changing anything; the generic action endpoint knows
"complete" from scheduled and "reopen"; and every step stays in the change log's history."""

from __future__ import annotations

import uuid

from tests.backend.conftest import create, execution_payload, placement_payload, task_payload


def plan(client, headers, name: str = "Study") -> tuple[dict, dict]:
    task = create(client, headers, "tasks", task_payload(name=name))
    return task, create(client, headers, "placements", placement_payload(task["id"]))


def outcome(client, headers, placement: dict, value: str, **body):
    return client.post(f"/placements/{placement['id']}/outcome", json={"outcome": value, **body}, headers=headers)


def test_outcomes_walk_every_column_with_one_execution(client, alice) -> None:
    task, placement = plan(client, alice)
    unchanged = outcome(client, alice, placement, "pending")
    assert unchanged.status_code == 200 and unchanged.json() == {
        "placement_id": placement["id"], "outcome": "pending", "execution": None}  # nothing to record
    assert client.get("/executions", headers=alice).json()["items"] == []

    done = outcome(client, alice, placement, "completed").json()
    execution = done["execution"]
    assert done["outcome"] == "completed" and execution["status"] == "completed"
    assert execution["scheduled_task_id"] == placement["id"] and execution["task_id"] == task["id"]
    assert execution["actual_active_duration_minutes"] is None  # done without timing: unknown, not 0
    assert execution["actual_final_end_at"] is not None

    back = outcome(client, alice, placement, "pending", base_version=execution["version"]).json()
    assert back["outcome"] == "pending" and back["execution"]["status"] == "scheduled"
    assert back["execution"]["actual_final_end_at"] is None and back["execution"]["id"] == execution["id"]
    missed = outcome(client, alice, placement, "uncompleted").json()
    assert missed["execution"]["status"] == "skipped"
    again = outcome(client, alice, placement, "completed").json()  # uncompleted -> completed: reopen + complete
    assert again["execution"]["status"] == "completed"
    assert [item["id"] for item in client.get("/executions", headers=alice).json()["items"]] == [execution["id"]]


def test_preconditions_refuse_a_stale_view_and_change_nothing(client, alice) -> None:
    _, placement = plan(client, alice)
    shown_nothing = outcome(client, alice, placement, "completed", base_version=None)  # showed no execution
    assert shown_nothing.status_code == 200
    stale = outcome(client, alice, placement, "uncompleted", base_version=None)  # one exists now
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert stale.json()["error"]["current"]["status"] == "completed"
    wrong = outcome(client, alice, placement, "pending", base_version=99)
    assert wrong.status_code == 409
    [execution] = client.get("/executions", headers=alice).json()["items"]
    assert execution["status"] == "completed" and execution["version"] == shown_nothing.json()["execution"]["version"]


def test_another_users_placement_is_not_found_and_untouched(client, alice, bob) -> None:
    _, placement = plan(client, alice)
    for value in ("completed", "uncompleted", "pending"):
        attempt = outcome(client, bob, placement, value)
        assert attempt.status_code == 404
    assert client.get("/executions", headers=alice).json()["items"] == []
    assert client.get("/executions", headers=bob).json()["items"] == []
    assert outcome(client, bob, {"id": str(uuid.uuid4())}, "completed").status_code == 404
    unauthenticated = client.post(f"/placements/{placement['id']}/outcome", json={"outcome": "completed"})
    assert unauthenticated.status_code == 401


def test_invalid_requests_are_refused(client, alice) -> None:
    _, placement = plan(client, alice)
    assert outcome(client, alice, placement, "missed").status_code == 422  # one canonical vocabulary
    assert client.post(f"/placements/{placement['id']}/outcome", json={"outcome": "completed", "extra": 1},
                       headers=alice).status_code == 422
    execution = create(client, alice, "executions", execution_payload(placement["task_id"], placement["id"]))
    client.post(f"/executions/{execution['id']}/actions/cancel", json={"base_version": 1}, headers=alice)
    cancelled = outcome(client, alice, placement, "pending")
    assert cancelled.status_code == 409 and cancelled.json()["error"]["code"] == "invalid_transition"


def test_generic_actions_complete_without_timing_and_reopen(client, alice) -> None:
    task, placement = plan(client, alice)
    execution = create(client, alice, "executions", execution_payload(task["id"], placement["id"]))
    done = client.post(f"/executions/{execution['id']}/actions/complete", json={"base_version": 1}, headers=alice)
    assert done.status_code == 200 and done.json()["status"] == "completed"
    reopened = client.post(f"/executions/{execution['id']}/actions/reopen", json={"base_version": 2}, headers=alice)
    assert reopened.status_code == 200 and reopened.json()["status"] == "scheduled"
    twice = client.post(f"/executions/{execution['id']}/actions/reopen", json={"base_version": 3}, headers=alice)
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "invalid_transition"

    started = client.post(f"/executions/{execution['id']}/actions/start", json={"base_version": 3}, headers=alice)
    first_start = started.json()["actual_first_start_at"]
    finished = client.post(f"/executions/{execution['id']}/actions/complete", json={"base_version": 4}, headers=alice)
    reopened = client.post(f"/executions/{execution['id']}/actions/reopen", json={"base_version": 5}, headers=alice)
    body = reopened.json()
    assert finished.json()["actual_active_duration_minutes"] is not None
    assert body["status"] == "paused" and len(body["sessions"]) == 1  # work stays recorded; it is not finished
    assert body["actual_first_start_at"] == first_start and body["actual_final_end_at"] is None
    assert body["actual_active_duration_minutes"] is None


def test_every_step_stays_in_the_change_log(client, alice) -> None:
    _, placement = plan(client, alice)
    done = outcome(client, alice, placement, "completed").json()["execution"]
    outcome(client, alice, placement, "pending", base_version=done["version"])
    changes = client.get("/changes", params={"after": 0}, headers=alice).json()["changes"]
    statuses = [change["record"]["status"] for change in changes if change["entity_type"] == "execution"]
    assert statuses == ["scheduled", "completed", "scheduled"]  # the completion is history, not overwritten
