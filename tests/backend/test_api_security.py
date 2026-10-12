"""
Authorization and API security (docs/api-security.md). Two accounts, one adversarial: whatever Bob sends --
Alice's ids, guessed ids, forged owner fields, mixed batches -- Alice's data is neither shown to him nor
changed. Plus the transport-level controls: token validation, CSRF, CORS, headers, limits, error hygiene
and the email-change flow. test_isolation.py covers the plain CRUD routes one by one; this file covers
everything else and the invariant across all of it.
"""

from __future__ import annotations

import json
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import models
from backend.app import create_app
from backend.executions import ACTIONS
from backend.security import TOKEN_TYPE, issue_access_token
from backend.settings import JWT_ALGORITHM, BackendSettings
from tests.backend.conftest import PASSWORD, TEST_SECRET, FakeMail, login_headers, register, seed, task_payload
from tests.backend.test_sync_push import op, push

NAMES = ("Thesis", "Study", "Sleep")  # what Alice's seeded records are called


def everything(client, headers) -> dict:
    """All an account can read about itself, for before/after comparison."""
    reads = {path: client.get(path, headers=headers) for path in (
        "/planning/snapshot?start_date=2026-03-01&end_date=2026-03-31&timezone=UTC", "/planning/csv/export",
        "/executions", "/days/summary?start_date=2026-03-01&end_date=2026-03-31",
        "/projects?include_deleted=true", "/tasks?include_deleted=true", "/fixed-blocks?include_deleted=true",
        "/placements?include_deleted=true", "/preferences?include_deleted=true",
        "/schedule-generations?include_deleted=true", "/task-types?include_deleted=true", "/me")}
    reads["/changes"] = client.get("/changes", params={"limit": 500}, headers=headers)
    return {path: (response.status_code, response.text) for path, response in reads.items()}


@pytest.fixture
def two(client):
    register(client, "alice@example.com")
    register(client, "bob@example.com")
    alice, bob = login_headers(client, "alice@example.com"), login_headers(client, "bob@example.com")
    return alice, bob, seed(client, alice)


def test_nothing_bob_sends_with_alices_ids_reads_or_changes_her_data(client, two) -> None:
    alice, bob, records = two
    before = everything(client, alice)
    ids = {name: record["id"] for name, record in records.items()}
    attempts: list[tuple[str, str, dict | None]] = []
    for action in ACTIONS:
        attempts.append(("POST", f"/executions/{ids['executions']}/actions/{action}", {"base_version": 1}))
    attempts += [
        ("POST", f"/executions/{ids['executions']}/feedback", {"base_version": 1, "note": "mine now"}),
        ("DELETE", f"/executions/{ids['executions']}?base_version=1", None),
        ("POST", f"/placements/{ids['placements']}/outcome", {"outcome": "completed"}),
        ("POST", f"/planning/placements/{ids['placements']}/reschedule", {
            "base_version": 1, "planned_start": "2026-03-02T12:00:00Z", "planned_end": "2026-03-02T13:00:00Z"}),
        ("POST", f"/planning/placements/{ids['placements']}/release", {"base_version": 1}),
        ("POST", f"/planning/occurrences/{ids['tasks']}/delete", {"base_version": 1}),
        ("POST", f"/planning/occurrences/{ids['tasks']}/edit", {"base_version": 1, "name": "Hijacked"}),
        ("POST", f"/planning/series/{ids['tasks']}/delete", {"base_version": 1}),
        ("POST", f"/planning/series/{ids['tasks']}/edit", {"base_version": 1, "name": "Hijacked"}),
        ("POST", "/placements", {"task_id": ids["tasks"], "planned_date": "2026-03-03", "timezone": "UTC",
                                 "planned_start": "2026-03-03T09:00:00Z", "planned_end": "2026-03-03T10:00:00Z"}),
        ("POST", "/tasks", task_payload(project_id=ids["projects"])),
        ("POST", "/executions", {"task_id": ids["tasks"], "scheduled_task_id": ids["placements"], "task_name": "x",
                                 "category": "study", "planned_duration": 30, "priority": 3}),
    ]
    for path, key in (("projects", "projects"), ("tasks", "tasks"), ("fixed-blocks", "fixed-blocks"),
                      ("placements", "placements"), ("preferences", "preferences"),
                      ("schedule-generations", "schedule-generations")):
        attempts += [("GET", f"/{path}/{ids[key]}", None), ("DELETE", f"/{path}/{ids[key]}?base_version=1", None),
                     ("PUT", f"/{path}/{ids[key]}", {"base_version": 1, "name": "Hijacked"})]
    for method, path, body in attempts:
        response = client.request(method, path, json=body, headers=bob)
        assert response.status_code in (404, 409, 422), (method, path, response.status_code, response.text[:200])
        assert not any(name in response.text for name in NAMES), (method, path)
    # Bob's own destructive operations touch only Bob.
    assert client.post("/me/task-data/reset", json={"confirm": True}, headers=bob).status_code == 200
    assert everything(client, alice) == before


