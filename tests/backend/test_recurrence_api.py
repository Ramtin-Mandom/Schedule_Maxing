"""Recurring series on the server (docs/recurrence.md): occurrences are owner-scoped tasks whose id derives from
(series, slot) and whose identity never changes; an older client's update cannot erase recurrence fields it
does not know; a repeated identical occurrence create converges while a different one conflicts; suppressed
slots arrive as tombstones and stay reserved; dependency rules (no one-off -> series edges, compatible
cadences, no cycles, skipped prerequisites) hold; the database enforces one record per (user, series, slot);
hosted generation expands first and logs every occurrence; a cross-date reschedule returns the re-dated
occurrence; and the server advertises the feature."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.planning.recurrence import occurrence_task_id
from backend import models
from backend.database import session_factory
from tests.backend.conftest import create, editable, task_payload
from tests.backend.test_sync_push import feed, op, push

MON = date(2026, 3, 2)


def series_payload(**overrides) -> dict:
    rule = {"frequency": "daily", "interval": 1, "start_date": MON.isoformat(), "timezone": "UTC",
            **overrides.pop("rule", {})}
    return task_payload(**{"name": "Walk", "category": "exercise", "estimated_duration_minutes": 30,
                           "recurrence": rule, **overrides})


def occurrence_payload(series: dict, slot: date, **overrides) -> dict:
    return {"id": str(occurrence_task_id(uuid.UUID(series["id"]), slot)), **task_payload(
        name=series["name"], category=series["category"], estimated_duration_minutes=30, required_date=slot.isoformat(),
        series_id=series["id"], occurrence_slot=slot.isoformat(), series_version=series["version"]), **overrides}


def test_series_and_occurrences_round_trip_and_identity_is_derived_and_immutable(client, alice) -> None:
    walk = create(client, alice, "tasks", series_payload())
    assert walk["recurrence"]["start_date"] == MON.isoformat() and walk["recurrence"]["timezone"] == "UTC"
    occurrence = create(client, alice, "tasks", occurrence_payload(walk, MON))
    assert (occurrence["series_id"], occurrence["occurrence_slot"], occurrence["occurrence_state"]) == (
        walk["id"], MON.isoformat(), None)

    wrong = {**occurrence_payload(walk, MON + timedelta(days=1)), "id": str(uuid.uuid4())}
    assert client.post("/tasks", json=wrong, headers=alice).status_code == 422  # the id is (series, slot)
    moved_slot = client.put(f"/tasks/{occurrence['id']}", headers=alice,
                            json=editable(occurrence, occurrence_slot=(MON + timedelta(days=1)).isoformat()))
    assert moved_slot.status_code == 422
    assert client.put(f"/tasks/{walk['id']}", headers=alice, json=editable(walk, recurrence=None)).status_code == 422


def test_an_older_clients_update_keeps_every_field_it_does_not_know(client, alice) -> None:
    walk = create(client, alice, "tasks", series_payload())
    occurrence = create(client, alice, "tasks", occurrence_payload(walk, MON))
    legacy_body = {key: value for key, value in editable(occurrence, priority=9).items()
                   if key not in ("series_id", "occurrence_slot", "occurrence_state", "series_version",
                                  "series_predecessor_id")}
    updated = client.put(f"/tasks/{occurrence['id']}", json=legacy_body, headers=alice).json()
    assert (updated["series_id"], updated["occurrence_slot"], updated["series_version"]) == (
        walk["id"], MON.isoformat(), 1)
    assert updated["priority"] == 9 and updated["occurrence_state"] == "modified"  # its own edit: a manual override

    old_rule = {key: value for key, value in walk["recurrence"].items() if key not in ("start_date", "timezone")}
    renamed = client.put(f"/tasks/{walk['id']}", headers=alice, json={
        **{key: value for key, value in editable(walk, name="Stroll").items() if key != "series_predecessor_id"},
        "recurrence": old_rule}).json()
    assert renamed["recurrence"]["start_date"] == MON.isoformat() and renamed["recurrence"]["timezone"] == "UTC"


def test_an_identical_occurrence_create_converges_and_a_different_one_conflicts(client, alice, engine) -> None:
    walk = create(client, alice, "tasks", series_payload())
    payload = occurrence_payload(walk, MON)
    occurrence_id = payload["id"]
    [first] = push(client, alice, op("task", "create", occurrence_id, payload={k: v for k, v in payload.items()
                                                                                if k != "id"}))
    log = feed(client, alice)
    [again] = push(client, alice, op("task", "create", occurrence_id, payload={k: v for k, v in payload.items()
                                                                                if k != "id"}))
    assert first["status"] == again["status"] == "applied"
    assert again["record"] == first["record"] and feed(client, alice) == log  # another device's same slot: no-op
    different = {**{k: v for k, v in payload.items() if k != "id"}, "priority": 1}
    [conflict] = push(client, alice, op("task", "create", occurrence_id, payload=different))
    assert conflict["status"] == "conflict" and conflict["error"]["code"] == "already_exists"
    with session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(models.Task).where(
            models.Task.series_id == uuid.UUID(walk["id"]))) == 1


def test_a_suppressed_slot_arrives_as_a_tombstone_and_stays_reserved(client, alice) -> None:
    walk = create(client, alice, "tasks", series_payload())
    skipped = occurrence_payload(walk, MON, occurrence_state="skipped")
    occurrence_id = skipped.pop("id")
    [created] = push(client, alice, op("task", "create", occurrence_id, payload=skipped))
    assert created["status"] == "applied" and created["record"]["deleted_at"] is not None
    assert feed(client, alice)[-1]["operation"] == "delete"
    live = {k: v for k, v in occurrence_payload(walk, MON).items() if k != "id"}
    [refused] = push(client, alice, op("task", "create", occurrence_id, payload=live))
    assert refused["status"] == "conflict"  # never minted again

    other = occurrence_payload(walk, MON + timedelta(days=1))
    other_id = other.pop("id")
    [made] = push(client, alice, op("task", "create", other_id, payload=other))
    [removed] = push(client, alice, op("task", "delete", other_id, base_version=made["record"]["version"],
                                       payload={"occurrence_state": "skipped"}))
    assert removed["record"]["occurrence_state"] == "skipped" and removed["record"]["deleted_at"] is not None


def test_dependency_rules_for_series_and_occurrences(client, alice) -> None:
    walk = create(client, alice, "tasks", series_payload())
    monthly = create(client, alice, "tasks", series_payload(name="Monthly", rule={"frequency": "monthly"}))
    one_off = client.post("/tasks", json=task_payload(dependency_ids=[walk["id"]]), headers=alice)
    assert one_off.status_code == 422 and "concrete occurrence" in one_off.json()["error"]["message"]
    mismatched = client.post("/tasks", json=series_payload(name="Run", dependency_ids=[monthly["id"]]), headers=alice)
    assert mismatched.status_code == 422
    run = create(client, alice, "tasks", series_payload(name="Run", dependency_ids=[walk["id"]]))
    cycle = client.put(f"/tasks/{walk['id']}", json=editable(walk, dependency_ids=[run["id"]]), headers=alice)
    assert cycle.status_code == 422 and "cycle" in cycle.json()["error"]["message"]

    prerequisite = create(client, alice, "tasks", occurrence_payload(walk, MON))
    dependent = create(client, alice, "tasks", occurrence_payload(run, MON, dependency_ids=[prerequisite["id"]]))
    skipped = client.delete(f"/tasks/{prerequisite['id']}", params={"base_version": prerequisite["version"]},
                            headers=alice)
    assert skipped.status_code == 200 and skipped.json()["occurrence_state"] == "deleted"  # a slot edge never blocks
    unchanged = client.put(f"/tasks/{dependent['id']}", json=editable(dependent, priority=4), headers=alice)
    assert unchanged.status_code == 200  # its edge to the skipped prerequisite stays valid


def test_occurrences_are_owner_scoped(client, alice, bob) -> None:
    walk = create(client, alice, "tasks", series_payload())
    stolen = client.post("/tasks", json=occurrence_payload(walk, MON), headers=bob)
    assert stolen.status_code == 422 and stolen.json()["error"]["code"] == "invalid_reference"


def test_the_database_keeps_one_record_per_user_series_and_slot(client, alice, engine) -> None:
    walk = create(client, alice, "tasks", series_payload())
    create(client, alice, "tasks", occurrence_payload(walk, MON))
    with session_factory(engine)() as session:
        stored = session.scalars(select(models.Task).where(models.Task.series_id == uuid.UUID(walk["id"]))).one()
        session.add(models.Task(
            user_id=stored.user_id, id=uuid.uuid4(), created_at=stored.created_at, updated_at=stored.updated_at,
            version=1, name="Twin", category="x", estimated_duration_minutes=5, priority=5, points=1, required=False,
            series_id=stored.series_id, occurrence_slot=stored.occurrence_slot))
        with pytest.raises(IntegrityError):
            session.flush()


def test_hosted_generation_expands_first_and_logs_every_occurrence(client, alice) -> None:
    walk = create(client, alice, "tasks", series_payload())
    body = {"start_date": MON.isoformat(), "end_date": (MON + timedelta(days=2)).isoformat(), "timezone": "UTC"}
    response = client.post("/planning/generate", json=body, headers=alice)
    assert response.status_code == 200, response.text
    expected = {str(occurrence_task_id(uuid.UUID(walk["id"]), MON + timedelta(days=offset))) for offset in range(3)}
    placed = {p["task_id"] for day in response.json()["days"] for p in day["placements"]}
    assert placed == expected
    logged = {change["entity_id"] for change in feed(client, alice) if change["entity_type"] == "task"}
    assert expected <= logged
    log = feed(client, alice)
    again = client.post("/planning/generate", json=body, headers=alice).json()
    assert again["status"] == "already_current" and feed(client, alice) == log


def test_a_cross_date_reschedule_returns_the_re_dated_occurrence(client, alice) -> None:
    walk = create(client, alice, "tasks", series_payload())
    body = {"start_date": MON.isoformat(), "end_date": MON.isoformat(), "timezone": "UTC"}
    [day] = client.post("/planning/generate", json=body, headers=alice).json()["days"]
    [placement] = day["placements"]
    tuesday = MON + timedelta(days=1)
    payload = {"replacement_id": str(uuid.uuid4()), "planned_date": tuesday.isoformat(), "timezone": "UTC",
               "planned_start": f"{tuesday}T18:00:00+00:00", "planned_end": f"{tuesday}T18:30:00+00:00"}
    [moved] = push(client, alice, op("placement", "action", placement["id"], base_version=placement["version"],
                                     action="reschedule", payload=payload))
    assert moved["status"] == "applied", moved
    [task] = [item["record"] for item in moved["related"] if item["entity_type"] == "task"]
    assert (task["id"], task["required_date"], task["occurrence_slot"], task["occurrence_state"]) == (
        placement["task_id"], tuesday.isoformat(), MON.isoformat(), "modified")
    assert walk["id"] == task["series_id"]


def test_the_server_advertises_its_sync_protocol_features(client, alice) -> None:
    capabilities = client.get("/sync/capabilities", headers=alice).json()
    assert capabilities["protocol_version"] >= 2 and "recurrence_occurrences" in capabilities["features"]
    assert client.get("/sync/capabilities").status_code == 401


def test_the_planning_api_expands_and_applies_scoped_changes_in_the_users_scope(client, alice, bob) -> None:
    walk = create(client, alice, "tasks", series_payload())
    body = {"start_date": MON.isoformat(), "end_date": (MON + timedelta(days=4)).isoformat()}
    expanded = client.post("/planning/recurrence/expand", json=body, headers=alice).json()
    assert len(expanded["created"]) == 5 and expanded["needs_configuration"] == []
    assert client.post("/planning/recurrence/expand", json=body, headers=alice).json()["created"] == []
    assert client.post("/planning/recurrence/expand", json=body, headers=bob).json()["created"] == []  # Bob's own

    by_slot = {task["occurrence_slot"]: task for task in expanded["created"]}
    first = by_slot[MON.isoformat()]
    skipped = client.post(f"/planning/occurrences/{first['id']}/delete", headers=alice,
                          json={"base_version": first["version"], "skip": True}).json()
    assert skipped["occurrence"]["occurrence_state"] == "skipped"
    second = by_slot[(MON + timedelta(days=1)).isoformat()]
    edited = client.post(f"/planning/occurrences/{second['id']}/edit", headers=alice, json={
        "base_version": second["version"], "task": {**task_payload(name="Long walk", estimated_duration_minutes=60,
                                                                  category="exercise")}}).json()
    assert edited["occurrence"]["occurrence_state"] == "modified" and edited["occurrence"]["occurrence_slot"] == \
        second["occurrence_slot"]  # identity kept although the request did not repeat it

    series = client.get(f"/tasks/{walk['id']}", headers=alice).json()
    split = client.post(f"/planning/series/{walk['id']}/edit", headers=alice, json={
        "base_version": series["version"], "scope": "future", "cutoff": (MON + timedelta(days=3)).isoformat(),
        "definition": {**task_payload(name="Run", category="exercise", estimated_duration_minutes=30),
                       "recurrence": series["recurrence"]}})
    assert split.status_code == 200, split.text
    change = split.json()
    assert change["successor"]["series_predecessor_id"] == walk["id"]
    assert {task["occurrence_slot"] for task in change["superseded"]} == {
        (MON + timedelta(days=3)).isoformat(), (MON + timedelta(days=4)).isoformat()}
    stale = client.post(f"/planning/series/{walk['id']}/delete", headers=alice,
                        json={"base_version": series["version"], "scope": "series"})
    assert stale.status_code == 409  # the split moved the series on: a stale delete changes nothing
    elsewhere = client.post(f"/planning/series/{walk['id']}/delete", headers=bob,
                            json={"base_version": 2, "scope": "series"})
    assert elsewhere.status_code == 404  # another user's series does not exist for Bob
    too_wide = client.post("/planning/recurrence/expand", headers=alice, json={
        "start_date": MON.isoformat(), "end_date": (MON + timedelta(days=90)).isoformat()})
    assert too_wide.status_code == 422


def test_a_new_occurrence_must_match_its_series_as_it_is_now(client, alice) -> None:
    walk = create(client, alice, "tasks", series_payload(rule={"count": 3}))
    renamed = client.put(f"/tasks/{walk['id']}", headers=alice, json=editable(walk, name="Stroll")).json()

    stale = client.post("/tasks", json=occurrence_payload(walk, MON), headers=alice)  # expanded from "Walk"
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "series_changed" and error["current"]["name"] == "Stroll"
    beyond = client.post("/tasks", json=occurrence_payload(renamed, MON + timedelta(days=3)), headers=alice)
    assert beyond.status_code == 409 and "no longer one of its dates" in beyond.json()["error"]["message"]

    current = client.post("/tasks", json=occurrence_payload(renamed, MON), headers=alice)
    assert current.status_code == 201
    exception = occurrence_payload(walk, MON + timedelta(days=1), occurrence_state="modified")
    assert client.post("/tasks", json=exception, headers=alice).status_code == 201  # the user's own content
    skipped = occurrence_payload(walk, MON + timedelta(days=2), occurrence_state="skipped")
    assert client.post("/tasks", json=skipped, headers=alice).status_code == 201  # a tombstone needs no match

    gone = create(client, alice, "tasks", series_payload(name="Swim"))
    deleted = client.delete(f"/tasks/{gone['id']}", headers=alice, params={"base_version": gone["version"]})
    assert deleted.status_code == 200, deleted.text
    late = client.post("/tasks", json=occurrence_payload(gone, MON), headers=alice)  # a device expanded offline
    assert late.status_code == 409 and late.json()["error"]["current"]["deleted_at"] is not None
