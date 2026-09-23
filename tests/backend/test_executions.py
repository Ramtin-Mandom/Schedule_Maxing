"""The execution aggregate on the server: the desktop's lifecycle rules, session
consistency, completion metrics, terminal states, preconditions, feedback,
historical (unresolved) references, and tombstones that keep sessions."""

from __future__ import annotations

import uuid

import pytest

from tests.backend.conftest import create, execution_payload, placement_payload, task_payload


@pytest.fixture
def linked(client, alice):
    task = create(client, alice, "tasks", task_payload())
    placement = create(client, alice, "placements", placement_payload(task["id"]))
    return task, placement


def act(client, headers, execution: dict, action: str, **body):
    return client.post(f"/executions/{execution['id']}/actions/{action}",
                       json={"base_version": execution["version"], **body}, headers=headers)


def test_lifecycle_sessions_and_metrics_follow_the_desktop_rules(client, alice, clock, linked) -> None:
    task, placement = linked
    execution = create(client, alice, "executions", execution_payload(
        task["id"], placement["id"], canonical_planned_start="2026-03-02T12:00:00Z",
        canonical_planned_end="2026-03-02T13:00:00Z", canonical_planned_date="2026-03-02", canonical_timezone="UTC",
    ))
    assert (execution["status"], execution["version"], execution["sessions"]) == ("scheduled", 1, [])

    clock.advance(minutes=10)  # the server clock starts at 12:00
    started = act(client, alice, execution, "start").json()
    clock.advance(minutes=20)
    paused = act(client, alice, started, "pause").json()
    clock.advance(minutes=10)
    resumed = act(client, alice, paused, "resume").json()
    clock.advance(minutes=15)
    completed = act(client, alice, resumed, "complete").json()

    assert [e["version"] for e in (started, paused, resumed, completed)] == [2, 3, 4, 5]
    assert completed["status"] == "completed"
    assert completed["sessions"] == [
        {"started_at": "2026-03-02T12:10:00Z", "ended_at": "2026-03-02T12:30:00Z"},
        {"started_at": "2026-03-02T12:40:00Z", "ended_at": "2026-03-02T12:55:00Z"},
    ]
    assert completed["actual_active_duration_minutes"] == 35.0
    assert completed["duration_variance_minutes"] == -25.0
    assert completed["start_delay_minutes"] == 10.0
    assert completed["actual_first_start_at"] == "2026-03-02T12:10:00Z"
    assert completed["actual_final_end_at"] == "2026-03-02T12:55:00Z"

    for action in ("start", "resume", "pause", "complete", "skip", "cancel"):  # terminal: nothing is allowed
        response = act(client, alice, completed, action)
        assert response.status_code == 409 and response.json()["error"]["code"] == "invalid_transition"


def test_stale_actions_and_feedback_are_rejected(client, alice, linked) -> None:
    execution = create(client, alice, "executions", execution_payload(*[r["id"] for r in linked]))
    started = act(client, alice, execution, "start").json()

    stale = act(client, alice, execution, "skip")  # based on version 1
    assert stale.status_code == 409 and stale.json()["error"]["current"] == started

    rated = client.post(f"/executions/{execution['id']}/feedback",
                        json={"base_version": started["version"], "focus_rating": 4, "note": "went well"}, headers=alice)
    assert rated.json()["focus_rating"] == 4 and rated.json()["version"] == 3
    stale_feedback = client.post(f"/executions/{execution['id']}/feedback",
                                 json={"base_version": started["version"], "note": "stale"}, headers=alice)
    assert stale_feedback.status_code == 409
    missing_version = client.post(f"/executions/{execution['id']}/feedback", json={"note": "x"}, headers=alice)
    assert missing_version.status_code == 422
    assert client.get(f"/executions/{execution['id']}", headers=alice).json()["note"] == "went well"


def test_snapshots_cannot_be_overwritten(client, alice, linked) -> None:
    execution = create(client, alice, "executions", execution_payload(*[r["id"] for r in linked]))
    assert client.put(f"/executions/{execution['id']}", json={"task_name": "Rewritten"}, headers=alice).status_code == 405
    bogus = client.post(f"/executions/{execution['id']}/actions/teleport", json={"base_version": 1}, headers=alice)
    assert bogus.status_code == 404