def test_bobs_reads_never_contain_alices_records(client, two) -> None:
    alice, bob, records = two
    theirs = {record["id"] for record in records.values()}
    for path, (status, text) in everything(client, bob).items():
        assert status == 200, path
        assert not any(identifier in text for identifier in theirs), path
        if path != "/me":
            assert not any(name in text for name in NAMES), path
    cohort = client.get("/planning/analytics/schedule-cohort", headers=bob)
    assert cohort.status_code in (200, 422) and not any(identifier in cohort.text for identifier in theirs)
    # A cursor is a position in the caller's own change log: Alice's cursor shows Bob nothing of hers.
    alices_cursor = client.get("/changes", params={"limit": 500}, headers=alice).json()["cursor"]
    for after in (0, alices_cursor, max(0, alices_cursor - 1)):
        page = client.get("/changes", params={"after": after, "limit": 500}, headers=bob).json()
        assert page["changes"] == []


def test_sync_push_cannot_reach_another_account_alone_or_in_a_mixed_batch(client, two) -> None:
    alice, bob, records = two
    before = everything(client, alice)
    project, task = records["projects"], records["tasks"]
    hostile = [
        op("project", "update", project["id"], base_version=project["version"], payload={"name": "Hijacked"}),
        op("project", "delete", project["id"], base_version=project["version"]),
        op("task", "update", task["id"], base_version=task["version"], payload=task_payload(name="Hijacked")),
        op("task", "delete", task["id"], base_version=task["version"]),
        op("task", "create", payload=task_payload(name="Parasite", project_id=project["id"])),
        op("task", "create", payload={**task_payload(name="Forged owner"), "user_id": project["id"]}),
    ]
    own = op("project", "create", payload={"name": "Bob's own"})
    results = push(client, bob, own, *hostile)
    assert results[0]["status"] == "applied"  # his own record in the same batch is his
    assert all(result["status"] != "applied" for result in results[1:]), results
    assert "Hijacked" not in json.dumps(results) and "Thesis" not in json.dumps(results)
    assert everything(client, alice) == before
    # The same client-chosen id under Bob's account is Bob's record, not a way into Alice's.
    [twin] = push(client, bob, op("project", "create", project["id"], payload={"name": "Same id, Bob's"}))
    assert twin["status"] == "applied" and everything(client, alice) == before
    assert client.get(f"/projects/{project['id']}", headers=alice).json()["name"] == "Thesis"


@pytest.mark.parametrize("extra", [
    {"user_id": str(uuid.uuid4())}, {"owner_id": str(uuid.uuid4())}, {"id": str(uuid.uuid4()), "version": 99},
    {"created_at": "2020-01-01T00:00:00Z"}, {"deleted_at": None}, {"is_admin": True}, {"role": "admin"},
])
def test_server_controlled_fields_cannot_be_assigned(client, two, extra: dict) -> None:
    alice, _bob, _records = two
    assert client.post("/tasks", json={**task_payload(), **extra}, headers=alice).status_code == 422
    assert client.post("/projects", json={"name": "P", **extra}, headers=alice).status_code == 422
    profile = client.get("/me", headers=alice).json()
    for field in ({"email": "mallory@example.com"}, {"email_verified": True}, {"credential_epoch": 0},
                  {"password_hash": "x"}, extra):
        changed = client.patch("/me", json={"base_version": profile["version"], "display_name": "A", **field},
                               headers=alice)
        assert changed.status_code == 422, field
    assert client.get("/me", headers=alice).json() == profile


def test_there_is_no_sharing_so_no_route_takes_another_accounts_id(client) -> None:
    """Private ownership is the only model: nothing in the API names a second account or grants access."""
    schema = client.app.openapi()
    for path in schema["paths"]:
        assert not any(word in path.lower() for word in ("share", "collaborat", "member", "invite", "grant")), path
    text = json.dumps(schema)
    assert '"user_id"' not in text and '"owner_id"' not in text  # no request or response names an owner


