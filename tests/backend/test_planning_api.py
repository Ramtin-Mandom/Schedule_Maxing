"""The hosted scheduling API (backend/planning_api.py) end to end, through real
endpoints: preview -> generate -> reload/restart -> an identical generate is a
no-op; incremental additions keep committed placements; explicit
regeneration; refused incompatible kept placements; concurrent input changes
roll back; both engines; owner isolation; reset; and the canonical CSV."""

from __future__ import annotations

import csv
import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.planning import workflow
from backend import models
from backend.app import create_app
from backend.database import create_backend_engine, session_factory
from backend.migrate import upgrade
from backend.mutations import mutation
from backend.resources import TASKS, TaskCreate
from tests.backend.conftest import account, block_payload, create, editable, preference_payload, task_payload

DAY = "2026-03-02"
RANGE = {"start_date": DAY, "end_date": "2026-03-03", "timezone": "UTC"}


def changes(engine) -> int:
    with session_factory(engine)() as session:
        return session.scalar(select(func.count()).select_from(models.ChangeLogEntry))


def seed(client, headers) -> dict:
    sleep = create(client, headers, "fixed-blocks", block_payload())  # 00:00-07:00 on DAY
    essay = create(client, headers, "tasks", task_payload(name="Essay", required=True, required_date=DAY,
                                                          estimated_duration_minutes=90, priority=8))
    gym = create(client, headers, "tasks", task_payload(name="Gym", category="health", preferred_dates=[DAY]))
    return {"sleep": sleep, "essay": essay, "gym": gym}


