"""The server's budget records (backend/resources.py BUDGET_*): REST CRUD through the Mutator, content validation
shared with app/budget, relationship rules (project, entry, occurrence date, one live edit per occurrence), the
entry-delete cascade, per-user isolation, and the change feed that returns budget records only on request."""

from __future__ import annotations

from tests.backend.conftest import create, editable

ENTRY = {"kind": "income", "name": "Salary", "amount_minor": 200000, "category": "Work 1",
         "start_date": "2026-01-31", "recurrence": "monthly", "end_date": None, "project_id": None}


def edit_payload(entry_id: str, **fields) -> dict:
    return {"entry_id": entry_id, "occurrence_date": "2026-02-28", "state": "modified", "amount_minor": 250000,
            **fields}


def test_budget_crud_round_trip(client, alice) -> None:
    balance = create(client, alice, "budget-balances", {"amount_minor": -1250, "as_of": "2026-01-31"})
    assert (balance["amount_minor"], balance["version"]) == (-1250, 1)
    entry = create(client, alice, "budget-entries", ENTRY)
    assert {name: entry[name] for name in ENTRY} == ENTRY
    edit = create(client, alice, "budget-overrides", edit_payload(entry["id"], date="2026-03-02"))
    assert (edit["date"], edit["amount_minor"], edit["name"]) == ("2026-03-02", 250000, None)

    updated = client.put(f"/budget-entries/{entry['id']}", json=editable(entry, amount_minor=210000), headers=alice)
    assert updated.status_code == 200 and updated.json()["version"] == 2
    stale = client.put(f"/budget-entries/{entry['id']}", json=editable(entry, amount_minor=1), headers=alice)
    assert stale.status_code == 409

    # Deleting the entry deletes its occurrence edits too (each a logged tombstone).
    deleted = client.delete(f"/budget-entries/{entry['id']}", params={"base_version": 2}, headers=alice)
    assert deleted.status_code == 200 and deleted.json()["deleted_at"] is not None
    gone = client.get(f"/budget-overrides/{edit['id']}", params={"include_deleted": True}, headers=alice).json()
    assert gone["deleted_at"] is not None


def test_content_is_validated_like_the_desktop(client, alice) -> None:
    for bad in ({"category": "Housing"}, {"amount_minor": 0}, {"amount_minor": 12.5}, {"amount_minor": "100"},
                {"recurrence": "yearly"}, {"recurrence": "none", "end_date": "2026-06-01"},
                {"end_date": "2026-01-01"}, {"start_date": "1800-01-01"}, {"user_id": None}):
        response = client.post("/budget-entries", json={**ENTRY, **bad}, headers=alice)
        assert response.status_code == 422, (bad, response.text)
    entry = create(client, alice, "budget-entries", ENTRY)
    for bad in ({"occurrence_date": "2026-02-27"}, {"category": "Housing"},
                {"state": "skipped"}):  # skipped with replacement values
        response = client.post("/budget-overrides", json=edit_payload(entry["id"], **bad), headers=alice)
        assert response.status_code == 422, (bad, response.text)
    one_time = create(client, alice, "budget-entries", {**ENTRY, "recurrence": "none"})
    response = client.post("/budget-overrides", json=edit_payload(one_time["id"], occurrence_date="2026-01-31"),
                           headers=alice)
    assert response.status_code == 422


def test_one_live_edit_per_occurrence_and_fixed_identity(client, alice) -> None:
    entry = create(client, alice, "budget-entries", ENTRY)
    edit = create(client, alice, "budget-overrides", edit_payload(entry["id"]))
    second = client.post("/budget-overrides", json=edit_payload(entry["id"], state="skipped", amount_minor=None),
                         headers=alice)
    assert second.status_code == 409 and second.json()["error"]["current"]["id"] == edit["id"]
    moved = client.put(f"/budget-overrides/{edit['id']}",
                       json=editable(edit, occurrence_date="2026-03-31"), headers=alice)
    assert moved.status_code == 422


def test_references_and_records_stay_within_the_user(client, alice, bob) -> None:
    project = create(client, alice, "projects", {"name": "Thesis"})
    entry = create(client, alice, "budget-entries", {**ENTRY, "project_id": project["id"]})
    assert client.post("/budget-entries", json={**ENTRY, "project_id": project["id"]}, headers=bob).status_code == 422
    assert client.post("/budget-overrides", json=edit_payload(entry["id"]), headers=bob).status_code == 422
    assert client.get(f"/budget-entries/{entry['id']}", headers=bob).status_code == 404
    assert client.get("/budget-entries", headers=bob).json()["items"] == []


def test_the_change_feed_returns_budget_records_only_on_request(client, alice) -> None:
    create(client, alice, "projects", {"name": "Thesis"})
    entry = create(client, alice, "budget-entries", ENTRY)
    old_client = client.get("/changes", params={"include_task_types": True}, headers=alice).json()
    assert [change["entity_type"] for change in old_client["changes"]] == ["project"]
    assert old_client["cursor"] == 1  # an older client's cursor stops before the budget change
    new_client = client.get("/changes", params={"include_task_types": True, "include_budget": True},
                            headers=alice).json()
    assert [change["entity_type"] for change in new_client["changes"]] == ["project", "budget_entry"]
    assert new_client["changes"][1]["record"]["id"] == entry["id"]
    features = client.get("/sync/capabilities", headers=alice).json()["features"]
    assert "budget" in features


def test_future_items_and_a_conversion_that_never_changes(client, alice, bob) -> None:
    idea = create(client, alice, "budget-ideas", {"name": "Raise", "expected_amount_minor": 50000,
                                                  "secured_amount_minor": 10000, "description": None})
    assert idea["secured_amount_minor"] == 10000
    assert client.post("/budget-ideas", json={**{k: idea[k] for k in ("name", "description")},
                                              "expected_amount_minor": 0, "secured_amount_minor": 0},
                       headers=alice).status_code == 422
    wish = create(client, alice, "budget-wishes", {"name": "Desk", "amount_minor": 30000, "category": "Shopping"})
    assert client.post("/budget-wishes", json={"name": "Desk", "amount_minor": 1, "category": "Work 1"},
                       headers=alice).status_code == 422
    expense = create(client, alice, "budget-entries", {**ENTRY, "kind": "expense", "category": "Shopping",
                                                       "recurrence": "none"})
    bobs = create(client, bob, "budget-entries", ENTRY)
    other = client.put(f"/budget-wishes/{wish['id']}", headers=alice,
                       json=editable(wish, converted_entry_id=bobs["id"], converted_on="2026-02-01"))
    assert other.status_code == 422  # another account's expense
    half = client.put(f"/budget-wishes/{wish['id']}", headers=alice, json=editable(wish, converted_on="2026-02-01"))
    assert half.status_code == 422  # the expense and its date go together
    converted = client.put(f"/budget-wishes/{wish['id']}", headers=alice,
                           json=editable(wish, converted_entry_id=expense["id"], converted_on="2026-02-01"))
    assert converted.status_code == 200
    again = client.put(f"/budget-wishes/{wish['id']}", headers=alice,
                       json=editable(converted.json(), converted_entry_id=None, converted_on=None))
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_converted"