def test_action_times_are_validated(client, alice, clock, linked) -> None:
    execution = create(client, alice, "executions", execution_payload(*[r["id"] for r in linked]))
    future = act(client, alice, execution, "start", at="2026-03-02T13:00:00Z")
    assert future.status_code == 422
    offline = act(client, alice, execution, "start", at="2026-03-02T08:00:00Z").json()  # recorded earlier, offline
    assert offline["sessions"][0]["started_at"] == "2026-03-02T08:00:00Z"
    before_session = act(client, alice, offline, "pause", at="2026-03-02T07:00:00Z")
    assert before_session.status_code == 422


@pytest.mark.parametrize("sessions, status", [
    ([{"started_at": "2026-03-02T09:00:00Z"}], "paused"),  # open session but not in progress
    ([{"started_at": "2026-03-02T09:00:00Z", "ended_at": "2026-03-02T09:30:00Z"}], "in_progress"),
    ([{"started_at": "2026-03-02T09:00:00Z"}, {"started_at": "2026-03-02T10:00:00Z"}], "in_progress"),
    ([{"started_at": "2026-03-02T09:00:00Z", "ended_at": "2026-03-02T10:00:00Z"},
      {"started_at": "2026-03-02T09:30:00Z", "ended_at": "2026-03-02T10:30:00Z"}], "completed"),  # overlap
    ([{"started_at": "2026-03-02T09:00:00Z", "ended_at": "2026-03-02T09:30:00Z"}], "scheduled"),
])
def test_uploaded_aggregates_must_be_consistent(client, alice, linked, sessions, status) -> None:
    payload = execution_payload(*[r["id"] for r in linked], status=status, sessions=sessions)
    assert client.post("/executions", json=payload, headers=alice).status_code == 422


def test_existing_history_can_be_uploaded_as_a_whole_aggregate(client, alice) -> None:
    history = execution_payload(
        status="completed", legacy_id="legacy-1", planned_date=1, planned_start=540, planned_end=600,
        sessions=[{"started_at": "2025-01-01T09:00:00Z", "ended_at": "2025-01-01T09:40:00Z"}],
        actual_active_duration_minutes=40.0, focus_rating=5,
    )
    created = create(client, alice, "executions", history)
    assert created["legacy_id"] == "legacy-1" and created["sessions"][0]["ended_at"] == "2025-01-01T09:40:00Z"
    assert client.post("/executions", json=history, headers=alice).status_code == 409  # legacy id stays unique
    assert client.post("/executions", json=execution_payload(legacy_id=str(uuid.uuid4())), headers=alice).status_code == 422


def test_historical_references_are_kept_without_fabricating_parents(client, alice) -> None:
    missing_task, missing_placement = str(uuid.uuid4()), str(uuid.uuid4())
    unresolved = client.post("/executions", json=execution_payload(missing_task, missing_placement), headers=alice)
    assert unresolved.status_code == 422 and unresolved.json()["error"]["code"] == "invalid_reference"

    historical = create(client, alice, "executions",
                        execution_payload(missing_task, missing_placement, historical_reference=True))
    assert (historical["task_id"], historical["scheduled_task_id"]) == (missing_task, missing_placement)
    assert client.get(f"/tasks/{missing_task}", headers=alice).status_code == 404  # nothing was invented


def test_links_must_be_consistent_and_one_execution_per_placement(client, alice, linked) -> None:
    task, placement = linked
    other = create(client, alice, "tasks", task_payload(name="Other"))
    mismatched = client.post("/executions", json=execution_payload(other["id"], placement["id"]), headers=alice)
    assert mismatched.status_code == 422
    create(client, alice, "executions", execution_payload(task["id"], placement["id"]))
    assert client.post("/executions", json=execution_payload(task["id"], placement["id"]), headers=alice).status_code == 409


def test_deleting_an_execution_keeps_a_tombstone_with_its_sessions(client, alice, linked) -> None:
    execution = create(client, alice, "executions", execution_payload(*[r["id"] for r in linked]))
    started = act(client, alice, execution, "start").json()
    assert client.delete(f"/executions/{execution['id']}", params={"base_version": 1}, headers=alice).status_code == 409
    tombstone = client.delete(f"/executions/{execution['id']}", params={"base_version": 2}, headers=alice).json()
    assert tombstone["deleted_at"] is not None and tombstone["sessions"] == started["sessions"]
    assert act(client, alice, tombstone, "pause").status_code == 409
    assert client.get(f"/executions/{execution['id']}", headers=alice).status_code == 404