# -- tokens -------------------------------------------------------------------------------------


def forged(settings, clock, *, key: str | None = None, algorithm: str = JWT_ALGORITHM, **claims) -> str:
    now = int(clock().timestamp())
    body = {"sub": str(uuid.uuid4()), "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": now,
            "nbf": now, "exp": now + 600, "jti": uuid.uuid4().hex, "typ": TOKEN_TYPE, "cep": 0, **claims}
    body = {name: value for name, value in body.items() if value is not None}
    return jwt.encode(body, key if key is not None else settings.jwt_secret, algorithm=algorithm)


def test_access_tokens_are_validated_for_key_algorithm_issuer_audience_expiry_and_purpose(client, settings, clock):
    register(client, "alice@example.com")
    with client.app.state.session_factory() as session:
        user_id = session.scalar(select(models.User.id))

    def status(token: str) -> int:
        return client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code

    assert status(issue_access_token(user_id, settings, clock()).token) == 200  # the control
    sub = str(user_id)
    bad = {
        "another key": forged(settings, clock, sub=sub, key="k" * 40),
        "another algorithm": forged(settings, clock, sub=sub, algorithm="HS512"),
        "another issuer": forged(settings, clock, sub=sub, iss="someone-else"),
        "another audience": forged(settings, clock, sub=sub, aud="another-api"),
        "no audience": forged(settings, clock, sub=sub, aud=None),
        "expired": forged(settings, clock, sub=sub, exp=int(clock().timestamp()) - 1),
        "not yet valid": forged(settings, clock, sub=sub, nbf=int(clock().timestamp()) + 600),
        "another purpose": forged(settings, clock, sub=sub, typ="refresh"),
        "no purpose": forged(settings, clock, sub=sub, typ=None),
        "no subject": forged(settings, clock, sub=None),
        "an older credential epoch": forged(settings, clock, sub=sub, cep=7),
        "a session that does not exist": forged(settings, clock, sub=sub, sid=str(uuid.uuid4())),
    }
    header, payload, _signature = bad["another key"].split(".")
    unsigned = jwt.utils.base64url_encode(b'{"alg":"none","typ":"JWT"}').decode()
    bad["alg none"] = f"{unsigned}.{payload}."
    bad["stripped signature"] = f"{header}.{payload}."
    for reason, token in bad.items():
        assert status(token) == 401, reason
    assert client.get("/me", headers={"Authorization": f"Basic {issue_access_token(user_id, settings, clock()).token}"}
                      ).status_code == 401


# -- browser sessions: cookies, CSRF, CORS ------------------------------------------------------


def browser_app(engine, clock, **overrides):
    settings = BackendSettings(**{"database_url": "sqlite://", "jwt_secret": TEST_SECRET,
                                  "browser_cookie_secure": False, **overrides})
    return create_app(settings, engine=engine, clock=clock)


def test_the_session_cookie_is_hardened_and_csrf_has_its_own_key(engine, clock) -> None:
    import hashlib
    import hmac

    with TestClient(create_app(BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET), engine=engine,
                               clock=clock), base_url="https://testserver") as client:
        register(client, "alice@example.com")
        signed_in = client.post("/auth/browser/login", json={"email": "alice@example.com", "password": PASSWORD})
        cookie = signed_in.headers["set-cookie"].lower()
        assert all(part in cookie for part in ("httponly", "secure", "samesite=strict", "path=/"))
        token, csrf = client.cookies["sm_session"], signed_in.json()["csrf_token"]
        # The CSRF token is not an HMAC under the token-signing key itself.
        assert csrf != hmac.new(TEST_SECRET.encode(), b"csrf:" + token.encode(), hashlib.sha256).hexdigest()
        assert token not in signed_in.text and csrf != token
        unsafe = {"name": "From the page"}
        assert client.post("/projects", json=unsafe).status_code == 403  # the cookie alone is not enough
        assert client.post("/projects", json=unsafe, headers={"X-CSRF-Token": "0" * 64}).status_code == 403
        assert client.post("/projects", json=unsafe, headers={"X-CSRF-Token": csrf,
                                                              "Origin": "https://evil.example"}).status_code == 403
        assert client.post("/projects", json=unsafe, headers={"X-CSRF-Token": csrf}).status_code == 201
        assert client.get("/projects").status_code == 200  # reads need no CSRF token


