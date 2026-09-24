"""The local web profile (app/web) end to end: loopback protection, offline
planning that survives restarts, account/ownerless isolation for every
operation family, the account and association workflow, sync status and
Sync now over the existing sync client, conflicts, account switches during
work, backend configuration, shutdown, and serving the frontend."""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.planning import workflow
from app.planning.scope import OwnerScope
from tests.sync.conftest import PASSWORD, InProcessTransport

DAY = "2026-03-02"
RANGE = {"start_date": DAY, "end_date": DAY, "timezone": "UTC"}


def block(label: str = "Sleep", start: str = "00:00", end: str = "07:00") -> dict:
    return {"label": label, "category": "sleep", "planned_date": DAY, "timezone": "UTC",
            "planned_start": f"{DAY}T{start}:00Z", "planned_end": f"{DAY}T{end}:00Z"}


def rows(local, table: str) -> list[tuple]:
    return [tuple(row) for row in local.runtime.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]


def local_state(local) -> tuple:
    return tuple(rows(local, table) for table in ("tasks", "fixed_blocks", "preference_overrides", "sync_dirty",
                                                   "sync_outbox", "sync_accounts"))


# -----------------------------------------------------------------------------
# Loopback protection
# -----------------------------------------------------------------------------


def test_requests_must_be_addressed_to_the_service(make_local) -> None:
    local = make_local(backend_url=None)
    response = local.client.get("/local/session", headers={"Host": "evil.example"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "host_not_allowed"


def test_every_api_needs_the_local_session(make_local) -> None:
    local = make_local(backend_url=None)
    with TestClient(local.app, base_url="http://testserver") as stranger:
        assert stranger.get("/local/session").json() == {"session": False, "csrf_token": None, "profile": "local",
                                                         "workspace": None}
        for path in ("/tasks", "/planning/snapshot?start_date=2026-03-02&end_date=2026-03-02&timezone=UTC",
                     "/local/sync/status", "/local/conflicts"):
            response = stranger.get(path)
            assert response.status_code == 401 and response.json()["error"]["code"] == "local_session_required", path
        # The bootstrap code was already used by `local`; it works exactly once.
        reused = stranger.post("/local/session", json={"bootstrap_code": local.config.bootstrap_code})
        assert reused.status_code == 401 and reused.json()["error"]["code"] == "bootstrap_invalid"


def test_changes_need_the_csrf_token_and_the_same_origin(make_local) -> None:
    local = make_local(backend_url=None)
    body = {"name": "x", "category": "c", "estimated_duration_minutes": 5, "priority": 1}
    assert local.client.post("/tasks", json=body).json()["error"]["code"] == "csrf_failed"
    evil = local.client.post("/tasks", json=body, headers={"X-CSRF-Token": local.csrf, "Origin": "http://evil.example"})
    assert evil.status_code == 403 and evil.json()["error"]["code"] == "origin_not_allowed"
    same = local.client.post("/tasks", json=body, headers={"X-CSRF-Token": local.csrf, "Origin": "http://testserver"})
    assert same.status_code == 201
    assert local.task_names() == {"x"}


def test_the_browser_never_receives_the_backend_token(make_local, transport) -> None:
    local = make_local(transport=transport)
    local.sign_in("alice@example.com")
    token = local.runtime.sync._token
    assert token
    for path in ("/local/session", "/local/account", "/local/sync/status", "/local/backend"):
        assert token not in local.get(path).text
    assert "sm_local_session" in local.client.cookies and token not in str(local.client.cookies)


# -----------------------------------------------------------------------------
# Offline use
# -----------------------------------------------------------------------------


def test_offline_create_generate_and_restart(make_local) -> None:
    local = make_local(backend_url=None)
    local.ok(local.post("/fixed-blocks", block()), 201)
    task = local.create_task("Essay", preferred_dates=[DAY])
    generated = local.ok(local.post("/planning/generate", RANGE))
    assert generated["status"] == "generated" and generated["days"][0]["placements"][0]["task_id"] == task["id"]
    status = local.ok(local.get("/local/sync/status"))
    assert status["backend"]["configured"] is False and status["pending"] is None and status["last_status"] == "inert"

    local.restart()
    snapshot = local.ok(local.get("/planning/snapshot", **RANGE))
    assert snapshot["days"][0]["status"] == "current" and len(snapshot["placements"]) == 1
    assert local.ok(local.post("/planning/generate", RANGE))["status"] == "already_current"


# -----------------------------------------------------------------------------
# Scopes: account A, account B and the ownerless workspace
# -----------------------------------------------------------------------------


def test_account_and_ownerless_scopes_are_separate_for_every_operation(make_local, transport) -> None:
    local = make_local(transport=transport)
    local.ok(local.post("/projects", {"name": "Offline project"}), 201)
    local.create_task("Offline task", preferred_dates=[DAY])
    local.ok(local.post("/fixed-blocks", block("Offline sleep")), 201)
    local.ok(local.post("/preferences", {"scope": "user", "overrides": {"optimizer_mode": "adhd_friendly"}}), 201)
    local.ok(local.post("/planning/generate", RANGE))

    local.sign_in("alice@example.com")  # not associated: the ownerless records stay ownerless and invisible
    assert local.task_names() == set()
    assert local.ok(local.get("/projects"))["items"] == [] and local.ok(local.get("/fixed-blocks"))["items"] == []
    assert local.ok(local.get("/preferences"))["items"] == []
    assert local.ok(local.get("/planning/snapshot", **RANGE))["placements"] == []
    alice_task = local.create_task("Alice task", preferred_dates=[DAY])
    local.ok(local.post("/fixed-blocks", block("Alice sleep")), 201)  # same interval, another owner: no overlap
    generated = local.ok(local.post("/planning/generate", RANGE))
    assert [p["task_id"] for p in generated["days"][0]["placements"]] == [alice_task["id"]]
    exported = local.get("/planning/csv/export").text
    assert "Alice task" in exported and "Offline task" not in exported
    reset = local.ok(local.post("/planning/reset/preview", {"start_date": DAY, "end_date": DAY}))
    assert reset["task_ids"] == [alice_task["id"]]
    preferences = local.ok(local.get("/planning/preferences", **RANGE))
    assert preferences["user_layer"] is None and preferences["days"][0]["effective"]["optimizer_mode"] == "precise_greedy"

    local.ok(local.post("/local/account/sign-out"))
    local.sign_in("bob@example.com")
    assert local.task_names() == set() and local.get("/planning/csv/export").text.count("\n") == 1

    local.ok(local.post("/local/account/sign-out"))
    assert local.task_names() == {"Offline task"}
    assert local.ok(local.get("/planning/preferences", **RANGE))["user_layer"]["overrides"]["optimizer_mode"] == \
        "adhd_friendly"
    owners = {row[0]: row[1] for row in local.runtime.connection.execute("SELECT name, user_id FROM tasks")}
    assert owners["Offline task"] is None and owners["Alice task"] is not None


def test_another_scopes_record_cannot_be_read_changed_or_deleted(make_local, transport) -> None:
    local = make_local(transport=transport)
    local.sign_in("alice@example.com")
    task = local.create_task("Alice task")
    local.ok(local.post("/local/account/sign-out"))
    local.sign_in("bob@example.com")
    assert local.get(f"/tasks/{task['id']}").status_code == 404
    update = local.put(f"/tasks/{task['id']}", {"name": "stolen", "category": "study", "estimated_duration_minutes": 60,
                                               "priority": 5, "base_version": task["version"]})
    assert update.status_code == 404
    assert local.delete(f"/tasks/{task['id']}", base_version=task["version"]).status_code == 404
    local.ok(local.post("/local/account/sign-out"))
    local.sign_in("alice@example.com")
    assert local.ok(local.get(f"/tasks/{task['id']}"))["name"] == "Alice task"


# -----------------------------------------------------------------------------
# Accounts
# -----------------------------------------------------------------------------


def test_register_sign_in_profile_expiry_and_sign_out(make_local, transport) -> None:
    local = make_local(transport=transport)
    created = local.ok(local.post("/local/account/register", {"email": "carol@example.com", "password": PASSWORD}), 201)
    assert created["email"] == "carol@example.com" and "password" not in str(created)
    duplicate = local.post("/local/account/register", {"email": "carol@example.com", "password": PASSWORD})
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "account_exists"
    wrong = local.post("/local/account/sign-in", {"email": "carol@example.com", "password": "wrong password"})
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_credentials"

    signed_in = local.sign_in("carol@example.com")
    assert signed_in["workspace"]["scope"] == "account" and signed_in["unassociated_records"] == 0
    account = local.ok(local.get("/local/account"))
    assert account["profile"]["email"] == "carol@example.com"
    renamed = local.ok(local.patch("/local/account/profile", {"base_version": account["profile"]["version"],
                                                              "display_name": "Carol"}))
    assert renamed["display_name"] == "Carol"

    local.runtime.sync._token = "expired-token"  # the backend no longer accepts it
    assert local.ok(local.post("/local/sync"))["status"] == "auth_required"
    status = local.ok(local.get("/local/sync/status"))
    assert status["auth_required"] and not status["signed_in"] and status["account"]["email"] == "carol@example.com"
    assert local.ok(local.get("/local/account"))["profile"] is None
    assert local.ok(local.get("/local/session"))["workspace"]["scope"] == "account"  # data stays in view

    workspace = local.ok(local.post("/local/account/sign-out"))
    assert workspace["scope"] == "ownerless" and workspace["account"] is None


def test_registration_needs_a_backend(make_local) -> None:
    local = make_local(backend_url=None)
    response = local.post("/local/account/register", {"email": "x@example.com", "password": PASSWORD})
    assert response.status_code == 409 and response.json()["error"]["code"] == "backend_not_configured"


# -----------------------------------------------------------------------------
# Association of ownerless data
# -----------------------------------------------------------------------------


def test_new_records_while_signed_in_belong_to_the_account_without_claiming_old_ones(make_local, transport, backend) -> None:
    local = make_local(transport=transport)
    offline = local.create_task("Offline")
    signed_in = local.sign_in("alice@example.com")
    assert signed_in["unassociated_records"] == 1
    online = local.create_task("Online")
    local.sync()

    on_server = {task["name"] for task in backend.get("alice@example.com", "/tasks")["items"]}
    assert on_server == {"Online"}
    owners = dict(local.runtime.connection.execute("SELECT id, user_id FROM tasks").fetchall())
    assert owners[offline["id"]] is None and owners[online["id"]] == local.runtime.sync.account.user_id
    assert local.runtime.sync.account.associated_at is None  # creating records is not the association step


def test_previewing_or_cancelling_association_changes_nothing(make_local, transport) -> None:
    local = make_local(transport=transport)
    local.create_task("Offline")
    local.ok(local.post("/fixed-blocks", block()), 201)
    local.sign_in("alice@example.com")
    before = local_state(local)
    preview = local.ok(local.get("/local/association/preview"))
    assert preview["counts"]["task"] == 1 and preview["counts"]["fixed_block"] == 1 and preview["problems"] == []
    assert local_state(local) == before  # nothing claimed, versioned, queued or activated


def test_confirmed_association_claims_the_preview_and_syncs_with_the_same_ids(make_local, transport, backend) -> None:
    local = make_local(transport=transport)
    task = local.create_task("Offline")
    local.sign_in("alice@example.com")
    preview = local.ok(local.get("/local/association/preview"))
    result = local.ok(local.post("/local/association", {"confirmation": preview["token"]}))
    assert result["associated"]["task"] == 1 and result["workspace"]["account"]["associated"] is True
    assert local.task_names() == {"Offline"}  # now in the account's scope
    local.sync()
    assert [t["id"] for t in backend.get("alice@example.com", "/tasks")["items"]] == [task["id"]]


def test_association_refuses_a_stale_preview(make_local, transport) -> None:
    local = make_local(transport=transport)
    local.create_task("Offline")
    local.sign_in("alice@example.com")
    preview = local.ok(local.get("/local/association/preview"))
    local.ok(local.post("/local/account/sign-out"))
    local.create_task("Added after the preview")
    local.sign_in("alice@example.com")
    before = local_state(local)

    stale = local.post("/local/association", {"confirmation": preview["token"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "preview_changed"
    assert local_state(local) == before
    fresh = local.ok(local.get("/local/association/preview"))
    assert fresh["counts"]["task"] == 2
    local.ok(local.post("/local/association", {"confirmation": fresh["token"]}))


def test_association_reports_and_refuses_scope_collisions(make_local, transport) -> None:
    local = make_local(transport=transport)
    local.ok(local.post("/preferences", {"scope": "user", "overrides": {"optimizer_mode": "adhd_friendly"}}), 201)
    local.sign_in("alice@example.com")
    local.ok(local.post("/preferences", {"scope": "user", "overrides": {"optimizer_mode": "precise_greedy"}}), 201)
    preview = local.ok(local.get("/local/association/preview"))
    assert [problem["code"] for problem in preview["problems"]] == ["scope_taken"]
    refused = local.post("/local/association", {"confirmation": preview["token"]})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "association_blocked"


# -----------------------------------------------------------------------------
# Sync status, Sync now, and the existing sync guarantees through the local API
# -----------------------------------------------------------------------------


def test_pending_counts_and_the_last_sync_persist_across_restarts(make_local, flaky, backend) -> None:
    local = make_local(transport=flaky)
    local.sign_in("alice@example.com")
    local.create_task("One")
    local.create_task("Two")
    flaky.fail_push = 1
    offline = local.sync()
    assert offline["status"] == "offline" and offline["sync"]["backend"]["reachable"] is False
    status = offline["sync"]
    assert status["pending"] == 2 and status["last_error"] and status["last_successful_sync_at"] is None

    local.restart()
    local.sign_in("alice@example.com")  # the token was only in memory
    assert local.ok(local.get("/local/sync/status"))["pending"] == 2
    done = local.sync()
    assert done["status"] == "ok" and done["pushed"] == 2 and done["sync"]["pending"] == 0
    synced_at = done["sync"]["last_successful_sync_at"]
    assert synced_at is not None

    local.restart()
    local.sign_in("alice@example.com")
    assert local.ok(local.get("/local/sync/status"))["last_successful_sync_at"] == synced_at
    assert {t["name"] for t in backend.get("alice@example.com", "/tasks")["items"]} == {"One", "Two"}


def test_a_lost_response_is_replayed_without_duplicates(make_local, flaky, backend) -> None:
    local = make_local(transport=flaky)
    local.sign_in("alice@example.com")
    local.create_task("Once")
    flaky.lose_push_response = 1
    assert local.sync()["status"] == "offline"
    assert local.sync()["status"] == "ok"
    assert [t["name"] for t in backend.get("alice@example.com", "/tasks")["items"]] == ["Once"]
    assert len([c for c in backend.changes("alice@example.com") if c["entity_type"] == "task"]) == 1


def test_edits_during_a_sync_are_kept_and_sent_next(make_local, flaky, backend) -> None:
    local = make_local(transport=flaky)
    local.sign_in("alice@example.com")
    task = local.create_task("Draft")

    def edit_while_in_flight() -> None:
        service = local.runtime.planning.scoped(local.runtime.scope())
        stored = service.get_task(uuid.UUID(task["id"]))
        service.update_task(stored.model_copy(update={"name": "Final"}), expected_version=stored.version)

    flaky.during_push = edit_while_in_flight
    result = local.sync()
    # The acknowledgement of the in-flight create did not clear the newer edit: it was sent in the next push
    # round (against the new server version), so nothing was lost or overwritten.
    assert result["status"] == "ok" and result["pushed"] == 2 and result["sync"]["pending"] == 0
    [on_server] = backend.get("alice@example.com", "/tasks")["items"]
    assert on_server["name"] == "Final" and on_server["version"] == 2


def test_two_devices_and_tombstones(make_local, backend) -> None:
    first = make_local("first", transport=InProcessTransport(backend.client))
    second = make_local("second", transport=InProcessTransport(backend.client))
    for device in (first, second):
        device.sign_in("alice@example.com")
    task = first.create_task("Shared")
    first.sync()
    second.sync()
    assert second.ok(second.get(f"/tasks/{task['id']}"))["name"] == "Shared"

    stored = second.ok(second.get(f"/tasks/{task['id']}"))
    second.ok(second.delete(f"/tasks/{task['id']}", base_version=stored["version"]))
    second.sync()
    first.sync()
    assert first.get(f"/tasks/{task['id']}").status_code == 404
    assert first.ok(first.get(f"/tasks/{task['id']}", include_deleted=True))["deleted_at"] is not None


def test_sync_now_refuses_a_duplicate_request(make_local, transport) -> None:
    local = make_local(transport=transport)
    local.sign_in("alice@example.com")
    with local.runtime.sync._sync_lock:  # a sync is running
        response = local.post("/local/sync")
    assert response.status_code == 409 and response.json()["error"]["code"] == "sync_in_progress"


# -----------------------------------------------------------------------------
# Conflicts
# -----------------------------------------------------------------------------


def _task_body(task: dict, **changes) -> dict:
    fields = ("name", "category", "estimated_duration_minutes", "priority", "tags", "required", "preferred_dates",
              "dependency_ids")
    return {**{name: task[name] for name in fields}, **changes, "base_version": task["version"]}


def _shared_task(make_local, backend):
    first = make_local("first", transport=InProcessTransport(backend.client))
    second = make_local("second", transport=InProcessTransport(backend.client))
    for device in (first, second):
        device.sign_in("alice@example.com")
    task = first.create_task("Shared")
    first.sync()
    second.sync()
    return first, second, task


def test_a_concurrent_edit_conflict_offers_both_supported_actions(make_local, backend) -> None:
    first, second, task = _shared_task(make_local, backend)
    first.ok(first.put(f"/tasks/{task['id']}", _task_body(first.ok(first.get(f"/tasks/{task['id']}")), name="First")))
    first.sync()
    second.ok(second.put(f"/tasks/{task['id']}", _task_body(second.ok(second.get(f"/tasks/{task['id']}")), name="Second")))
    assert second.sync()["conflicts"] == 1

    [conflict] = second.ok(second.get("/local/conflicts"))
    assert conflict["allowed_actions"] == ["accept_remote", "keep_local"] and conflict["unavailable_actions"] == {}
    assert conflict["local_record"]["name"] == "Second" and conflict["remote_record"]["name"] == "First"
    resolved = second.ok(second.post(f"/local/conflicts/{conflict['id']}/resolve", {"choice": "keep_local"}))
    assert resolved["status"] == "resolved" and resolved["resolution"]["choice"] == "keep_local"
    second.sync()
    assert backend.get("alice@example.com", f"/tasks/{task['id']}")["name"] == "Second"
    assert second.ok(second.get("/local/conflicts")) == []
    assert second.ok(second.get("/local/conflicts", status="resolved"))[0]["id"] == conflict["id"]


def test_a_remote_deletion_cannot_be_kept_locally(make_local, backend) -> None:
    first, second, task = _shared_task(make_local, backend)
    stored = first.ok(first.get(f"/tasks/{task['id']}"))
    first.ok(first.delete(f"/tasks/{task['id']}", base_version=stored["version"]))
    first.sync()
    second.ok(second.put(f"/tasks/{task['id']}", _task_body(second.ok(second.get(f"/tasks/{task['id']}")), name="Edited")))
    second.sync()

    [conflict] = second.ok(second.get("/local/conflicts"))
    assert conflict["remote_deleted"] is True and conflict["allowed_actions"] == ["accept_remote"]
    assert "deleted on the server" in conflict["unavailable_actions"]["keep_local"]
    refused = second.post(f"/local/conflicts/{conflict['id']}/resolve", {"choice": "keep_local"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "resolution_refused"
    second.ok(second.post(f"/local/conflicts/{conflict['id']}/resolve", {"choice": "accept_remote"}))
    assert second.get(f"/tasks/{task['id']}").status_code == 404
    assert second.ok(second.get(f"/local/conflicts/{conflict['id']}"))["status"] == "resolved"


# -----------------------------------------------------------------------------
# Account switches while work runs
# -----------------------------------------------------------------------------


def test_a_generation_keeps_its_scope_when_the_account_changes_meanwhile(make_local, transport, monkeypatch) -> None:
    local = make_local(transport=transport)
    local.sign_in("alice@example.com")
    task = local.create_task("Alice work", preferred_dates=[DAY])
    alice = local.runtime.sync.account.user_id
    real = workflow.generate_selected_day

    def switching(*args, **kwargs):
        local.runtime.sync.sign_in("bob@example.com", PASSWORD)
        return real(*args, **kwargs)

    monkeypatch.setattr(workflow, "generate_selected_day", switching)
    generated = local.ok(local.post("/planning/generate", RANGE))
    monkeypatch.setattr(workflow, "generate_selected_day", real)

    assert [p["task_id"] for p in generated["days"][0]["placements"]] == [task["id"]]
    assert local.runtime.sync.account.email == "bob@example.com"
    assert local.ok(local.get("/planning/snapshot", **RANGE))["placements"] == []  # bob sees nothing of it
    owners = {row[0] for row in local.runtime.connection.execute("SELECT user_id FROM scheduled_tasks")}
    records = {row[0] for row in local.runtime.connection.execute("SELECT user_id FROM schedule_generations")}
    assert owners == records == {alice}


def test_an_account_switch_waits_for_a_running_sync(make_local, flaky, backend) -> None:
    local = make_local(transport=flaky)
    local.sign_in("alice@example.com")
    local.create_task("Alice's")
    switched = threading.Event()

    def switch_in_the_middle() -> None:
        def sign_in_bob() -> None:
            local.runtime.sync.sign_in("bob@example.com", PASSWORD)
            switched.set()

        threading.Thread(target=sign_in_bob, daemon=True).start()
        time.sleep(0.3)
        assert not switched.is_set()  # blocked until this sync (alice's) has finished

    flaky.during_push = switch_in_the_middle
    assert local.sync()["status"] == "ok"
    assert switched.wait(10)
    assert [t["name"] for t in backend.get("alice@example.com", "/tasks")["items"]] == ["Alice's"]
    assert backend.get("bob@example.com", "/tasks")["items"] == []
    assert local.runtime.sync.account.email == "bob@example.com"


# -----------------------------------------------------------------------------
# Backend configuration
# -----------------------------------------------------------------------------


def test_backend_configuration_validation_and_switching(make_local, transport) -> None:
    local = make_local(transport=transport)
    bad = local.put("/local/backend", {"backend_url": "ftp://backend.test"})
    assert bad.status_code == 422
    assert local.ok(local.post("/local/backend/check"))["reachable"] is True

    local.sign_in("alice@example.com")
    switched = local.ok(local.put("/local/backend", {"backend_url": "http://other.test"}))
    assert switched["backend_url"] == "http://other.test"
    assert local.ok(local.get("/local/session"))["workspace"]["scope"] == "ownerless"  # the session ended first
    assert local.runtime.sync._engine.store.setting("backend_url") == "http://other.test"

    offline = local.ok(local.put("/local/backend", {"backend_url": None}))
    assert offline["configured"] is False
    local.backend_url = None  # restart with no URL on the command line: the saved choice applies
    local.restart()
    assert local.ok(local.get("/local/backend"))["configured"] is False  # the choice persists


def test_an_invalid_backend_url_never_blocks_offline_use(make_local, transport) -> None:
    local = make_local(transport=transport, backend_url="not a url")
    backend = local.ok(local.get("/local/backend"))
    assert backend["configured"] is False and "must start with" in backend["error"]
    assert local.create_task("Still works")["name"] == "Still works"


# -----------------------------------------------------------------------------
# Shutdown, capabilities, frontend
# -----------------------------------------------------------------------------


def test_shutdown_waits_for_work_in_progress(make_local, monkeypatch) -> None:
    local = make_local(backend_url=None)
    local.create_task("Slow", preferred_dates=[DAY])
    real = workflow.generate_selected_day
    started = threading.Event()

    def slow(*args, **kwargs):
        started.set()
        time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(workflow, "generate_selected_day", slow)
    result: dict = {}
    worker = threading.Thread(target=lambda: result.setdefault("response", local.post("/planning/generate", RANGE)))
    worker.start()
    assert started.wait(10)
    assert local.runtime.close() is True  # waited for the generation, then closed the database
    worker.join(10)
    assert result["response"].status_code == 200
    with pytest.raises(Exception):
        local.runtime.connection.execute("SELECT 1")
    assert local.client.get("/local/session").json()["error"]["code"] == "shutting_down"


def test_local_capabilities_describe_device_persistence(make_local) -> None:
    local = make_local(backend_url=None)
    capabilities = local.ok(local.get("/planning/capabilities"))
    assert capabilities["profile"] == "local" and capabilities["persistence"] == "device"
    assert capabilities["reports_device_pending_changes"] is True and capabilities["extra"]["timezone"] == "UTC"


def test_the_built_frontend_is_served_with_deep_links(make_local, tmp_path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    local = make_local(backend_url=None, static_dir=dist)
    assert local.get("/").text == "<html>app</html>"
    assert local.get("/week/2026-03-02").text == "<html>app</html>"  # an SPA route
    assert local.get("/assets/app.js").text == "console.log(1)"
    assert local.get("/assets/missing.js").status_code == 404
    api_miss = local.get("/planning/unknown")
    assert api_miss.status_code in (404, 405) and "error" in api_miss.json()
    assert local.get("/../secrets.txt").status_code == 404


def test_scope_helper_matches_the_selected_account(make_local, transport) -> None:
    local = make_local(transport=transport)
    assert local.runtime.scope() == OwnerScope.ownerless()
    local.sign_in("alice@example.com")
    assert local.runtime.scope() == OwnerScope.account(uuid.UUID(local.runtime.sync.account.user_id))


def test_a_mixed_owner_store_keeps_every_owner_apart(make_local, transport, backend) -> None:
    """A device that already holds records of two accounts and ownerless ones (e.g. from earlier sign-ins)."""
    local = make_local(transport=transport)
    local.sign_in("bob@example.com")
    bob_task = local.create_task("Bob's")
    bob = local.runtime.sync.account.user_id
    local.ok(local.post("/local/account/sign-out"))
    ownerless = local.create_task("Nobody's")
    local.sign_in("alice@example.com")
    assert local.task_names() == set()

    preview = local.ok(local.get("/local/association/preview"))
    assert preview["counts"]["task"] == 1  # only the ownerless one; bob's record is never claimable
    local.ok(local.post("/local/association", {"confirmation": preview["token"]}))
    owners = dict(local.runtime.connection.execute("SELECT id, user_id FROM tasks").fetchall())
    assert owners[bob_task["id"]] == bob and owners[ownerless["id"]] == local.runtime.sync.account.user_id
    assert local.task_names() == {"Nobody's"}
    local.sync()
    assert [t["name"] for t in backend.get("alice@example.com", "/tasks")["items"]] == ["Nobody's"]
    assert backend.get("bob@example.com", "/tasks")["items"] == []  # bob's local record waits for bob's own sync
