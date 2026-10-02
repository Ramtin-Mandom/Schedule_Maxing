"""Manual placements on the server (docs/execution-rescheduling.md, "Manual
placements"): a hosted move's destination is manual and preserved, a full
generation keeps it, the snapshot names it, and the release endpoint releases
only the intent (version-checked); an older client's placement update cannot
erase origin or intent, and no update can grant intent; a blocking conflict
answers with structured problems and writes nothing; work outside the
generated dates is reported, never superseded; cancel reasons are recorded
(user by default, a system reason only on cancel); and the server advertises
the feature."""

from __future__ import annotations

from sqlalchemy import func, select

from backend import models
from backend.database import session_factory
from tests.backend.conftest import (
    block_payload,
    create,
    editable,
    execution_payload,
    placement_payload,
    task_payload,
)

DAY, NEXT = "2026-03-02", "2026-03-03"
RANGE = {"start_date": DAY, "end_date": NEXT, "timezone": "UTC"}


def generate(client, headers, **extra) -> dict:
    response = client.post("/planning/generate", json={**RANGE, "generate_end": DAY, **extra}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def snapshot(client, headers) -> dict:
    return client.get("/planning/snapshot", params=RANGE, headers=headers).json()


def placements_of(client, headers, task_id: str) -> list[dict]:
    return [p for p in snapshot(client, headers)["placements"] if p["task_id"] == task_id]


def move(client, headers, placement: dict, start: str, end: str, day: str = DAY) -> dict:
    response = client.post(f"/planning/placements/{placement['id']}/reschedule", headers=headers, json={
        "base_version": placement["version"], "planned_date": day, "timezone": "UTC",
        "planned_start": start, "planned_end": end})
    assert response.status_code == 200, response.text
    return response.json()["replacement"]


def change_count(engine) -> int:
    with session_factory(engine)() as session:
        return session.scalar(select(func.count()).select_from(models.ChangeLogEntry))


def test_a_hosted_move_is_kept_by_generation_until_released(client, alice) -> None:
    essay = create(client, alice, "tasks", task_payload(name="Essay", required_date=DAY))
    generate(client, alice)
    [placed] = placements_of(client, alice, essay["id"])
    assert (placed["origin"], placed["preserved"]) == ("generated", False)
    moved = move(client, alice, placed, f"{DAY}T15:00:00Z", f"{DAY}T16:00:00Z")
    assert (moved["origin"], moved["preserved"]) == ("manual", True)
    create(client, alice, "tasks", task_payload(name="Other", required_date=DAY))

    result = generate(client, alice, mode="full")
    assert moved["id"] in result["days"][0]["kept_placement_ids"]
    assert placements_of(client, alice, essay["id"]) == [moved]
    assert snapshot(client, alice)["preserved_placement_ids"] == [moved["id"]]

    stale = client.post(f"/planning/placements/{moved['id']}/release", json={"base_version": moved["version"] + 1},
                        headers=alice)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    released = client.post(f"/planning/placements/{moved['id']}/release", json={"base_version": moved["version"]},
                           headers=alice)
    assert released.status_code == 200, released.text
    body = released.json()
    assert (body["preserved"], body["version"], body["planned_start"]) == (
        False, moved["version"] + 1, moved["planned_start"])
    again = client.post(f"/planning/placements/{moved['id']}/release", json={"base_version": body["version"]},
                        headers=alice)
    assert again.status_code == 422  # nothing left to release
    assert snapshot(client, alice)["preserved_placement_ids"] == []


def test_an_update_keeps_origin_and_intent_and_can_only_release(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload())
    manual = create(client, alice, "placements", placement_payload(task["id"], origin="manual", preserved=True))
    legacy = {key: value for key, value in editable(manual, score=2.0).items() if key not in ("origin", "preserved")}
    updated = client.put(f"/placements/{manual['id']}", json=legacy, headers=alice).json()
    assert (updated["origin"], updated["preserved"], updated["score"]) == ("manual", True, 2.0)

    generated = create(client, alice, "placements", placement_payload(task["id"], origin="generated",
                                                                       planned_start="2026-03-02T15:00:00Z",
                                                                       planned_end="2026-03-02T16:00:00Z"))
    grant = client.put(f"/placements/{generated['id']}", headers=alice,
                       json=editable(generated, origin="manual", preserved=True))
    assert grant.status_code == 422  # a known origin never changes, and intent is never granted by an update
    released = client.put(f"/placements/{updated['id']}", json=editable(updated, preserved=False), headers=alice)
    assert released.status_code == 200 and released.json()["preserved"] is False
    regrant = client.put(f"/placements/{updated['id']}", json=editable(released.json(), preserved=True), headers=alice)
    assert regrant.status_code == 422
    unknown = create(client, alice, "placements", placement_payload(task["id"], planned_start="2026-03-02T17:00:00Z",
                                                                     planned_end="2026-03-02T18:00:00Z"))
    assert (unknown["origin"], unknown["preserved"]) == (None, False)
    invalid = client.post("/placements", headers=alice, json=placement_payload(
        task["id"], preserved=True, planned_start="2026-03-02T19:00:00Z", planned_end="2026-03-02T20:00:00Z"))
    assert invalid.status_code == 422  # only a manual placement can be preserved


def test_a_blocking_conflict_is_structured_and_writes_nothing(client, alice, engine) -> None:
    essay = create(client, alice, "tasks", task_payload(name="Essay", required_date=DAY))
    generate(client, alice)
    [placed] = placements_of(client, alice, essay["id"])
    moved = move(client, alice, placed, f"{DAY}T15:00:00Z", f"{DAY}T16:00:00Z")
    create(client, alice, "fixed-blocks", block_payload(label="Class", category="event",
                                                        planned_start=f"{DAY}T15:00:00Z",
                                                        planned_end=f"{DAY}T16:00:00Z"))
    before = (change_count(engine), snapshot(client, alice)["placements"])

    refused = client.post("/planning/generate", json={**RANGE, "generate_end": DAY, "mode": "full"}, headers=alice)

    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "regenerate_required"
    [problem] = error["details"]["problems"] if "details" in error else error["problems"]
    assert (problem["placement_id"], problem["reason"], problem["kept_as"], problem["blocking"]) == (
        moved["id"], "overlaps_fixed_block", "manual", True)
    assert "release_manual_intent" in problem["remedies"]
    assert (change_count(engine), snapshot(client, alice)["placements"]) == before


def test_work_on_another_date_is_reported_and_left_untouched(client, alice) -> None:
    floating = create(client, alice, "tasks", task_payload(name="Floating", preferred_dates=[DAY]))
    generate(client, alice)
    [placed] = placements_of(client, alice, floating["id"])
    moved = move(client, alice, placed, f"{NEXT}T10:00:00Z", f"{NEXT}T11:00:00Z", day=NEXT)
    create(client, alice, "tasks", task_payload(name="Other", required_date=DAY))

    result = generate(client, alice)

    assert result["kept_elsewhere"] == [{"task_id": floating["id"], "placement_id": moved["id"], "date": NEXT}]
    assert result["superseded_placement_ids"] == []
    assert placements_of(client, alice, floating["id"]) == [moved]


def test_cancel_reasons_are_recorded_on_cancel_only(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload())
    placement = create(client, alice, "placements", placement_payload(task["id"]))
    first = create(client, alice, "executions", execution_payload(task["id"], placement["id"]))
    skipped = client.post(f"/executions/{first['id']}/actions/skip", headers=alice,
                          json={"base_version": first["version"], "cancel_reason": "superseded"})
    assert skipped.status_code == 422
    cancelled = client.post(f"/executions/{first['id']}/actions/cancel", headers=alice,
                            json={"base_version": first["version"]}).json()
    assert (cancelled["status"], cancelled["cancel_reason"]) == ("cancelled", "user")

    second = create(client, alice, "executions", execution_payload(task["id"], None))
    system = client.post(f"/executions/{second['id']}/actions/cancel", headers=alice,
                         json={"base_version": second["version"], "cancel_reason": "superseded"}).json()
    assert system["cancel_reason"] == "superseded"
    mislabeled = client.post("/executions", headers=alice, json=execution_payload(task["id"], None,
                                                                                   cancel_reason="user"))
    assert mislabeled.status_code == 422  # only a cancelled execution has a reason


def test_the_server_advertises_manual_placements(client, alice) -> None:
    features = client.get("/sync/capabilities", headers=alice).json()["features"]
    assert "manual_placements" in features