def test_cors_is_an_explicit_allowlist_and_never_authorization(engine, clock) -> None:
    app = browser_app(engine, clock, cors_origins=("https://app.example.com",))
    with TestClient(app) as client:
        allowed = client.options("/tasks", headers={"Origin": "https://app.example.com",
                                                    "Access-Control-Request-Method": "POST"})
        assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"
        assert allowed.headers["access-control-allow-credentials"] == "true"
        for origin in ("https://evil.example", "https://app.example.com.evil.example", "null"):
            refused = client.options("/tasks", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
            assert "access-control-allow-origin" not in refused.headers, origin
            assert client.get("/health", headers={"Origin": origin}).headers.get("access-control-allow-origin") is None
        # An allowed origin is still nobody without credentials...
        assert client.get("/tasks", headers={"Origin": "https://app.example.com"}).status_code == 401
        # ...and with a cookie, an allowed CORS origin does not replace the CSRF token.
        register(client, "alice@example.com")
        client.post("/auth/browser/login", json={"email": "alice@example.com", "password": PASSWORD})
        assert client.post("/projects", json={"name": "x"},
                           headers={"Origin": "https://app.example.com"}).status_code == 403
    with pytest.raises(Exception):
        BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET, cors_origins=("*",))


# -- headers, readiness, documentation, errors, limits ------------------------------------------


def test_every_response_carries_the_security_headers(client, engine, clock) -> None:
    for path in ("/health", "/tasks", "/no-such-route", "/auth/verify", "/auth/recovery/reset"):
        headers = client.get(path).headers
        assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff", path
        assert headers["referrer-policy"] == "no-referrer" and headers["x-frame-options"] == "DENY", path
        assert "frame-ancestors 'none'" in headers["content-security-policy"], path
        assert "strict-transport-security" not in headers  # plain-http development
    assert "script-src" in client.get("/auth/verify").headers["content-security-policy"]  # the page's own policy
    assert "content-security-policy" not in client.get("/docs").headers  # the viewer loads its own assets
    production = create_app(BackendSettings(
        database_url="postgresql+psycopg://u:p@db.example/app", jwt_secret=TEST_SECRET, environment="production",
        allowed_hosts=("testserver",)), engine=engine, clock=clock)
    with TestClient(production) as live:
        assert "max-age=31536000" in live.get("/health").headers["strict-transport-security"]
        for path in ("/docs", "/redoc", "/openapi.json"):  # off by default in production; a setting turns it on
            assert live.get(path).status_code == 404
    opted_in = create_app(BackendSettings(
        database_url="postgresql+psycopg://u:p@db.example/app", jwt_secret=TEST_SECRET, environment="production",
        allowed_hosts=("testserver",), api_docs_enabled=True), engine=engine, clock=clock)
    with TestClient(opted_in) as live:
        assert live.get("/openapi.json").status_code == 200


def test_public_health_and_readiness_say_only_what_a_load_balancer_needs(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready").json()
    assert ready == {"status": "ready", "database": "reachable", "migrations": "current"}
    assert not any(word in json.dumps(ready) for word in ("revision", "sqlite", "postgres", "version"))


def test_errors_never_carry_internals(client, two, monkeypatch) -> None:
    alice, _bob, _records = two
    from backend import api

    def explode(*_args, **_kwargs):
        raise RuntimeError("SELECT * FROM users; postgresql://admin:hunter2@db.internal/app; Traceback ...")

    monkeypatch.setattr(api, "_list", explode)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    broken = quiet.get("/tasks", headers=alice)
    assert broken.status_code == 500
    assert broken.json() == {"error": {"code": "internal_error", "message": "An unexpected error occurred."}}
    invalid = client.post("/tasks", json={"name": "x" * 5000, "password": "hunter2-in-a-field"}, headers=alice)
    assert invalid.status_code == 422 and "hunter2" not in invalid.text and "Traceback" not in invalid.text


def test_requests_are_bounded_in_size_batch_and_page(engine, clock) -> None:
    app = create_app(BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET, max_request_bytes=20_000),
                     engine=engine, clock=clock)
    with TestClient(app) as client:
        register(client, "alice@example.com")
        alice = login_headers(client, "alice@example.com")
        huge = client.post("/tasks", content=b'{"name": "' + b"x" * 30_000 + b'"}',
                           headers={**alice, "Content-Type": "application/json"})
        assert huge.status_code == 413 and huge.json()["error"]["code"] == "request_too_large"
        assert client.post("/tasks", json={**task_payload(), "name": "n" * 2000}, headers=alice).status_code == 422
        assert client.post("/tasks", json={**task_payload(), "tags": [f"t{i}" for i in range(60)]},
                           headers=alice).status_code == 422
        assert client.get("/tasks", params={"limit": 100_000}, headers=alice).status_code == 422
        assert client.get("/changes", params={"limit": 100_000}, headers=alice).status_code == 422
    default = create_app(BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET), engine=engine, clock=clock)
    with TestClient(default) as client:
        alice = login_headers(client, "alice@example.com")
        too_many = [op("project", "create", payload={"name": f"P{index}"}) for index in range(201)]
        assert client.post("/sync/push", json={"operations": too_many}, headers=alice).status_code == 422


