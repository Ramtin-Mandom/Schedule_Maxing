"""Date-level views on the server: GET /days/summary returns every date of a range in one call (the
shared classification and aggregates), POST /days/{date}/outcome completes or uncompletes all of a
date's scheduled tasks in one transaction, both strictly for the authenticated user; and task
points travel through the REST schema with their default and bounds."""

from __future__ import annotations

from tests.backend.conftest import create, placement_payload, task_payload


def plan(client, headers, name: str, hour: int, day: str = "2026-03-02", points: int = 2) -> tuple[dict, dict]:
    task = create(client, headers, "tasks", task_payload(name=name, points=points))
    start, end = f"{day}T{hour:02d}:00:00Z", f"{day}T{hour + 1:02d}:00:00Z"
    return task, create(client, headers, "placements", placement_payload(
        task["id"], planned_date=day, planned_start=start, planned_end=end))


def summary(client, headers, start: str, end: str) -> list[dict]:
    response = client.get("/days/summary", params={"start_date": start, "end_date": end}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["days"]


def test_one_call_summarizes_a_range_with_the_shared_classification(client, alice) -> None:
    plan(client, alice, "A", 9)
    _, b = plan(client, alice, "B", 11, points=5)
    create(client, alice, "tasks", task_payload(name="Never placed", points=40))  # not scheduled: not counted
    client.post(f"/placements/{b['id']}/outcome", json={"outcome": "completed"}, headers=alice)

    days = summary(client, alice, "2026-03-01", "2026-03-31")
    assert len(days) == 31 and days[0]["date"] == "2026-03-01"
    march_2 = days[1]
    assert (march_2["scheduled_count"], march_2["completed_count"], march_2["pending_count"]) == (2, 1, 1)
    assert (march_2["scheduled_minutes"], march_2["completed_minutes"]) == (120, 60)
    assert (march_2["points_scheduled"], march_2["points_completed"]) == (7, 5)
    assert march_2["status_class"] == "mostly_pending"  # 50% still pending
    assert days[0]["status_class"] == "no_tasks" and days[0]["scheduled_count"] == 0


def test_bulk_outcomes_change_every_scheduled_task_of_the_date_atomically(client, alice) -> None:
    for index, name in enumerate(("A", "B", "C")):
        plan(client, alice, name, 9 + 2 * index)
    plan(client, alice, "Other day", 9, day="2026-03-03")

    done = client.post("/days/2026-03-02/outcome", json={"outcome": "completed"}, headers=alice)
    assert done.status_code == 200, done.text
    body = done.json()
    assert len(body["changed"]) == 3 and body["unchanged"] == [] and body["skipped"] == []
    assert body["summary"]["completed_count"] == 3 and body["summary"]["status_class"] == "mostly_completed_strong"
    assert summary(client, alice, "2026-03-03", "2026-03-03")[0]["completed_count"] == 0  # other dates untouched

    again = client.post("/days/2026-03-02/outcome", json={"outcome": "completed"}, headers=alice).json()
    assert again["changed"] == [] and len(again["unchanged"]) == 3  # idempotent
    missed = client.post("/days/2026-03-02/outcome", json={"outcome": "uncompleted"}, headers=alice).json()
    assert missed["summary"]["uncompleted_count"] == 3
    assert len(client.get("/executions", headers=alice).json()["items"]) == 3  # one per placement

    empty = client.post("/days/2026-04-01/outcome", json={"outcome": "completed"}, headers=alice).json()
    assert empty["changed"] == [] and empty["summary"]["status_class"] == "no_tasks"


def test_everything_is_scoped_to_the_authenticated_user(client, alice, bob) -> None:
    plan(client, alice, "Alice's", 9)
    assert summary(client, bob, "2026-03-02", "2026-03-02")[0]["scheduled_count"] == 0
    bulk = client.post("/days/2026-03-02/outcome", json={"outcome": "completed"}, headers=bob).json()
    assert bulk["changed"] == []  # bob's day is empty; alice's placement is not his to change
    assert summary(client, alice, "2026-03-02", "2026-03-02")[0]["completed_count"] == 0
    assert client.get("/days/summary", params={"start_date": "2026-03-02", "end_date": "2026-03-02"}).status_code == 401
    assert client.post("/days/2026-03-02/outcome", json={"outcome": "completed"}).status_code == 401


def test_invalid_ranges_and_outcomes_are_refused(client, alice) -> None:
    backwards = client.get("/days/summary", params={"start_date": "2026-03-05", "end_date": "2026-03-01"},
                           headers=alice)
    assert backwards.status_code == 422
    too_long = client.get("/days/summary", params={"start_date": "2026-01-01", "end_date": "2027-06-01"},
                          headers=alice)
    assert too_long.status_code == 422
    assert client.post("/days/2026-03-02/outcome", json={"outcome": "missed"}, headers=alice).status_code == 422


def test_points_have_a_default_bounds_and_are_not_a_score(client, alice) -> None:
    default = create(client, alice, "tasks", task_payload())
    assert default["points"] == 1
    assert create(client, alice, "tasks", task_payload(points=0))["points"] == 0
    for bad in (-1, 1001, 2.5):
        assert client.post("/tasks", json=task_payload(points=bad), headers=alice).status_code == 422
    placement = create(client, alice, "placements", placement_payload(default["id"]))
    assert placement["score"] == 3.5 and "points" not in placement  # the placement keeps its optimizer score apart
