"""
Operations (docs/security-operations.md): cleanup of expired authentication leftovers, redacted security
events and counters, a restore of a database copy, and the whole account journey end to end --
signup -> verification -> welcome -> sign-in -> two-step sign-in -> sync -> password change -> reset ->
session revocation -- on an isolated database with fake mail.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid

import pyotp
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import maintenance, models, outbox, security_events
from backend.app import create_app
from backend.database import create_backend_engine, session_factory
from backend.migrate import upgrade
from backend.settings import BackendSettings
from tests.backend.conftest import PASSWORD, TEST_SECRET, FakeMail, create, login_headers, register, task_payload
from tests.backend.test_sync_push import op, push

NEW_PASSWORD = "an entirely different passphrase"
FINAL_PASSWORD = "the third passphrase of this account"


def full_settings(**overrides) -> BackendSettings:
    return BackendSettings(**{
        "database_url": "sqlite://", "jwt_secret": TEST_SECRET, "app_public_url": "https://app.example.com",
        "smtp_sender": "noreply@example.com", "smtp_host": "smtp.example.com",
        "mfa_encryption_key": Fernet.generate_key().decode(), **overrides})


def count(factory, model, *where) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model).where(*where))


# -- cleanup ------------------------------------------------------------------------------------


def test_cleanup_removes_what_expired_keeps_reuse_evidence_and_never_touches_user_data(engine, clock) -> None:
    mail = FakeMail()
    app = create_app(full_settings(rate_limit_enabled=True), engine=engine, clock=clock, mail=mail)
    factory = app.state.session_factory
    with TestClient(app) as client:
        register(client, "alice@example.com")
        alice = login_headers(client, "alice@example.com")
        create(client, alice, "tasks", task_payload())
        pair = client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).json()
        rotated = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]}).json()
        client.post("/auth/register", json={"email": "never-confirmed@example.com"})
        client.post("/auth/recovery/request", json={"identifier": "alice@example.com"})
        client.post("/auth/login", json={"email": "alice@example.com", "password": "a wrong password"})
        assert maintenance.cleanup(factory, clock) == {}  # nothing is old enough yet

        clock.advance(days=3)  # the pending registration, its token and the recovery token are over
        removed = maintenance.cleanup(factory, clock)
        assert removed["pending_registrations"] == 1 and removed["password_recovery_tokens"] == 1
        assert removed["rate_limit_buckets"] >= 1 and "native_sessions" not in removed
        assert count(factory, models.EmailVerificationToken) == 0
        # The session is still refreshable, so the consumed credential stays: its reuse is still detected.
        assert count(factory, models.RefreshCredential, models.RefreshCredential.consumed_at.is_not(None)) == 1
        assert client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code == 401
        assert client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]}).status_code == 401

        clock.advance(days=120)  # past every session's absolute lifetime and the outbox retention
        removed = maintenance.cleanup(factory, clock, batch=1, rounds=50)  # small batches still finish the job
        assert removed["native_sessions"] == 2 and removed["email_outbox"] >= 2
        assert count(factory, models.NativeSession) == 0 and count(factory, models.RefreshCredential) == 0
        # Accounts and their records are not "expired" by authentication cleanup.
        assert count(factory, models.User) == 1 and count(factory, models.Task) == 1
        assert count(factory, models.SignInFingerprint) == 1
        assert maintenance.cleanup(factory, clock) == {}
        fresh = login_headers(client, "alice@example.com")
        assert [task["name"] for task in client.get("/tasks", headers=fresh).json()["items"]] == ["Study"]


def test_a_backlog_is_worked_off_in_bounded_batches(engine, clock) -> None:
    factory = session_factory(engine)
    with factory() as session:
        for index in range(25):
            session.add(models.RateLimitBucket(key=f"{index:064d}", scope="probe", count=1, expires_at=clock()))
        session.commit()
    clock.advance(seconds=1)
    assert maintenance.cleanup(factory, clock, batch=10, rounds=1) == {"rate_limit_buckets": 10}  # one bounded run
    assert maintenance.cleanup(factory, clock, batch=10, rounds=5) == {"rate_limit_buckets": 15}


# -- events -------------------------------------------------------------------------------------


def test_security_events_are_structured_counted_and_cannot_carry_secrets(caplog) -> None:
    security_events.reset()
    with caplog.at_level(logging.INFO, logger="schedule_maxing.security.events"):
        security_events.record("rate_limited", scope="login_fail_pair")
        security_events.record("rate_limited", scope="login_fail_pair")
        security_events.record("sign_in_failed", password="hunter2", email="alice@example.com", token="abc")
        security_events.record("email_not_accepted", kind="verification",
                               error="postgresql://admin:hunter2@db.internal/app")
        security_events.record("webhook_refused", reason="https://app.example.com/auth/verify#token=SECRET")
    lines = [json.loads(record.getMessage()) for record in caplog.records]
    assert lines[0] == {"event": "rate_limited", "scope": "login_fail_pair"}
    assert lines[2] == {"event": "sign_in_failed"}  # unknown fields are dropped, whatever they hold
    assert lines[3]["error"] == "[redacted]" and lines[4]["reason"] == "[redacted]"
    assert not any(secret in caplog.text for secret in ("hunter2", "alice@example.com", "SECRET", "db.internal"))
    assert security_events.counters()["rate_limited|scope=login_fail_pair"] == 2
    security_events.log_counters()
    security_events.reset()


def test_abuse_shows_up_in_the_counters_without_identifying_anyone(engine, clock, caplog) -> None:
    security_events.reset()
    mail = FakeMail()
    app = create_app(full_settings(rate_limit_enabled=True), engine=engine, clock=clock, mail=mail)
    with TestClient(app) as client, caplog.at_level(logging.DEBUG):
        register(client, "alice@example.com")
        for _ in range(7):
            client.post("/auth/login", json={"email": "alice@example.com", "password": "a wrong password"})
        client.post("/auth/verify/confirm", json={"token": "not-a-token", "password": PASSWORD})
        client.post("/auth/recovery/reset", json={"token": "not-a-token", "new_password": NEW_PASSWORD})
        mail.fail = True
        client.post("/auth/register", json={"email": "new@example.com"})
    totals = security_events.counters()
    assert totals["sign_in_failed"] == 5 and totals["rate_limited|scope=login_fail_pair"] == 2
    assert totals["verification_token_refused|kind=registration"] == 1 and totals["recovery_token_refused"] == 1
    assert totals["email_not_accepted|error=TimeoutError|kind=verification"] == 1
    for secret in ("alice@example.com", "new@example.com", "a wrong password", PASSWORD, "not-a-token"):
        assert secret not in caplog.text
    security_events.reset()


# -- restore ------------------------------------------------------------------------------------


def test_a_restored_copy_of_the_database_serves_the_same_accounts_and_records(tmp_path, clock) -> None:
    source_path, restored_path = tmp_path / "live.db", tmp_path / "restored.db"
    engine = create_backend_engine(f"sqlite:///{source_path}")
    upgrade(engine)
    key = Fernet.generate_key().decode()
    with TestClient(create_app(full_settings(mfa_encryption_key=key), engine=engine, clock=clock,
                               mail=FakeMail())) as client:
        register(client, "alice@example.com")
        alice = login_headers(client, "alice@example.com")
        task = create(client, alice, "tasks", task_payload(name="Before the backup"))
    engine.dispose()

    with sqlite3.connect(source_path) as live, sqlite3.connect(restored_path) as copy:  # the backup, then the restore
        live.backup(copy)
    restored = create_backend_engine(f"sqlite:///{restored_path}")
    with TestClient(create_app(full_settings(mfa_encryption_key=key), engine=restored, clock=clock,
                               mail=FakeMail())) as client:
        assert client.get("/ready").json()["status"] == "ready"  # the schema revision came with the data
        again = login_headers(client, "alice@example.com")  # the password hash too
        assert client.get(f"/tasks/{task['id']}", headers=again).json()["name"] == "Before the backup"
        assert client.get("/tasks", headers=alice).status_code == 200  # sessions are data as well
    restored.dispose()


# -- the whole journey --------------------------------------------------------------------------


def test_signup_verification_sign_in_two_steps_sync_password_change_reset_and_revocation(engine, clock) -> None:
    mail = FakeMail()
    app = create_app(full_settings(), engine=engine, clock=clock, mail=mail)

    def drain() -> None:
        outbox.process_due(app.state.session_factory, mail, app.state.settings, clock)

    with TestClient(app) as client:
        # Signup: an email, no account; the link's owner chooses the password; one welcome.
        assert client.post("/auth/register", json={"email": "Alice@Example.com"}).status_code == 202
        assert client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 401
        confirmed = client.post("/auth/verify/confirm", json={"token": mail.token("alice@example.com"),
                                                              "password": PASSWORD, "display_name": "Alice"})
        assert confirmed.status_code == 201 and confirmed.json()["email_verified"] is True
        drain()
        assert [message.subject for message in mail.sent].count("Welcome to Schedule Maxing") == 1

        # Sign-in, then two-step sign-in is turned on.
        first = client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).json()
        headers = {"Authorization": f"Bearer {first['access_token']}"}
        secret = client.post("/auth/mfa/totp/enroll", json={"current_password": PASSWORD}, headers=headers).json()["secret"]
        totp = pyotp.TOTP(secret)
        codes = client.post("/auth/mfa/totp/confirm", headers=headers, json={
            "current_password": PASSWORD, "code": totp.at(clock().timestamp())}).json()["recovery_codes"]
        clock.advance(seconds=30)

        def sign_in(password: str) -> dict:
            challenge = client.post("/auth/login", json={"email": "alice@example.com", "password": password}).json()
            assert challenge["mfa_required"] and "access_token" not in challenge
            done = client.post("/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"],
                                                         "code": totp.at(clock().timestamp())})
            assert done.status_code == 200, done.text
            clock.advance(seconds=30)
            return done.json()

        laptop, phone = sign_in(PASSWORD), sign_in(PASSWORD)
        laptop_headers = {"Authorization": f"Bearer {laptop['access_token']}"}
        phone_headers = {"Authorization": f"Bearer {phone['access_token']}"}

        # Sync: one device pushes, the other pulls the same record; a stranger sees nothing.
        project_id = str(uuid.uuid4())
        [applied] = push(client, laptop_headers, op("project", "create", project_id, payload={"name": "Thesis"}))
        assert applied["status"] == "applied"
        pulled = client.get("/changes", params={"limit": 100}, headers=phone_headers).json()["changes"]
        assert any(change["record"]["id"] == project_id for change in pulled if change.get("record"))
        register(client, "bob@example.com")
        bob = login_headers(client, "bob@example.com")
        assert client.get(f"/projects/{project_id}", headers=bob).status_code == 404
        assert client.get("/changes", params={"limit": 100}, headers=bob).json()["changes"] == []

        # One session is revoked: its token dies at once, the other device keeps working.
        sessions = client.get("/auth/sessions", headers=laptop_headers).json()["sessions"]
        others = [item["id"] for item in sessions if not item["current"]]
        assert len(sessions) == 3 and len(others) == 2
        phone_id = next(item["id"] for item in sessions
                        if not item["current"] and item["id"] != _session_of(app, first["refresh_token"]))
        assert client.delete(f"/auth/sessions/{phone_id}", headers=laptop_headers).status_code == 204
        assert client.get("/me", headers=phone_headers).status_code == 401
        assert client.get("/me", headers=laptop_headers).status_code == 200

        # Password change: needs the current password, ends every session, keeps the second factor.
        changed = client.post("/auth/password/change", headers=laptop_headers,
                              json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        assert changed.status_code == 200 and client.get("/me", headers=laptop_headers).status_code == 401
        assert client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 401
        again = sign_in(NEW_PASSWORD)
        again_headers = {"Authorization": f"Bearer {again['access_token']}"}
        assert client.get(f"/projects/{project_id}", headers=again_headers).json()["name"] == "Thesis"

        # Recovery reset: an emailed link, every session ends again, and signing in still takes two steps.
        assert client.post("/auth/recovery/request", json={"identifier": "alice@example.com"}).status_code == 202
        link_token = mail.token("alice@example.com")
        assert client.post("/auth/recovery/reset", json={"token": link_token,
                                                         "new_password": FINAL_PASSWORD}).status_code == 200
        assert client.post("/auth/recovery/reset", json={"token": link_token,
                                                         "new_password": "yet another passphrase"}).status_code == 400
        assert client.get("/me", headers=again_headers).status_code == 401
        final = sign_in(FINAL_PASSWORD)
        final_headers = {"Authorization": f"Bearer {final['access_token']}"}

        # A recovery code works once; sign out everywhere ends what is left.
        challenge = client.post("/auth/login", json={"email": "alice@example.com", "password": FINAL_PASSWORD}).json()
        by_code = client.post("/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"], "code": codes[0]})
        assert by_code.status_code == 200
        challenge = client.post("/auth/login", json={"email": "alice@example.com", "password": FINAL_PASSWORD}).json()
        assert client.post("/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"],
                                                     "code": codes[0]}).status_code == 401
        assert client.post("/auth/sessions/revoke-all", headers=final_headers).status_code == 204
        assert client.get("/me", headers=final_headers).status_code == 401
        assert client.get("/me", headers={"Authorization": f"Bearer {by_code.json()['access_token']}"}).status_code == 401
        assert client.post("/auth/refresh", json={"refresh_token": final["refresh_token"]}).status_code == 401

        drain()
        subjects = [message.subject for message in mail.to("alice@example.com")]
        assert subjects.count("Welcome to Schedule Maxing") == 1
        assert subjects.count("Your Schedule Maxing password was changed") == 2  # the change and the reset
        assert subjects.count("Two-step sign-in was turned on") == 1
        everything_mailed = "\n".join(message.text for message in mail.sent)
        for secret in (PASSWORD, NEW_PASSWORD, FINAL_PASSWORD, secret, *codes):
            assert secret not in everything_mailed


def _session_of(app, refresh_token: str) -> str:
    import hashlib

    with app.state.session_factory() as session:
        return str(session.scalar(select(models.RefreshCredential.session_id).where(
            models.RefreshCredential.token_hash == hashlib.sha256(refresh_token.encode()).hexdigest())))