# -- changing the email address -----------------------------------------------------------------


@pytest.fixture
def mailed(engine, clock):
    mail = FakeMail()
    settings = BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET,
                               app_public_url="https://app.example.com", smtp_sender="noreply@example.com",
                               smtp_host="smtp.example.com")
    with TestClient(create_app(settings, engine=engine, clock=clock, mail=mail)) as client:
        yield client, mail


def change_code(mail, address: str) -> str:
    return mail.to(address)[-1].text.split("\n\n")[2].strip()


def test_an_email_change_needs_the_password_the_new_mailbox_and_the_session(mailed, clock) -> None:
    client, mail = mailed
    register(client, "alice@example.com")
    register(client, "bob@example.com")
    alice, bob = login_headers(client, "alice@example.com"), login_headers(client, "bob@example.com")

    def ask(new_email: str, password: str = PASSWORD, headers=alice):
        return client.post("/auth/email/change/request", headers=headers,
                           json={"current_password": password, "new_email": new_email})

    assert ask("new@example.com", "not the password").status_code == 403  # reauthentication
    assert ask("a@example.com,b@example.org").status_code == 422 and ask("alice@example.com").status_code == 422
    assert client.post("/auth/email/change/request", json={"current_password": PASSWORD,
                                                           "new_email": "new@example.com"}).status_code == 401
    taken = ask("bob@example.com")
    assert taken.status_code == 202 and mail.to("bob@example.com")[-1].subject.startswith("Welcome")  # no code sent
    first = ask("typo@example.com")
    assert first.status_code == 202 and first.json() == taken.json()  # the same answer either way
    stale = change_code(mail, "typo@example.com")
    clock.advance(seconds=61)
    assert ask("new@example.com").status_code == 202
    code = change_code(mail, "new@example.com")
    assert client.get("/me", headers=alice).json()["email"] == "alice@example.com"  # nothing changed yet

    def confirm(token: str, headers=alice):
        return client.post("/auth/email/change/confirm", json={"token": token}, headers=headers)

    assert confirm(stale).status_code == 400  # the earlier request was cancelled by the newer one
    assert confirm(code, headers=bob).status_code == 400  # another account cannot use it
    assert client.post("/auth/email/change/confirm", json={"token": code}).status_code == 401  # nor nobody
    assert client.post("/auth/verify/confirm", json={"token": code, "password": PASSWORD}).status_code == 400
    assert confirm(code).status_code == 200
    assert confirm(code).status_code == 401  # every session ended, this one included
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 401
    fresh = login_headers(client, "new@example.com")
    profile = client.get("/me", headers=fresh).json()
    assert profile["email"] == "new@example.com" and profile["email_verified"] is True
    notice = mail.to("alice@example.com")[-1]
    assert "was changed" in notice.subject and code not in notice.text and "new@example.com" not in notice.text
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(models.User)) == 2


def test_a_password_change_or_reset_cancels_a_pending_email_change(mailed, clock) -> None:
    client, mail = mailed
    register(client, "alice@example.com")
    alice = login_headers(client, "alice@example.com")
    client.post("/auth/email/change/request", headers=alice,
                json={"current_password": PASSWORD, "new_email": "new@example.com"})
    code = change_code(mail, "new@example.com")
    changed = client.post("/auth/password/change", headers=alice,
                          json={"current_password": PASSWORD, "new_password": "an entirely different passphrase"})
    assert changed.status_code == 200
    again = login_headers(client, "alice@example.com", "an entirely different passphrase")
    assert client.post("/auth/email/change/confirm", json={"token": code}, headers=again).status_code == 400
    assert client.get("/me", headers=again).json()["email"] == "alice@example.com"
