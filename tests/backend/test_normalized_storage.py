"""The normalized storage model (backend/models.py, migrations 0004-0006):
database-enforced ownership and canonical references, exact list and
preference-state semantics, optimistic concurrency and rollback over child
rows, retained history, the bounded placement extension object, no
structured JSON/text blobs, and bounded query counts for large reads."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import JSON, Text, event, func, select
from sqlalchemy.exc import IntegrityError

from backend import models
from backend.database import session_factory
from backend.planning_repository import ServerPlanningRepository
from backend.record_mapping import MAX_OPTIMIZATION_METADATA_BYTES
from tests.backend.conftest import create, editable, execution_payload, placement_payload, task_payload

NOW = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)


def user_id(client, headers) -> uuid.UUID:
    return uuid.UUID(client.get("/me", headers=headers).json()["id"])


def insert(engine, *rows) -> None:
    with session_factory(engine)() as session:
        session.add_all(rows)
        session.commit()


def execution_row(owner: uuid.UUID, **values) -> models.Execution:
    return models.Execution(
        user_id=owner, id=uuid.uuid4(), created_at=NOW, updated_at=NOW, version=1, historical_reference=False,
        task_name="x", category="c", tag="", planned_duration=5, priority=5, status="scheduled", **values)


# -----------------------------------------------------------------------------
# Database-enforced ownership and canonical references
# -----------------------------------------------------------------------------


def test_child_rows_and_execution_links_cannot_cross_users(client, engine, alice, bob) -> None:
    task = create(client, alice, "tasks", task_payload())
    bob_id, task_id = user_id(client, bob), uuid.UUID(task["id"])

    with pytest.raises(IntegrityError):  # Bob's tag row for Alice's task: the composite key includes user_id
        insert(engine, models.TaskTag(user_id=bob_id, task_id=task_id, position=0, tag="stolen"))
    with pytest.raises(IntegrityError):  # an execution of Bob enforced-linked to Alice's task
        insert(engine, execution_row(bob_id, task_id=task_id, linked_task_id=task_id))


def test_a_linked_placement_must_belong_to_the_linked_task(client, engine, alice) -> None:
    first, second = create(client, alice, "tasks", task_payload()), create(client, alice, "tasks", task_payload())
    placement = create(client, alice, "placements", placement_payload(second["id"]))
    alice_id = user_id(client, alice)
    wrong = execution_row(alice_id, task_id=uuid.UUID(first["id"]), scheduled_task_id=uuid.UUID(placement["id"]),
                          linked_task_id=uuid.UUID(first["id"]), linked_placement_id=uuid.UUID(placement["id"]))
    with pytest.raises(IntegrityError):
        insert(engine, wrong)
    # Through the API it is an invalid reference (422) -- the database is the last line, not the only one.
    response = client.post("/executions", json=execution_payload(first["id"], placement["id"]), headers=alice)
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_reference"


def test_a_non_historical_execution_needs_its_enforced_links(client, engine, alice) -> None:
    unknown = uuid.uuid4()
    with pytest.raises(IntegrityError):  # the database refuses a canonical execution naming an unresolved task
        insert(engine, execution_row(user_id(client, alice), task_id=unknown))
    # A historical upload of the same ids is accepted as history and nothing is invented for it.
    record = create(client, alice, "executions", execution_payload(str(unknown), historical_reference=True))
    with session_factory(engine)() as session:
        row = session.get(models.Execution, (user_id(client, alice), uuid.UUID(record["id"])))
        assert (row.task_id, row.linked_task_id) == (unknown, None)
        assert session.get(models.Task, (row.user_id, unknown)) is None


def test_soft_deleting_tasks_and_placements_keeps_linked_history(client, engine, alice) -> None:
    task = create(client, alice, "tasks", task_payload(tags=["keep"]))
    placement = create(client, alice, "placements", placement_payload(task["id"]))
    execution = create(client, alice, "executions", execution_payload(task["id"], placement["id"]))
    deleted = client.delete(f"/tasks/{task['id']}", params={"base_version": 1}, headers=alice).json()

    assert deleted["deleted_at"] is not None and deleted["tags"] == ["keep"]
    assert client.get(f"/executions/{execution['id']}", headers=alice).json() == execution
    with session_factory(engine)() as session:
        row = session.get(models.Execution, (user_id(client, alice), uuid.UUID(execution["id"])))
        assert (str(row.linked_task_id), str(row.linked_placement_id)) == (task["id"], placement["id"])
    records = [c["record"] for c in client.get("/changes", headers=alice).json()["changes"]]
    assert records[0] == task and records[-1] == deleted  # the history of the task, before and after


# -----------------------------------------------------------------------------
# Exact content round trips
# -----------------------------------------------------------------------------


def test_task_lists_keep_their_exact_order_and_repeats(client, alice) -> None:
    dependency, other = create(client, alice, "tasks", task_payload()), create(client, alice, "tasks", task_payload())
    task = create(client, alice, "tasks", task_payload(
        tags=["b", "a", "b", ""], preferred_dates=["2026-03-05", "2026-03-01", "2026-03-05"],
        dependency_ids=[other["id"], dependency["id"]], deadline="2026-03-05T17:00:00+05:30",
        recurrence={"frequency": "weekly", "interval": 3, "weekdays": [4, 0, 4], "count": 2},
    ))
    assert task["tags"] == ["b", "a", "b", ""]
    assert task["preferred_dates"] == ["2026-03-05", "2026-03-01", "2026-03-05"]
    assert task["dependency_ids"] == [other["id"], dependency["id"]]
    assert task["deadline"] == "2026-03-05T17:00:00+05:30"
    assert task["recurrence"] == {"frequency": "weekly", "interval": 3, "weekdays": [0, 4], "day_of_month": None,
                                  "end_date": None, "count": 2, "start_date": None,
                                  "timezone": None}  # the canonical model's set of weekdays; not configured
    assert client.get(f"/tasks/{task['id']}", headers=alice).json() == task

    changed = client.put(f"/tasks/{task['id']}", headers=alice, json=editable(
        task, tags=["a"], preferred_dates=[], dependency_ids=[dependency["id"]], recurrence=None)).json()
    assert (changed["tags"], changed["preferred_dates"], changed["dependency_ids"], changed["recurrence"]) == (
        ["a"], [], [dependency["id"]], None)
    assert client.get(f"/tasks/{task['id']}", headers=alice).json() == changed


def test_preference_layers_keep_absent_value_and_clear_states(client, alice) -> None:
    overrides = {
        "day_window": {"start_minute": 300, "end_minute": 1440, "end_day_offset": 0},
        "category_multipliers": {"study": 2.5, "work": None, "zero": 0.0},
        "category_preferred_windows": {"study": {"start_minute": 480, "end_minute": 600}, "rest": None},
        "optimizer_mode": "adhd_friendly",
        "reward": {"weight_importance": 7.0, "same_tag_window_minutes": 0,
                   "tag_relations": {"math": ["physics", "math", "physics"], "solo": []}},
    }
    user = create(client, alice, "preferences", {"scope": "user", "overrides": overrides})
    empty = create(client, alice, "preferences", {"scope": "date", "date": "2026-03-02",
                                                  "overrides": {"reward": {"tag_relations": {}}}})
    absent = create(client, alice, "preferences", {"scope": "date", "date": "2026-03-03", "overrides": {}})

    stored = user["overrides"]
    assert stored["day_window"] == {"start_minute": 300, "end_minute": 0, "end_day_offset": 1}  # the model's 24:00 form
    assert stored["category_multipliers"] == {"study": 2.5, "work": None, "zero": 0.0}
    assert stored["category_preferred_windows"] == {"rest": None, "study": {"start_minute": 480, "end_minute": 600}}
    assert stored["reward"]["tag_relations"] == {"math": ["physics", "math", "physics"], "solo": []}
    assert stored["reward"]["weight_importance"] == 7.0 and stored["reward"]["weight_time_bonus"] is None
    assert empty["overrides"]["reward"]["tag_relations"] == {}  # present and empty ...
    assert absent["overrides"]["reward"]["tag_relations"] is None  # ... is not absent
    for record in (user, empty, absent):
        assert client.get(f"/preferences/{record['id']}", headers=alice).json() == record

    # Replacing the layer removes keys that are no longer present (a clear is not the same as an absent key).
    changed = client.put(f"/preferences/{user['id']}", headers=alice, json=editable(
        user, overrides={"category_multipliers": {"work": 1.5}})).json()
    assert changed["overrides"]["category_multipliers"] == {"work": 1.5}
    assert changed["overrides"]["category_preferred_windows"] == {} and changed["overrides"]["reward"]["tag_relations"] is None
    assert client.get(f"/preferences/{user['id']}", headers=alice).json() == changed


def test_placement_metadata_is_a_bounded_extension_object(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload())
    small = create(client, alice, "placements", placement_payload(task["id"], optimization_metadata={"n": [1, None]}))
    assert small["optimization_metadata"] == {"n": [1, None]}
    too_big = {"blob": "x" * MAX_OPTIMIZATION_METADATA_BYTES}
    schedule = {"placements": [{"task_id": task["id"]}]}
    for metadata in (too_big, schedule):
        response = client.post("/placements", json=placement_payload(task["id"], optimization_metadata=metadata),
                               headers=alice)
        assert response.status_code == 422, response.text


# -----------------------------------------------------------------------------
# Concurrency, rollback and history
# -----------------------------------------------------------------------------


def test_a_stale_update_changes_no_child_rows(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload(tags=["one"]))
    current = client.put(f"/tasks/{task['id']}", json=editable(task, tags=["two"]), headers=alice).json()
    stale = client.put(f"/tasks/{task['id']}", json=editable(task, tags=["three", "four"]), headers=alice)
    assert stale.status_code == 409 and stale.json()["error"]["current"] == current
    assert client.get(f"/tasks/{task['id']}", headers=alice).json() == current


def test_a_failed_group_rolls_back_its_children_and_snapshots(client, engine, alice) -> None:
    task = create(client, alice, "tasks", task_payload(tags=["before"]))

    def counts() -> tuple[int, int]:
        with session_factory(engine)() as session:
            return (session.scalar(select(func.count()).select_from(models.RecordRevision)),
                    session.scalar(select(func.count()).select_from(models.TaskTag)))

    before, group = counts(), str(uuid.uuid4())
    results = client.post("/sync/push", headers=alice, json={"operations": [
        {"op_id": str(uuid.uuid4()), "entity_type": "task", "entity_id": task["id"], "kind": "update",
         "base_version": 1, "payload": task_payload(tags=["after", "more"]), "group": group},
        {"op_id": str(uuid.uuid4()), "entity_type": "task", "entity_id": str(uuid.uuid4()), "kind": "update",
         "base_version": 1, "payload": task_payload(), "group": group},
    ]}).json()["results"]
    assert [r["status"] for r in results] == ["rejected", "rejected"]
    assert client.get(f"/tasks/{task['id']}", headers=alice).json() == task
    assert counts() == before  # neither the updated tags nor the rolled-back change-log snapshot remain


def test_an_applied_sync_outcome_shares_its_change_log_snapshot(client, engine, alice) -> None:
    op_id = str(uuid.uuid4())
    push = {"operations": [{"op_id": op_id, "entity_type": "task", "entity_id": str(uuid.uuid4()), "kind": "create",
                            "payload": task_payload(tags=["a", "a"])}]}
    [result] = client.post("/sync/push", json=push, headers=alice).json()["results"]
    with session_factory(engine)() as session:
        outcome = session.get(models.SyncOperation, (user_id(client, alice), uuid.UUID(op_id)))
        entry = session.scalars(select(models.ChangeLogEntry)).one()
        assert outcome.record_revision_id == entry.revision_id  # one immutable snapshot, referenced twice
        assert session.scalar(select(func.count()).select_from(models.RecordRevision)) == 1
    assert client.post("/sync/push", json=push, headers=alice).json()["results"] == [result]


def test_the_change_log_keeps_every_historical_snapshot(client, alice) -> None:
    task = create(client, alice, "tasks", task_payload(tags=["v1"]))
    second = client.put(f"/tasks/{task['id']}", json=editable(task, tags=["v2"], name="Second"), headers=alice).json()
    third = client.put(f"/tasks/{task['id']}", json=editable(second, tags=[]), headers=alice).json()
    assert [c["record"] for c in client.get("/changes", headers=alice).json()["changes"]] == [task, second, third]


# -----------------------------------------------------------------------------
# Schema: no structured blobs
# -----------------------------------------------------------------------------

#: The only JSON: the documented, bounded placement extension object (live, and in its snapshots).
ALLOWED_JSON = {("placements", "optimization_metadata"), ("placement_revisions", "optimization_metadata")}
#: Unbounded text is only ever one human-written or atomic value -- never a document.
ALLOWED_TEXT = {
    ("projects", "description"), ("project_revisions", "description"), ("executions", "note"),
    ("execution_revisions", "note"), ("task_tags", "tag"), ("task_revision_tags", "tag"),
    ("placement_task_tags", "tag"), ("placement_revision_task_tags", "tag"),
    ("preference_category_multipliers", "category"), ("preference_category_windows", "category"),
    ("preference_tag_relations", "tag"), ("preference_related_tags", "tag"),
    ("preference_related_tags", "related_tag"), ("preference_revision_category_multipliers", "category"),
    ("preference_revision_category_windows", "category"), ("preference_revision_tag_relations", "tag"),
    ("preference_revision_related_tags", "tag"), ("preference_revision_related_tags", "related_tag"),
    ("sync_operations", "error_message"), ("sync_operation_problems", "message"),
    ("sync_operation_problem_locations", "part"),
}


def test_the_schema_has_no_structured_json_or_text_blobs() -> None:
    json_columns, text_columns = set(), set()
    for table in models.Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSON) or any(isinstance(variant, JSON)
                                                    for variant in getattr(column.type, "_variant_mapping", {}).values()):
                json_columns.add((table.name, column.name))
            elif isinstance(column.type, Text):
                text_columns.add((table.name, column.name))
    assert json_columns == ALLOWED_JSON
    assert text_columns == ALLOWED_TEXT
    for table in models.Base.metadata.tables.values():
        assert not {"payload", "result", "overrides", "document", "schedule", "tasks"} & set(table.columns.keys())


# -----------------------------------------------------------------------------
# A large round trip with bounded queries
# -----------------------------------------------------------------------------


@contextmanager
def counted(engine):
    statements: list[str] = []

    def before(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


def test_a_thousand_tasks_round_trip_with_bounded_relationship_queries(client, engine, alice) -> None:
    anchor = create(client, alice, "tasks", task_payload(name="Anchor"))
    expected = {}
    for batch in range(5):
        operations = []
        for index in range(batch * 200, batch * 200 + 200):
            task_id = str(uuid.uuid4())
            payload = task_payload(
                name=f"Task {index}", tags=[f"t{index % 7}", "shared", f"t{index % 7}"][: 1 + index % 3],
                preferred_dates=["2026-03-02", "2026-03-01"][: index % 3], dependency_ids=[anchor["id"]] * (index % 2),
                recurrence={"frequency": "weekly", "interval": 1, "weekdays": [index % 7]} if index % 4 == 0 else None,
            )
            operations.append({"op_id": str(uuid.uuid4()), "entity_type": "task", "entity_id": task_id,
                               "kind": "create", "payload": payload})
            expected[task_id] = payload
        results = client.post("/sync/push", json={"operations": operations}, headers=alice).json()["results"]
        assert {r["status"] for r in results} == {"applied"}

    def page(path: str, limit: int, **params) -> tuple[dict, int]:
        with counted(engine) as statements:
            body = client.get(path, params={"limit": limit, **params}, headers=alice).json()
        return body, len(statements)

    # The tags/dates/dependencies/weekdays load per page, not per task: a page of 10 and of 400 cost the same.
    # (SQLAlchemy loads children in IN-lists of up to 500 keys; a page reads limit + 1 rows, so a page of 500 adds
    # at most one more query per child kind.)
    _, small_queries = page("/tasks", 10)
    _, big_queries = page("/tasks", 400)
    first, full_queries = page("/tasks", 500)
    assert big_queries == small_queries <= 8 and full_queries <= small_queries + 4
    second, _ = page("/tasks", 500, cursor=first["next_cursor"])
    third, _ = page("/tasks", 500, cursor=second["next_cursor"])
    stored = {item["id"]: item for item in first["items"] + second["items"] + third["items"]}
    assert len(stored) == 1001 and third["next_cursor"] is None
    for task_id, payload in expected.items():
        record = stored[task_id]
        assert (record["name"], record["tags"], record["preferred_dates"], record["dependency_ids"]) == (
            payload["name"], payload["tags"], payload["preferred_dates"], payload["dependency_ids"])
        assert (record["recurrence"] or {}).get("weekdays") == (payload["recurrence"] or {}).get("weekdays")

    _, feed_small = page("/changes", 10)
    _, feed_big = page("/changes", 400)
    feed, feed_full = page("/changes", 500)
    # Snapshots load per page as well: revisions, their typed rows and four child kinds (one more chunk each at 500).
    assert feed_big == feed_small <= 10 and feed_full <= feed_small + 6
    assert len(feed["changes"]) == 500
    assert [c["record"] for c in feed["changes"][1:]] == [stored[c["entity_id"]] for c in feed["changes"][1:]]

    owner = user_id(client, alice)
    with session_factory(engine)() as session, counted(engine) as statements:
        tasks = ServerPlanningRepository(session, owner, lambda: NOW).list_tasks()
    # One query for the tasks plus one per child kind per 500 tasks (SQLAlchemy's selectin IN-list chunk), plus
    # PostgreSQL's connection pre-ping.
    assert len(tasks) == 1001 and len(statements) <= 2 + 4 * 3
    assert sorted(task.tags for task in tasks if str(task.id) in expected) == sorted(
        payload["tags"] for payload in expected.values())
