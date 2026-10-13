"""
Signup email verification and transactional email (docs/email.md): address validation, the pending
registration that holds no credentials, tokens, the outbox and its retries, the provider webhook and
the operator's diagnostic. Every email goes to a FakeMail; nothing leaves the process.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import email_diagnostic, models, outbox
from backend.accounts import AccountValidationError, check_email
from backend.app import create_app
from backend.email_webhooks import sign
from backend.settings import BackendConfigError, BackendSettings
from tests.backend.conftest import PASSWORD, TEST_SECRET, FakeMail, login_headers

PUBLIC = "https://app.example.com"
WEBHOOK_SECRET = "whsec_dGVzdC1zZWNyZXQtZm9yLXdlYmhvb2tz"
GENERIC = "pending_verification"


def mail_settings(**overrides) -> BackendSettings:
    return BackendSettings(**{"database_url": "sqlite://", "jwt_secret": TEST_SECRET, "app_public_url": PUBLIC,
                              "smtp_sender": "noreply@example.com", "smtp_host": "smtp.example.com", **overrides})


@pytest.fixture
def mail() -> FakeMail:
    return FakeMail()


@pytest.fixture
def api(engine, clock, mail):
    with TestClient(create_app(mail_settings(), engine=engine, clock=clock, mail=mail)) as client:
        yield client


def ask(client, email: str, path: str = "/auth/register", **extra):
    return client.post(path, json={"email": email, **extra})


def confirm(client, token: str, password: str = PASSWORD, **extra):
    return client.post("/auth/verify/confirm", json={"token": token, "password": password, **extra})


def count(client, model) -> int:
    with client.app.state.session_factory() as session:
        return session.scalar(select(func.count()).select_from(model))


# -- addresses --------------------------------------------------------------------------------


@pytest.mark.parametrize("address", [
    "a@example.com,b@example.org", "a@example.com; b@example.org", "Alice <alice@example.com>",
    "alice@example.com\r\nBcc: mallory@example.org", "alice@example.com\nSubject: hi", "alice@exa\x00mple.com",
    '"alice smith"@example.com', "alice@[127.0.0.1]", "alice", "alice@", "@example.com", "alice@@example.com",
    "alice@localhost", "alice example@example.com", "<alice@example.com>", "mailto:alice@example.com",
])
def test_anything_but_one_plain_mailbox_is_refused(api, mail, address: str) -> None:
    with pytest.raises(AccountValidationError):
        check_email(address)
    response = ask(api, address)
    assert response.status_code == 422 and mail.sent == [] and count(api, models.PendingRegistration) == 0


def test_normalization_is_the_same_everywhere_and_keeps_plus_tags_and_dots(api, mail) -> None:
    assert check_email("  Alice.B+News@Example.COM ") == "alice.b+news@example.com"
    assert check_email("alice.b@example.com") != check_email("aliceb@example.com")
    assert ask(api, "Alice.B+News@Example.COM").status_code == 202
    assert confirm(api, mail.token("alice.b+news@example.com")).json()["email"] == "alice.b+news@example.com"
    assert login_headers(api, "ALICE.B+NEWS@example.com")  # sign-in finds it however it is capitalized


def test_dns_can_refuse_a_dead_domain_but_a_dns_failure_refuses_nothing(monkeypatch) -> None:
    import dns.exception
    import dns.resolver

    def answer(error):
        def resolve(self, *_args, **_kwargs):
            raise error
        return resolve

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", answer(dns.resolver.NXDOMAIN()))
    with pytest.raises(AccountValidationError, match="cannot receive mail"):
        check_email("alice@no-such-domain.example.org", check_dns=True)
    assert check_email("alice@no-such-domain.example.org") == "alice@no-such-domain.example.org"  # syntax only
    monkeypatch.setattr(dns.resolver.Resolver, "resolve", answer(dns.exception.Timeout()))
    assert check_email("alice@slow-dns.example.org", check_dns=True) == "alice@slow-dns.example.org"


# -- pending registrations ----------------------------------------------------------------------


def test_register_verify_activate_and_exactly_one_welcome(api, mail) -> None:
    answer = ask(api, "alice@example.com", password="ignored-by-the-server", display_name="Mallory")
    assert answer.status_code == 202 and answer.json()["status"] == GENERIC and "token" not in answer.text
    assert count(api, models.User) == 0  # no account, so nothing to sign in to
    assert api.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 401
    assert api.post("/auth/login", json={"email": "alice@example.com",
                                         "password": "ignored-by-the-server"}).status_code == 401
    [verification] = mail.sent
    assert verification.to == "alice@example.com" and f"{PUBLIC}/auth/verify#token=" in verification.text

    created = confirm(api, mail.token(), display_name="Alice")
    assert created.status_code == 201
    user = created.json()
    assert user["email"] == "alice@example.com" and user["email_verified"] and user["display_name"] == "Alice"
    assert [message.subject for message in mail.sent[1:]] == ["Welcome to Schedule Maxing"]
    assert api.get("/me", headers=login_headers(api, "alice@example.com")).json()["email_verified"] is True
    assert count(api, models.PendingRegistration) == 0 and count(api, models.EmailVerificationToken) == 0
    outbox.process_due(api.app.state.session_factory, mail, api.app.state.settings, api.app.state.clock)
    assert len([message for message in mail.sent if message.subject.startswith("Welcome")]) == 1


def test_the_public_answer_is_the_same_for_new_pending_and_existing_addresses(api, mail) -> None:
    new = ask(api, "new@example.com")
    pending = ask(api, "new@example.com")
    confirm(api, mail.token("new@example.com"))
    existing = ask(api, "new@example.com")
    assert new.status_code == pending.status_code == existing.status_code == 202
    assert new.json() == pending.json() == existing.json()
    notice = mail.to("new@example.com")[-1]
    assert "already" in notice.subject and "#token=" not in notice.text  # the owner is told; nothing changes
    assert count(api, models.User) == 1 and count(api, models.PendingRegistration) == 0


def test_an_attacker_who_registers_a_victims_address_gains_nothing(api, mail) -> None:
    # The attacker asks first, with a password of their choosing; the victim receives the email.
    ask(api, "victim@example.com", password="the attacker's password", username="attacker")
    assert api.post("/auth/login", json={"email": "victim@example.com",
                                         "password": "the attacker's password"}).status_code == 401
    # The victim confirms and chooses their own password: the attacker's never existed anywhere.
    assert confirm(api, mail.token("victim@example.com"), "the victim's own password").status_code == 201
    assert api.post("/auth/login", json={"email": "victim@example.com",
                                         "password": "the attacker's password"}).status_code == 401
    assert login_headers(api, "victim@example.com", "the victim's own password")
    with api.app.state.session_factory() as session:
        assert session.scalar(select(models.User.username)) is None


def test_a_duplicate_request_never_touches_an_active_account(api, mail) -> None:
    ask(api, "alice@example.com")
    confirm(api, mail.token())
    with api.app.state.session_factory() as session:
        before = session.execute(select(models.User.password_hash, models.User.credential_epoch,
                                        models.User.version)).one()
    for _ in range(3):
        ask(api, "alice@example.com", password="another password entirely")
    with api.app.state.session_factory() as session:
        assert session.execute(select(models.User.password_hash, models.User.credential_epoch,
                                      models.User.version)).one() == before
    assert login_headers(api, "alice@example.com")


def test_a_pending_registration_expires_and_reserves_nothing(api, mail, clock) -> None:
    ask(api, "alice@example.com")
    stale = mail.token()
    clock.advance(hours=49)
    assert confirm(api, stale).status_code == 400 and count(api, models.User) == 0
    assert ask(api, "alice@example.com").status_code == 202  # anyone may ask again
    assert confirm(api, mail.token()).status_code == 201


# -- tokens -------------------------------------------------------------------------------------


def test_tokens_expire_are_single_use_and_purpose_bound(api, mail, clock) -> None:
    ask(api, "alice@example.com")
    token = mail.token()
    with api.app.state.session_factory() as session:
        stored = session.scalars(select(models.EmailVerificationToken)).one()
        assert token not in (stored.token_hash, stored.email) and len(stored.token_hash) == 64
    refused = confirm(api, "not-the-token")
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_verification_token"
    assert api.post("/auth/recovery/reset", json={"token": token, "new_password": PASSWORD}).status_code == 400
    assert confirm(api, token).status_code == 201
    assert confirm(api, token).status_code == 400  # replayed
    ask(api, "bob@example.com")
    late = mail.token("bob@example.com")
    clock.advance(hours=25)
    assert confirm(api, late).status_code == 400  # the token's own lifetime (24 h) is over


def test_a_bad_password_leaves_the_token_usable(api, mail) -> None:
    ask(api, "alice@example.com")
    assert confirm(api, mail.token(), "short").status_code == 422
    assert confirm(api, mail.token()).status_code == 201


def test_a_scanner_opening_the_link_changes_nothing(api, mail) -> None:
    ask(api, "alice@example.com")
    token = mail.token()
    for _ in range(3):  # a mail scanner or link preview: GET (and HEAD) on the emailed URL
        page = api.get("/auth/verify")
        assert page.status_code == 200 and "<form" in page.text and token not in page.text
        assert page.headers["cache-control"] == "no-store" and "frame-ancestors 'none'" in page.headers[
            "content-security-policy"]
        api.get("/auth/verify", params={"token": token})
    assert api.get("/auth/verify/confirm").status_code == 405
    assert count(api, models.User) == 0
    assert confirm(api, token).status_code == 201  # only the explicit submission creates the account


def test_concurrent_confirmations_create_one_account(tmp_path, clock) -> None:
    from backend.database import create_backend_engine, session_factory
    from backend.migrate import upgrade
    from backend.verification import VerificationService

    engine = create_backend_engine(f"sqlite:///{tmp_path / 'race.db'}")  # real connections, unlike the shared one
    upgrade(engine)
    factory = session_factory(engine)
    with factory() as session:
        service = VerificationService(session, clock)
        service.start_registration("alice@example.com", send=False)
        token = service.mint_registration_token("alice@example.com")
    results: list[str] = []
    barrier = threading.Barrier(4)

    def attempt(index: int) -> None:
        with factory() as session:
            barrier.wait()
            try:
                VerificationService(session, clock).confirm_registration(token, f"a long passphrase {index}")
                results.append("ok")
            except Exception as error:  # noqa: BLE001 - the losers' failure is the point
                results.append(type(error).__name__)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(models.User)) == 1
        assert session.scalar(select(func.count()).select_from(models.EmailOutbox)
                              .where(models.EmailOutbox.kind == outbox.WELCOME)) == 1
    engine.dispose()


def test_resends_keep_earlier_links_valid_within_a_bound_and_verification_ends_them_all(api, mail, clock) -> None:
    ask(api, "alice@example.com")
    first = mail.token()
    for _ in range(2):
        assert ask(api, "alice@example.com", "/auth/verify/resend").status_code == 202
    assert len(mail.sent) == 3 and len({message.text for message in mail.sent}) == 3
    newest = mail.token()
    assert confirm(api, first).status_code == 201  # an earlier link still works
    assert confirm(api, newest).status_code == 400  # and using one ends the others
    # The bound: more than MAX_OUTSTANDING_TOKENS emails revoke the oldest links.
    ask(api, "bob@example.com")
    oldest = mail.token("bob@example.com")
    for _ in range(5):
        clock.advance(seconds=1)
        ask(api, "bob@example.com", "/auth/verify/resend")
    with api.app.state.session_factory() as session:
        live = session.scalar(select(func.count()).select_from(models.EmailVerificationToken)
                              .where(models.EmailVerificationToken.revoked_at.is_(None)))
    assert live == 5 and confirm(api, oldest).status_code == 400
    assert confirm(api, mail.token("bob@example.com")).status_code == 201


def test_emails_are_limited_per_destination_with_a_cooldown_and_for_the_whole_service(engine, clock, mail) -> None:
    from backend.rate_limit import LIMITS, Limit, RateLimiter

    app = create_app(mail_settings(rate_limit_enabled=True), engine=engine, clock=clock, mail=mail)
    with TestClient(app) as client:
        assert ask(client, "alice@example.com").status_code == 202
        again = ask(client, "alice@example.com", "/auth/verify/resend")  # inside the cooldown
        assert again.status_code == 202 and again.json()["status"] == GENERIC and len(mail.sent) == 1
        for _ in range(5):
            clock.advance(seconds=61)
            assert ask(client, "alice@example.com", "/auth/verify/resend").status_code == 202
        assert len(mail.to("alice@example.com")) == 4  # the destination's hourly limit; the answer never changed
        assert ask(client, "bob@example.com").status_code == 202 and len(mail.to("bob@example.com")) == 1
        # The source is limited too -- and says so, since that reveals nothing about any address.
        statuses = [ask(client, f"user{index}@example.com").status_code for index in range(6)]
        assert statuses[-1] == 429
        # A service-wide ceiling: once reached, nothing more is queued for anyone.
        app.state.rate_limiter = RateLimiter(app.state.session_factory, clock, limits={
            **LIMITS, "register_ip": Limit(1000, 3600), "email_global": Limit(2, 3600)})
        clock.advance(hours=3)
        before = len(mail.sent)
        assert [ask(client, f"wave{index}@example.com").status_code for index in range(5)] == [202] * 5
        assert len(mail.sent) - before == 2


# -- existing accounts --------------------------------------------------------------------------


def legacy_account(client, email: str = "old@example.com") -> dict:
    """An account from before verification: created with a password, its address never proved."""
    from backend.accounts import AccountService

    with client.app.state.session_factory() as session:
        AccountService(session, client.app.state.clock).register(email=email, password=PASSWORD)
    return login_headers(client, email)


def test_an_existing_account_is_not_declared_verified_and_keeps_working(api, mail) -> None:
    headers = legacy_account(api)
    assert api.get("/me", headers=headers).json()["email_verified"] is False
    assert api.get("/tasks", headers=headers).status_code == 200  # nothing is taken away
    assert api.post("/auth/email/verification/request", headers=headers).status_code == 202
    code = mail.sent[-1].text.split("\n\n")[2].strip()
    # The mailbox owner alone (no session) cannot bless credentials somebody else chose for the address.
    assert api.post("/auth/email/verification/confirm", json={"token": code}).status_code == 401
    assert confirm(api, code).status_code == 400  # nor is it a registration token
    other = legacy_account(api, "other@example.com")
    assert api.post("/auth/email/verification/confirm", json={"token": code}, headers=other).status_code == 400
    verified = api.post("/auth/email/verification/confirm", json={"token": code}, headers=headers)
    assert verified.status_code == 200 and verified.json()["email_verified"] is True
    assert api.post("/auth/email/verification/confirm", json={"token": code}, headers=headers).status_code == 400
    assert not [message for message in mail.sent if message.subject.startswith("Welcome")]


def test_unverified_accounts_can_be_kept_from_records_by_configuration(engine, clock, mail) -> None:
    app = create_app(mail_settings(require_verified_email=True), engine=engine, clock=clock, mail=mail)
    with TestClient(app) as client:
        headers = legacy_account(client)
        blocked = client.get("/tasks", headers=headers)
        assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "email_unverified"
        assert client.get("/me", headers=headers).status_code == 200  # enough to see and fix it
        client.post("/auth/email/verification/request", headers=headers)
        code = mail.sent[-1].text.split("\n\n")[2].strip()
        assert client.post("/auth/email/verification/confirm", json={"token": code}, headers=headers).status_code == 200
        assert client.get("/tasks", headers=headers).status_code == 200


# -- the outbox ---------------------------------------------------------------------------------


def rows(client) -> list[models.EmailOutbox]:
    with client.app.state.session_factory() as session:
        return list(session.scalars(select(models.EmailOutbox).order_by(models.EmailOutbox.created_at)))


def drain(client, mail) -> int:
    state = client.app.state
    return outbox.process_due(state.session_factory, mail, state.settings, state.clock)


def test_a_provider_outage_is_retried_with_backoff_and_never_bypasses_verification(api, mail, clock) -> None:
    mail.fail = True
    assert ask(api, "alice@example.com").status_code == 202  # the answer does not depend on the provider
    [row] = rows(api)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "TimeoutError") and row.accepted_at is None
    assert count(api, models.User) == 0
    assert drain(api, mail) == 0 and rows(api)[0].attempts == 1  # not due yet: no hammering
    clock.advance(seconds=61)
    assert drain(api, mail) == 0 and rows(api)[0].attempts == 2
    mail.fail = False
    clock.advance(minutes=6)
    assert drain(api, mail) == 1
    [row] = rows(api)
    assert row.status == "sent" and row.accepted_at is not None and row.provider_message_id == "fake-1"
    assert row.delivered_at is None  # accepted is not delivered
    assert confirm(api, mail.token()).status_code == 201


def test_retries_are_bounded_and_the_queue_holds_no_token(api, mail, clock) -> None:
    mail.fail = True
    ask(api, "alice@example.com")
    for _ in range(10):
        clock.advance(hours=5)
        drain(api, mail)
    [row] = rows(api)
    assert row.status == "dead" and row.attempts == outbox.MAX_ATTEMPTS
    stored = {column.name: getattr(row, column.name) for column in models.EmailOutbox.__table__.columns}
    assert not any(isinstance(value, str) and ("token" in value or "http" in value) for value in stored.values())
    assert count(api, models.User) == 0


def test_an_interrupted_send_is_taken_over_after_its_lock_and_never_sent_twice_at_once(api, mail, clock) -> None:
    mail.fail = True
    ask(api, "alice@example.com")
    mail.fail = False
    state = api.app.state
    with state.session_factory() as session:  # a worker that claimed the row and died
        row = session.scalars(select(models.EmailOutbox)).one()
        row.status, row.locked_until = "sending", clock() + timedelta(seconds=outbox.LOCK_SECONDS)
        session.commit()
    assert drain(api, mail) == 0 and mail.sent == []  # locked: nobody else sends it meanwhile
    clock.advance(seconds=outbox.LOCK_SECONDS + 1)
    assert drain(api, mail) == 1 and drain(api, mail) == 0 and len(mail.sent) == 1


def test_an_email_about_something_that_is_gone_is_dropped(api, mail, clock) -> None:
    mail.fail = True
    ask(api, "alice@example.com")
    mail.fail = False
    clock.advance(hours=49)  # the registration expired before the provider recovered
    assert drain(api, mail) == 0 and mail.sent == []
    assert (rows(api)[0].status, rows(api)[0].last_error) == ("dead", "obsolete")


def test_the_welcome_email_is_queued_once_whatever_is_repeated(api, mail) -> None:
    ask(api, "alice@example.com")
    user_id = confirm(api, mail.token()).json()["id"]
    with api.app.state.session_factory() as session:
        import uuid

        for _ in range(3):
            assert not outbox.enqueue(session, kind=outbox.WELCOME, to_email="alice@example.com",
                                      subject_id=uuid.UUID(user_id), dedupe_key=f"welcome:{user_id}",
                                      now=api.app.state.clock())
        session.commit()
    drain(api, mail)
    assert len([message for message in mail.sent if message.subject.startswith("Welcome")]) == 1


def test_links_come_from_the_configured_origin_never_the_host_header(api, mail) -> None:
    api.post("/auth/register", json={"email": "alice@example.com"},
             headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
    assert "evil.example" not in mail.sent[0].text and f"{PUBLIC}/auth/verify#token=" in mail.sent[0].text
    for bad in ("http://app.example.com", "https://app.example.com/path", "https://user:pw@app.example.com",
                "https://app.example.com?x=1"):
        with pytest.raises(BackendConfigError):
            mail_settings(app_public_url=bad)


def test_registration_is_unavailable_alike_for_every_address_without_email(client) -> None:
    register_new = client.post("/auth/register", json={"email": "new@example.com"})
    assert register_new.status_code == 503 and register_new.json()["error"]["code"] == "email_unavailable"


def test_nothing_secret_is_logged(api, mail, caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    mail.fail = True
    ask(api, "alice@example.com")
    mail.fail = False
    api.app.state.clock.advance(seconds=61)
    drain(api, mail)
    token = mail.token()
    confirm(api, token)
    assert token not in caplog.text and "alice@example.com" not in caplog.text and PASSWORD not in caplog.text


# -- the provider webhook -----------------------------------------------------------------------


def webhook(client, event: dict, *, event_id: str = "msg_1", secret: str = WEBHOOK_SECRET, timestamp: int | None = None,
            tamper: bool = False, prefix: str = "webhook"):
    body = json.dumps(event).encode()
    stamp = str(int(client.app.state.clock().timestamp()) if timestamp is None else timestamp)
    signature = sign(secret, event_id, stamp, body)
    return client.post("/webhooks/email", content=body + (b" " if tamper else b""), headers={
        f"{prefix}-id": event_id, f"{prefix}-timestamp": stamp, f"{prefix}-signature": signature,
        "Content-Type": "application/json"})


def test_webhooks_are_authenticated_replay_safe_and_the_only_source_of_delivery(engine, clock, mail) -> None:
    app = create_app(mail_settings(email_webhook_secret=WEBHOOK_SECRET), engine=engine, clock=clock, mail=mail)
    with TestClient(app) as client:
        ask(client, "alice@example.com")
        delivered = {"type": "email.delivered", "data": {"email_id": "fake-1"}}
        assert rows(client)[0].accepted_at is not None and rows(client)[0].delivered_at is None

        assert webhook(client, delivered, secret="whsec_d3Jvbmctc2VjcmV0").status_code == 401  # forged
        assert webhook(client, delivered, tamper=True).status_code == 401  # altered body
        assert webhook(client, delivered, timestamp=int(clock().timestamp()) - 3600).status_code == 401  # stale
        assert client.post("/webhooks/email", json=delivered).status_code == 401  # unsigned
        assert rows(client)[0].delivered_at is None

        assert webhook(client, delivered, prefix="svix").json() == {"status": "ok"}
        first = rows(client)[0].delivered_at
        assert first is not None and rows(client)[0].bounced_at is None
        clock.advance(seconds=30)
        assert webhook(client, delivered, prefix="svix").json() == {"status": "duplicate"}  # replayed
        assert rows(client)[0].delivered_at == first
        assert webhook(client, {"type": "email.bounced", "data": {"email_id": "fake-1"}},
                       event_id="msg_2").status_code == 200
        assert rows(client)[0].bounced_at is not None
        assert webhook(client, {"type": "email.delivered", "data": {"email_id": "unknown"}},
                       event_id="msg_3").status_code == 200  # acknowledged, nothing to update


def test_the_webhook_does_not_exist_without_a_secret(api) -> None:
    assert webhook(api, {"type": "email.delivered", "data": {"email_id": "fake-1"}}).status_code == 404


# -- the operator's diagnostic ------------------------------------------------------------------


def test_the_diagnostic_mails_only_the_configured_recipient_and_never_claims_delivery(api, mail) -> None:
    lines: list[str] = []
    factory = api.app.state.session_factory
    assert email_diagnostic.send(mail_settings(), adapter=mail, factory=factory, out=lines.append) == 2
    assert "configuration: FAILED" in lines[0] and mail.sent == []  # no recipient configured: nothing is sent

    lines.clear()
    settings = mail_settings(email_diagnostic_recipient="ops@example.com")
    assert email_diagnostic.send(settings, adapter=mail, factory=factory, out=lines.append) == 0
    [message] = mail.sent
    assert message.to == "ops@example.com"
    report = "\n".join(lines)
    assert "configuration: passed" in report and "accepted by the provider: yes" in report
    assert "none recorded" in report and "NOT VERIFIED" in report
    assert "delivered" not in report.replace("delivery", "") and "ops@example.com" not in report
    row_id = str(rows(api)[0].id)

    lines.clear()
    assert email_diagnostic.status(row_id, factory, out=lines.append) == 0
    assert "none recorded" in "\n".join(lines) and "NOT VERIFIED" in "\n".join(lines)

    lines.clear()
    mail.fail = True
    assert email_diagnostic.send(settings, adapter=mail, factory=factory, out=lines.append) == 1
    assert "accepted by the provider: NO" in "\n".join(lines)


def test_the_brevo_adapter_sends_over_https_with_the_key_in_a_header_and_reports_failures_by_class(monkeypatch) -> None:
    import io
    import urllib.error

    from backend import mail as mail_module
    from backend.mail import BrevoMail, MailError, OutboundEmail, mail_for

    settings = mail_settings(email_provider="brevo", brevo_api_key="xkeysib-test", smtp_host="",
                             smtp_sender="Schedule Maxing <sender@example.com>")
    assert isinstance(mail_for(settings), BrevoMail)
    assert mail_for(mail_settings(email_provider="brevo", smtp_host="")) is None  # no key: not configured
    sent = {}

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def accept(request, timeout):
        sent.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data))
        return Answer(b'{"messageId": "<abc@smtp-relay.mailin.fr>"}')

    monkeypatch.setattr(mail_module.urllib.request, "urlopen", accept)
    message = OutboundEmail(to="alice@example.com", subject="Hello", text="Body", idempotency_key="k")
    assert BrevoMail(settings).deliver(message) == "<abc@smtp-relay.mailin.fr>"
    assert sent["url"] == "https://api.brevo.com/v3/smtp/email" and sent["headers"]["Api-key"] == "xkeysib-test"
    assert sent["body"] == {"sender": {"email": "sender@example.com", "name": "Schedule Maxing"},
                            "to": [{"email": "alice@example.com"}], "subject": "Hello", "textContent": "Body"}

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"message": "Key not found"}'))

    monkeypatch.setattr(mail_module.urllib.request, "urlopen", refuse)
    with pytest.raises(MailError, match="HTTP 401") as refused:
        BrevoMail(settings).deliver(message)
    assert "xkeysib" not in str(refused.value) and "Key not found" not in str(refused.value)


def test_the_mailjet_adapter_uses_basic_auth_over_https_and_a_refused_message_is_not_accepted(monkeypatch) -> None:
    import base64
    import io

    from backend import mail as mail_module
    from backend.mail import MailError, MailjetMail, OutboundEmail, mail_for

    settings = mail_settings(email_provider="mailjet", mailjet_api_key="public-key", mailjet_secret_key="secret-key",
                             smtp_host="", smtp_sender="Schedule Maxing <sender@example.com>")
    assert isinstance(mail_for(settings), MailjetMail)
    assert mail_for(mail_settings(email_provider="mailjet", mailjet_api_key="only-one", smtp_host="")) is None
    sent, answer = {}, {"Messages": [{"Status": "success", "To": [{"Email": "alice@example.com", "MessageID": 4711}]}]}

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def respond(request, timeout):
        sent.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data))
        return Answer(json.dumps(answer).encode())

    monkeypatch.setattr(mail_module.urllib.request, "urlopen", respond)
    message = OutboundEmail(to="alice@example.com", subject="Hello", text="Body", idempotency_key="row-1")
    assert MailjetMail(settings).deliver(message) == "4711"
    assert sent["url"] == "https://api.mailjet.com/v3.1/send"
    assert sent["headers"]["Authorization"] == "Basic " + base64.b64encode(b"public-key:secret-key").decode()
    assert sent["body"] == {"Messages": [{
        "From": {"Email": "sender@example.com", "Name": "Schedule Maxing"}, "To": [{"Email": "alice@example.com"}],
        "Subject": "Hello", "TextPart": "Body", "CustomID": "row-1"}]}
    answer["Messages"][0]["Status"] = "error"  # e.g. the sender address was never validated
    with pytest.raises(MailError, match="refused"):
        MailjetMail(settings).deliver(message)