def generate(client, headers, **extra) -> dict:
    response = client.post("/planning/generate", json={**RANGE, "generate_end": DAY, **extra}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def day_placements(client, headers) -> list[dict]:
    snapshot = client.get("/planning/snapshot", params={**RANGE}, headers=headers).json()
    return sorted((p for p in snapshot["placements"] if p["planned_date"] == DAY), key=lambda p: p["planned_start"])


def test_preview_generate_reload_and_an_identical_generate_is_a_no_op(client, alice, engine, settings, clock) -> None:
    records = seed(client, alice)
    preview = client.post("/planning/allocation/preview", json=RANGE, headers=alice).json()
    assert {(a["task_id"], a["date"]) for a in preview["assignments"]} >= {(records["essay"]["id"], DAY)}
    assert day_placements(client, alice) == []  # a preview never generates or saves anything

    result = generate(client, alice, expected_fingerprint=preview["fingerprint"])
    assert result["status"] == "generated" and result["fingerprint"] == preview["fingerprint"]
    placed = day_placements(client, alice)
    assert {p["task_id"] for p in placed} == {records["essay"]["id"], records["gym"]["id"]}
    assert all(p["planned_start"] >= f"{DAY}T07:00:00Z" for p in placed)  # around the sleep block
    snapshot = client.get("/planning/snapshot", params=RANGE, headers=alice).json()
    assert snapshot["days"][0]["status"] == "current" and snapshot["days"][0]["unscheduled_count"] == 0

    before = changes(engine)
    again = generate(client, alice)
    assert again["status"] == "already_current" and again["days"][0]["unscheduled"] is None
    assert day_placements(client, alice) == placed and changes(engine) == before

    # A restarted server (a new app on the same database) derives the same state from what is stored.
    with TestClient(create_app(settings, engine=engine, clock=clock)) as restarted:
        assert generate(restarted, alice)["status"] == "already_current"
        assert day_placements(restarted, alice) == placed
    assert changes(engine) == before


def test_a_stale_preview_fingerprint_is_refused(client, alice) -> None:
    seed(client, alice)
    preview = client.post("/planning/allocation/preview", json=RANGE, headers=alice).json()
    create(client, alice, "tasks", task_payload(name="New", preferred_dates=[DAY]))
    response = client.post("/planning/generate", json={**RANGE, "expected_fingerprint": preview["fingerprint"]},
                           headers=alice)
    assert response.status_code == 409 and response.json()["error"]["code"] == "inputs_changed"
    assert day_placements(client, alice) == []


def test_incremental_generation_adds_work_without_moving_committed_placements(client, alice, engine) -> None:
    seed(client, alice)
    generate(client, alice)
    committed = day_placements(client, alice)

    urgent = create(client, alice, "tasks", task_payload(name="Urgent", priority=10, preferred_dates=[DAY],
                                                         estimated_duration_minutes=45))
    result = generate(client, alice, mode="incremental")

    assert result["status"] == "generated"
    after = {p["id"]: p for p in day_placements(client, alice)}
    for placement in committed:  # same ids, intervals and versions
        assert after[placement["id"]] == placement
    new = [p for p in after.values() if p["id"] not in {c["id"] for c in committed}]
    assert [p["task_id"] for p in new] == [urgent["id"]]
    assert set(result["days"][0]["kept_placement_ids"]) == {p["id"] for p in committed}

    # With nothing new, an incremental run neither clears nor moves anything.
    assert generate(client, alice, mode="incremental")["status"] == "already_current"
    assert {p["id"] for p in day_placements(client, alice)} == set(after)


def test_incompatible_kept_placements_require_an_explicit_regeneration(client, alice, engine) -> None:
    records = seed(client, alice)
    generate(client, alice)
    committed = day_placements(client, alice)
    first = committed[0]
    create(client, alice, "fixed-blocks", block_payload(label="Meeting", planned_start=first["planned_start"],
                                                        planned_end=first["planned_end"]))
    create(client, alice, "tasks", task_payload(name="New", preferred_dates=[DAY], estimated_duration_minutes=15))
    before = changes(engine)

    refused = client.post("/planning/generate", json={**RANGE, "generate_end": DAY, "mode": "incremental"}, headers=alice)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "regenerate_required"
    assert [(p["placement_id"], p["reason"]) for p in error["problems"]] == [(first["id"], "overlaps_fixed_block")]
    assert changes(engine) == before and day_placements(client, alice) == committed

    regenerated = generate(client, alice, mode="full")
    assert regenerated["status"] == "generated"
    moved = {p["task_id"]: p for p in day_placements(client, alice)}
    assert moved[first["task_id"]]["planned_start"] != first["planned_start"]
    assert records["essay"]["id"] in moved


def test_explicit_regeneration_never_moves_started_work(client, alice) -> None:
    records = seed(client, alice)
    generate(client, alice)
    essay = next(p for p in day_placements(client, alice) if p["task_id"] == records["essay"]["id"])
    execution = create(client, alice, "executions", {
        "task_id": essay["task_id"], "scheduled_task_id": essay["id"], "task_name": "Essay", "category": "study",
        "tag": "", "planned_duration": 90, "priority": 8})
    started = client.post(f"/executions/{execution['id']}/actions/start",
                          json={"base_version": 1, "at": "2026-03-02T11:30:00Z"}, headers=alice)
    assert started.status_code == 200, started.text
    create(client, alice, "tasks", task_payload(name="Bigger", priority=10, preferred_dates=[DAY]))

    generate(client, alice, mode="full")
    kept = next(p for p in day_placements(client, alice) if p["task_id"] == records["essay"]["id"])
    assert kept == essay  # same id, interval and version: the started work stayed where it was


def test_a_concurrent_input_change_rolls_the_generation_back(tmp_path, settings, clock, monkeypatch) -> None:
    engine = create_backend_engine(f"sqlite:///{(tmp_path / 'server.db').as_posix()}")
    upgrade(engine)
    with TestClient(create_app(settings, engine=engine, clock=clock)) as client:
        headers = account(client, "racer@example.com")
        user_id = uuid.UUID(client.get("/me", headers=headers).json()["id"])
        seed(client, headers)
        generate(client, headers)
        committed = day_placements(client, headers)
        create(client, headers, "tasks", task_payload(name="Makes it stale", preferred_dates=[DAY]))

        real = workflow.generate_selected_day

        def racing(*args, **kwargs):
            with session_factory(engine)() as other, mutation(other, user_id, clock) as mutator:
                mutator.create(TASKS, TaskCreate(name="Sneaked in", category="x", estimated_duration_minutes=5,
                                                 priority=1, preferred_dates=[DAY]))
            return real(*args, **kwargs)

        monkeypatch.setattr(workflow, "generate_selected_day", racing)
        before = changes(engine)
        response = client.post("/planning/generate", json={**RANGE, "generate_end": DAY}, headers=headers)

        assert response.status_code == 409 and response.json()["error"]["code"] == "inputs_changed"
        assert day_placements(client, headers) == committed  # the old schedule stands
        assert changes(engine) == before + 1  # only the concurrent task was committed
    engine.dispose()


@pytest.mark.parametrize("mode", ["precise_greedy", "adhd_friendly"])
def test_both_engines(client, alice, mode) -> None:
    seed(client, alice)
    create(client, alice, "preferences", preference_payload(overrides={"optimizer_mode": mode}))
    result = generate(client, alice)
    assert result["days"][0]["engine_mode"] == mode
    if mode == "adhd_friendly":
        for placement in result["days"][0]["placements"]:
            assert placement["planned_start"][14:16] in ("00", "15", "30", "45")


def test_owners_are_isolated(client, alice, bob) -> None:
    seed(client, alice)
    generate(client, alice)
    bobs = client.get("/planning/snapshot", params=RANGE, headers=bob).json()
    assert bobs["tasks"] == bobs["placements"] == bobs["fixed_blocks"] == []
    assert generate(client, bob)["days"][0]["placements"] == []
    assert len(day_placements(client, alice)) == 2
    reset = client.post("/planning/reset/preview", json={"start_date": DAY, "end_date": DAY}, headers=bob).json()
    assert sum(reset["counts"].values()) == 1  # bob's own empty schedule record only


def test_reset_preview_and_confirmed_reset(client, alice, engine) -> None:
    records = seed(client, alice)
    backlog = create(client, alice, "tasks", task_payload(name="Backlog"))
    generate(client, alice)
    preview = client.post("/planning/reset/preview", json={"start_date": DAY, "end_date": DAY}, headers=alice).json()
    assert set(preview["task_ids"]) == {records["essay"]["id"], records["gym"]["id"]}
    assert preview["fixed_block_ids"] == [records["sleep"]["id"]] and not preview["blocked"]

    stale = client.post("/planning/reset", json={"start_date": DAY, "end_date": DAY, "confirmation": "0" * 64},
                        headers=alice)
    assert stale.status_code == 409
    done = client.post("/planning/reset", json={"start_date": DAY, "end_date": DAY, "confirmation": preview["token"]},
                       headers=alice)
    assert done.status_code == 200 and done.json()["deleted"] == preview["counts"]
    remaining = client.get("/tasks", headers=alice).json()["items"]
    assert [t["id"] for t in remaining] == [backlog["id"]]
    assert day_placements(client, alice) == []


def test_csv_round_trip_keeps_identities_and_a_bad_file_changes_nothing(client, alice, bob, engine) -> None:
    seed(client, alice)
    generate(client, alice)
    exported = client.get("/planning/csv/export", headers=alice)
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/csv")
    text = exported.text

    preview = client.post("/planning/csv/preview", content=text.encode(), headers={**alice, "Content-Type": "text/csv"})
    assert preview.status_code == 200 and preview.json()["applied"] is False
    assert sum(preview.json()["created"].values()) == 0 and preview.json()["unchanged"]["task"] == 2

    # Another account cannot import alice's records, and a file of ownerless records is not claimed by importing it.
    foreign = client.post("/planning/csv/import", content=text.encode(), headers={**bob, "Content-Type": "text/csv"})
    assert foreign.status_code == 422 and foreign.json()["error"]["code"] == "out_of_scope"

    # A broken reference anywhere in the file refuses all of it: neither new row is created.
    rows = list(csv.DictReader(io.StringIO(text)))
    task_row = next(row for row in rows if row["record_type"] == "task")
    fine = {**task_row, "id": str(uuid.uuid4()), "name": "Fine"}
    dangling = {**task_row, "id": str(uuid.uuid4()), "name": "Dangling", "dependency_ids": json.dumps([str(uuid.uuid4())])}
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows([*rows, fine, dangling])
    before = changes(engine)
    response = client.post("/planning/csv/import", content=out.getvalue().encode(),
                           headers={**alice, "Content-Type": "text/csv"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_reference"
    assert changes(engine) == before
    assert {t["name"] for t in client.get("/tasks", headers=alice).json()["items"]} == {"Essay", "Gym"}


def test_csv_imports_new_records_with_their_ids(client, alice, bob) -> None:
    records = seed(client, alice)
    text = client.get("/planning/csv/export", headers=alice).text
    alice_id = client.get("/me", headers=alice).json()["id"]
    bob_id = client.get("/me", headers=bob).json()["id"]
    as_bob = text.replace(alice_id, bob_id)
    imported = client.post("/planning/csv/import", content=as_bob.encode(), headers={**bob, "Content-Type": "text/csv"})
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"]["task"] == 2
    assert client.get(f"/tasks/{records['essay']['id']}", headers=bob).json()["name"] == "Essay"
    # The server owns versions: the imported records start at version 1 on bob's account.
    assert client.get(f"/tasks/{records['essay']['id']}", headers=bob).json()["version"] == 1


def test_preferences_view_lists_layers_and_inheritance(client, alice) -> None:
    create(client, alice, "preferences", preference_payload(overrides={"optimizer_mode": "adhd_friendly"}))
    create(client, alice, "preferences", preference_payload(
        scope="date", date=DAY, overrides={"day_window": {"start_minute": 480, "end_minute": 1200}}))
    view = client.get("/planning/preferences", params=RANGE, headers=alice).json()
    first, second = view["days"]
    assert first["effective"]["day_window"]["start_minute"] == 480 and first["inherited"]["day_window"]["start_minute"] == 0
    assert first["effective"]["optimizer_mode"] == "adhd_friendly" and second["date_layer_id"] is None
    assert view["user_layer"]["overrides"]["optimizer_mode"] == "adhd_friendly"
    assert {engine["mode"] for engine in view["engines"]} == {
        "precise_greedy", "adhd_friendly", "early_finish", "night_owl", "catch_up"}


def test_capabilities_and_bounded_ranges(client, alice) -> None:
    capabilities = client.get("/planning/capabilities").json()
    assert capabilities["profile"] == "hosted" and capabilities["reports_device_pending_changes"] is False
    too_long = client.get("/planning/snapshot", params={**RANGE, "end_date": "2026-06-01"}, headers=alice)
    assert too_long.status_code == 422
    bad_zone = client.get("/planning/snapshot", params={**RANGE, "timezone": "Mars/Base"}, headers=alice)
    assert bad_zone.status_code == 422
    assert client.get("/planning/snapshot", params=RANGE).status_code == 401


def test_fixed_block_edits_through_rest_mark_the_day_stale(client, alice) -> None:
    records = seed(client, alice)
    generate(client, alice)
    client.put(f"/fixed-blocks/{records['sleep']['id']}", headers=alice,
               json=editable(records["sleep"], planned_end=f"{DAY}T08:00:00Z"))
    day = client.get("/planning/snapshot", params=RANGE, headers=alice).json()["days"][0]
    assert day["status"] == "stale" and day["stale_reason"] == "inputs_changed"


def test_openapi_publishes_typed_planning_schemas(client) -> None:
    schema = client.get("/openapi.json").json()
    for path in ("/planning/snapshot", "/planning/generate", "/planning/allocation/preview", "/planning/reset",
                 "/planning/csv/import", "/auth/browser/login"):
        assert path in schema["paths"], path
    generate_response = schema["paths"]["/planning/generate"]["post"]["responses"]["200"]["content"]["application/json"]
    assert generate_response["schema"]["$ref"].endswith("/GenerateOut")
    assert {"GenerateIn", "GenerateOut", "SnapshotOut", "DayStateOut", "AllocationPreviewOut"} <= set(
        schema["components"]["schemas"])


BACKEND_ONLY_SCRIPT = """
import sys
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.database import create_backend_engine
from backend.migrate import upgrade
from backend.settings import BackendSettings

engine = create_backend_engine("sqlite://")
upgrade(engine)
app = create_app(BackendSettings(database_url="sqlite://", jwt_secret="s" * 40), engine=engine)
with TestClient(app) as client:
    client.post("/auth/register", json={"email": "a@example.com", "password": "long enough pw"})
    token = client.post("/auth/login", json={"email": "a@example.com", "password": "long enough pw"}).json()
    headers = {"Authorization": "Bearer " + token["access_token"]}
    client.post("/tasks", headers=headers, json={"name": "T", "category": "c", "estimated_duration_minutes": 30,
                                                 "priority": 5, "preferred_dates": ["2026-03-02"]})
    result = client.post("/planning/generate", headers=headers,
                         json={"start_date": "2026-03-02", "end_date": "2026-03-02", "timezone": "UTC"}).json()
    assert len(result["days"][0]["placements"]) == 1, result
print(sorted({name.split(".")[0] for name in sys.modules} & {"tkinter", "customtkinter", "pandas", "sklearn"}))
"""


def test_generation_needs_no_desktop_packages(tmp_path) -> None:
    """A fresh process generates through the hosted API without importing Tk, pandas or scikit-learn."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = tmp_path / "backend_only.py"
    script.write_text(BACKEND_ONLY_SCRIPT, encoding="utf-8")
    result = subprocess.run([sys.executable, str(script)], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=120,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
