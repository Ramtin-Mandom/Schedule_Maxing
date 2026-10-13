"""
Account security (docs/account-security.md): the password policy, recovery under abuse, password change,
sessions and their revocation, refresh rotation, two-step sign-in and security notifications.
"""

from __future__ import annotations

import uuid

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import models, outbox, password_policy
from backend.accounts import AccountService, AccountValidationError, check_password
from backend.app import create_app
from backend.password_policy import HibpRangeChecker, WeakPasswordError, check_new_password
from backend.security import issue_access_token, issue_mfa_token, verify_access_identity, verify_mfa_token
from backend.settings import BackendConfigError, BackendSettings
from tests.backend.conftest import PASSWORD, TEST_SECRET, FakeMail, mint_verification_token, register

NEW_PASSWORD = "an entirely different passphrase"
MFA_KEY = Fernet.generate_key().decode()


def secure_settings(**overrides) -> BackendSettings:
    return BackendSettings(**{"database_url": "sqlite://", "jwt_secret": TEST_SECRET, "mfa_encryption_key": MFA_KEY,
                              "app_public_url": "https://app.example.com", "smtp_sender": "noreply@example.com",
                              "smtp_host": "smtp.example.com", **overrides})


@pytest.fixture
def mail() -> FakeMail:
    return FakeMail()


@pytest.fixture
def api(engine, clock, mail):
    with TestClient(create_app(secure_settings(), engine=engine, clock=clock, mail=mail)) as client:
        yield client


def login(client, email: str = "alice@example.com", password: str = PASSWORD, **headers):
    return client.post("/auth/login", json={"email": email, "password": password}, headers=headers)


def bearer(pair: dict) -> dict:
    return {"Authorization": f"Bearer {pair['access_token']}"}


def account(client, email: str = "alice@example.com") -> dict:
    register(client, email)
    return bearer(login(client, email).json())


def drain(client, mail) -> None:
    state = client.app.state
    outbox.process_due(state.session_factory, mail, state.settings, state.clock)


def subjects(mail, address: str = "alice@example.com") -> list[str]:
    return [message.subject for message in mail.to(address)]


def enable_mfa(client, headers: dict, clock) -> tuple[pyotp.TOTP, list[str]]:
    secret = client.post("/auth/mfa/totp/enroll", json={"current_password": PASSWORD}, headers=headers).json()["secret"]
    totp = pyotp.TOTP(secret)
    confirmed = client.post("/auth/mfa/totp/confirm", headers=headers,
                            json={"current_password": PASSWORD, "code": totp.at(clock().timestamp())})
    assert confirmed.status_code == 200, confirmed.text
    clock.advance(seconds=30)  # the confirming code's step is spent
    return totp, confirmed.json()["recovery_codes"]


# -- the password policy ------------------------------------------------------------------------


WEAK = ["short", "seven77", "password123", "Qwerty123", "12345678", "iloveyou",
        "a" * 20, "abcabcabcabcabcabc", "12121212121212121212", "abcdefghijklmnopqrstuvwxyz",
        "0123456789012345", "qwertyuiopasdfghjkl", "PasswordPassword", "correct horse battery staple",
        "x" * 1025]
STRONG = ["correct horse battery", "plum7otter", "plum-otter visits 7 quiet lakes", "пароль из нескольких слов",
          "🦊🦊 foxes jump over 9 lazy dogs",
          "  leading and trailing spaces  ", "Tr0ub4dor&3 is not required here", "y" * 5 + "Zq8#" * 4]


@pytest.mark.parametrize("password", WEAK)
def test_weak_new_passwords_are_refused_without_being_echoed(password: str) -> None:
    with pytest.raises(WeakPasswordError) as refused:
        check_new_password(password)
    assert password not in str(refused.value)


@pytest.mark.parametrize("password", STRONG)
def test_long_passphrases_unicode_and_whitespace_are_accepted_without_composition_rules(password: str) -> None:
    check_new_password(password)


def test_the_accounts_own_identifiers_are_not_a_password() -> None:
    with pytest.raises(AccountValidationError):
        check_password("alice.liddell@example.com", email="alice.liddell@example.com")
    with pytest.raises(AccountValidationError):
        check_password("alice_in_wonderland", username="alice_in_wonderland")
    check_password("alice.liddell@example.com and three more words", email="alice.liddell@example.com")


def test_breached_passwords_are_found_by_k_anonymity_and_an_outage_refuses_nothing(monkeypatch) -> None:
    import hashlib

    breached = "this passphrase was in a breach"
    digest = hashlib.sha1(breached.encode()).hexdigest().upper()  # noqa: S324 - the range API's digest
    asked: list[str] = []

    def listing(prefix: str) -> str:
        asked.append(prefix)
        return f"0000000000000000000000000000000000A:3\r\n{digest[5:]}:42\r\n{'F' * 35}:0\r\n"

    monkeypatch.setattr(password_policy, "_breach_checker", HibpRangeChecker(fetch=listing))
    with pytest.raises(WeakPasswordError, match="known data breaches"):
        check_new_password(breached)
    check_new_password("a passphrase nobody has leaked yet")
    assert asked[0] == digest[:5] and all(len(prefix) == 5 for prefix in asked)  # only 5 hex digits ever leave
    assert breached not in "".join(asked) and digest not in "".join(asked)

    def outage(_prefix: str) -> str:
        raise OSError("unreachable")

    monkeypatch.setattr(password_policy, "_breach_checker", HibpRangeChecker(fetch=outage))
    check_new_password(breached)  # could not be checked: not refused


def test_one_policy_for_signup_reset_and_change_and_none_for_signing_in(api, mail, clock) -> None:
    # An account from before the policy, with a password that is too short today.
    with api.app.state.session_factory() as session:
        now = clock()
        from backend.passwords import hash_password

        session.add(models.User(id=uuid.uuid4(), email="old@example.com", password_hash=hash_password("old-pass"),
                                change_seq=0, created_at=now, updated_at=now, version=1))
        session.commit()
    old = login(api, "old@example.com", "old-pass")
    assert old.status_code == 200  # existing users keep signing in
    for weak in ("seven77", "PasswordPassword", "a" * 20):
        token = mint_verification_token(api.app.state.session_factory, "new@example.com", clock)
        signup = api.post("/auth/verify/confirm", json={"token": token, "password": weak})
        change = api.post("/auth/password/change", headers=bearer(old.json()),
                          json={"current_password": "old-pass", "new_password": weak})
        api.post("/auth/recovery/request", json={"identifier": "old@example.com"})
        reset = api.post("/auth/recovery/reset", json={"token": mail.token("old@example.com"), "new_password": weak})
        assert signup.status_code == change.status_code == reset.status_code == 422, weak
        assert weak not in signup.text + change.text + reset.text
    assert login(api, "old@example.com", "old-pass").status_code == 200  # nothing was changed by the refusals


# -- recovery under abuse -----------------------------------------------------------------------


def test_an_attackers_requests_neither_cancel_the_owners_link_nor_block_recovery(engine, clock, mail) -> None:
    app = create_app(secure_settings(rate_limit_enabled=True, trusted_proxies=("10.0.0.0/8",)), engine=engine,
                     clock=clock, mail=mail)
    with TestClient(app, client=("10.0.0.5", 5000)) as client:
        register(client, "alice@example.com", username="alice_a")

        def ask(identifier: str, address: str):
            return client.post("/auth/recovery/request", json={"identifier": identifier},
                               headers={"X-Forwarded-For": address})

        assert ask("alice@example.com", "198.51.100.1").status_code == 202  # the owner asks
        owners_link = mail.token("alice@example.com")
        for index in range(8):  # somebody else keeps asking, by email and by username, from elsewhere
            clock.advance(seconds=61)
            assert ask("alice@example.com" if index % 2 else "ALICE_A", f"203.0.113.{index}").status_code == 202
        recovery_mails = [message for message in mail.to("alice@example.com") if "Reset your" in message.subject]
        assert len(recovery_mails) == 4  # the destination's hourly email limit, whoever asked
        # The owner is not locked out: their own request is still answered, and the link they hold still works.
        assert ask("alice@example.com", "198.51.100.1").status_code == 202
        done = client.post("/auth/recovery/reset", json={"token": owners_link, "new_password": NEW_PASSWORD},
                           headers={"X-Forwarded-For": "198.51.100.1"})
        assert done.status_code == 200
        # A successful reset revokes every other outstanding link.
        assert client.post("/auth/recovery/reset", json={"token": mail.token("alice@example.com"),
                                                         "new_password": "one more passphrase to try"},
                           headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 400


def test_outstanding_recovery_links_are_bounded(api, mail, clock) -> None:
    register(api, "alice@example.com")
    tokens = []
    for _ in range(7):
        clock.advance(seconds=1)
        api.post("/auth/recovery/request", json={"identifier": "alice@example.com"})
        tokens.append(mail.token("alice@example.com"))
    with api.app.state.session_factory() as session:
        live = session.scalar(select(func.count()).select_from(models.PasswordRecoveryToken)
                              .where(models.PasswordRecoveryToken.revoked_at.is_(None)))
    assert live == 5
    assert api.post("/auth/recovery/reset", json={"token": tokens[0], "new_password": NEW_PASSWORD}).status_code == 400
    assert api.post("/auth/recovery/reset", json={"token": tokens[-1], "new_password": NEW_PASSWORD}).status_code == 200


def test_known_and_unknown_identifiers_take_the_same_path_and_never_touch_the_provider(engine, clock, mail,
                                                                                       monkeypatch) -> None:
    from backend import passwords

    app = create_app(secure_settings(rate_limit_enabled=True), engine=engine, clock=clock, mail=mail)
    with TestClient(app) as client:
        register(client, "alice@example.com")
        limiter, calls = app.state.rate_limiter, []
        real_require, real_record = limiter.require, limiter.record
        monkeypatch.setattr(limiter, "require", lambda scope, subject, **kw: (calls.append(("require", scope)),
                                                                             real_require(scope, subject, **kw))[1])
        monkeypatch.setattr(limiter, "record", lambda scope, subject: (calls.append(("record", scope)),
                                                                       real_record(scope, subject))[1])
        hashes: list[str] = []

        class Spy:
            def hash(self, *_args):
                hashes.append("hash")
                return "x"

            def verify(self, *_args):
                hashes.append("verify")
                return True

        monkeypatch.setattr(passwords, "_hasher", Spy())
        mail.fail = True  # the provider is down: the answer must not show it

        known = client.post("/auth/recovery/request", json={"identifier": "alice@example.com"})
        known_calls, calls[:] = list(calls), []
        unknown = client.post("/auth/recovery/request", json={"identifier": "nobody@example.com"})
        assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
        assert known_calls == calls and len(calls) == 6  # the same limiter work either way
        assert hashes == []  # and no password hashing on this path at all


def test_a_reset_keeps_the_second_factor_ends_every_session_and_notifies_once(api, mail, clock) -> None:
    headers = account(api)
    totp, _codes = enable_mfa(api, headers, clock)
    browser = api.post("/auth/browser/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert browser.json()["mfa_required"] and "sm_session" not in api.cookies
    api.post("/auth/recovery/request", json={"identifier": "alice@example.com"})
    assert api.post("/auth/recovery/reset", json={"token": mail.token("alice@example.com"),
                                                  "new_password": NEW_PASSWORD}).status_code == 200
    assert api.get("/me", headers=headers).status_code == 401  # the old access token: refused at once
    with api.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(models.NativeSession)
                              .where(models.NativeSession.revoked_at.is_(None))) == 0
    again = login(api, password=NEW_PASSWORD).json()
    assert again.get("mfa_required") is True and "access_token" not in again  # the reset did not remove it
    finished = api.post("/auth/mfa/verify", json={"mfa_token": again["mfa_token"], "code": totp.at(clock().timestamp())})
    assert finished.status_code == 200
    drain(api, mail)
    assert subjects(mail).count("Your Schedule Maxing password was changed") == 1


# -- password change and sessions ---------------------------------------------------------------


def test_password_change_needs_the_current_password_and_ends_every_session(api, mail) -> None:
    headers = account(api)
    other_device = bearer(login(api).json())
    wrong = api.post("/auth/password/change", headers=headers,
                     json={"current_password": "not the password", "new_password": NEW_PASSWORD})
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "reauthentication_failed"
    same = api.post("/auth/password/change", headers=headers,
                    json={"current_password": PASSWORD, "new_password": PASSWORD})
    assert same.status_code == 422
    assert api.post("/auth/password/change", json={"current_password": PASSWORD,
                                                   "new_password": NEW_PASSWORD}).status_code == 401
    changed = api.post("/auth/password/change", headers=headers,
                       json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert changed.status_code == 200 and NEW_PASSWORD not in changed.text
    # Already-issued access tokens are refused on their next request -- this device's and the other's.
    assert api.get("/me", headers=headers).status_code == 401
    assert api.get("/tasks", headers=other_device).status_code == 401
    assert login(api).status_code == 401 and login(api, password=NEW_PASSWORD).status_code == 200
    assert subjects(mail).count("Your Schedule Maxing password was changed") == 1
    assert PASSWORD not in mail.sent[-1].text and NEW_PASSWORD not in mail.sent[-1].text


def test_wrong_current_passwords_are_limited_per_account(engine, clock, mail) -> None:
    app = create_app(secure_settings(rate_limit_enabled=True), engine=engine, clock=clock, mail=mail)
    with TestClient(app) as client:
        headers = account(client)
        body = {"current_password": "a wrong guess every time", "new_password": NEW_PASSWORD}
        statuses = [client.post("/auth/password/change", headers=headers, json=body).status_code for _ in range(7)]
        assert statuses == [403] * 5 + [429] * 2
        right = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
        assert client.post("/auth/password/change", headers=headers, json=right).status_code == 429  # bounded...
        clock.advance(minutes=31)
        assert client.post("/auth/password/change", headers=headers, json=right).status_code == 200  # ...not for ever


def test_a_change_racing_a_reset_changes_the_password_once(api, clock) -> None:
    from backend import account_security
    from backend.recovery import RecoveryService

    register(api, "alice@example.com")
    factory = api.app.state.session_factory
    with factory() as session:
        user_id = session.scalar(select(models.User.id))
        token = RecoveryService(session, clock).request("alice@example.com").token
    real_check = account_security.check_password

    def reset_in_between(password, **context):  # a reset lands after the change verified the old password
        with factory() as other:
            RecoveryService(other, clock).reset(token, "the reset's passphrase wins")
        return real_check(password, **context)

    account_security.check_password = reset_in_between
    try:
        with factory() as session, pytest.raises(Exception) as void:
            account_security.change_password(session, user_id, PASSWORD, NEW_PASSWORD, clock())
    finally:
        account_security.check_password = real_check
    assert type(void.value).__name__ == "InvalidCredentialsError"
    assert login(api, password="the reset's passphrase wins").status_code == 200
    assert login(api, password=NEW_PASSWORD).status_code == 401
    with factory() as session:
        assert session.scalar(select(models.User.credential_epoch)) == 1


def test_sessions_are_listed_revoked_one_by_one_and_belong_to_their_account(api) -> None:
    register(api, "alice@example.com")
    register(api, "bob@example.com")
    desktop = login(api, **{"User-Agent": "ScheduleMaxing/1.2 (Windows)"}).json()
    laptop = login(api, **{"User-Agent": "ScheduleMaxing/1.2 (macOS)"}).json()
    bob = login(api, "bob@example.com").json()
    listed = api.get("/auth/sessions", headers=bearer(desktop)).json()["sessions"]
    assert {(item["client"], item["current"]) for item in listed} == {
        ("ScheduleMaxing/1.2 (Windows)", True), ("ScheduleMaxing/1.2 (macOS)", False)}
    assert all(set(item) == {"id", "kind", "created_at", "expires_at", "client", "current"} for item in listed)
    laptop_id = next(item["id"] for item in listed if not item["current"])

    # Another account can neither see nor end it, with its id or a guessed one.
    assert laptop_id not in str(api.get("/auth/sessions", headers=bearer(bob)).json())
    assert api.delete(f"/auth/sessions/{laptop_id}", headers=bearer(bob)).status_code == 404
    assert api.delete(f"/auth/sessions/{uuid.uuid4()}", headers=bearer(desktop)).status_code == 404
    assert api.get("/me", headers=bearer(laptop)).status_code == 200

    assert api.delete(f"/auth/sessions/{laptop_id}", headers=bearer(desktop)).status_code == 204
    assert api.get("/me", headers=bearer(laptop)).status_code == 401  # its access token: refused at once
    assert api.post("/auth/refresh", json={"refresh_token": laptop["refresh_token"]}).status_code == 401
    assert api.get("/me", headers=bearer(desktop)).status_code == 200 and api.get("/me", headers=bearer(bob)).status_code == 200


def test_sign_out_everywhere_ends_app_and_browser_sessions_at_once(engine, clock, mail) -> None:
    api = TestClient(create_app(secure_settings(browser_cookie_secure=False), engine=engine, clock=clock, mail=mail))
    headers = account(api)
    second = login(api).json()
    browser = api.post("/auth/browser/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert browser.json()["authenticated"] and api.get("/me").status_code == 200  # the cookie works
    assert len(api.get("/auth/sessions", headers=headers).json()["sessions"]) == 3
    assert api.post("/auth/sessions/revoke-all", headers=headers).status_code == 204
    assert api.get("/me", headers=headers).status_code == 401
    assert api.get("/me", headers=bearer(second)).status_code == 401
    api.headers.pop("Authorization", None)
    assert api.get("/me").status_code == 401  # the browser cookie too
    assert api.post("/auth/refresh", json={"refresh_token": second["refresh_token"]}).status_code == 401
    assert login(api).status_code == 200  # the password still signs in


# -- refresh rotation ---------------------------------------------------------------------------


def test_refresh_rotates_a_racing_twin_is_tolerated_and_replay_ends_the_session(api, clock) -> None:
    register(api, "alice@example.com")
    first = login(api).json()

    def refresh(pair):
        return api.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})

    second = refresh(first)
    assert second.status_code == 200 and second.json()["refresh_token"] != first["refresh_token"]
    # The same client's other request, a moment later with the credential that was just exchanged.
    twin = refresh(first)
    assert twin.status_code == 409 and twin.json()["error"]["code"] == "refresh_superseded"
    assert "access_token" not in twin.text  # it gets no credential...
    assert api.get("/me", headers=bearer(second.json())).status_code == 200  # ...and the session lives on
    third = refresh(second.json())
    assert third.status_code == 200
    # Long after: the first credential again is replay. The whole session ends, for whoever holds it.
    clock.advance(seconds=60)
    assert refresh(first).status_code == 401
    assert refresh(third.json()).status_code == 401
    assert api.get("/me", headers=bearer(third.json())).status_code == 401


# -- two-step sign-in ---------------------------------------------------------------------------


def test_enrollment_needs_the_password_and_a_working_code_before_it_counts(api, mail, clock) -> None:
    headers = account(api)
    assert api.get("/auth/mfa", headers=headers).json() == {"available": True, "enabled": False, "recovery_codes_left": 0}
    assert api.post("/auth/mfa/totp/enroll", json={"current_password": "wrong"}, headers=headers).status_code == 403
    assert api.post("/auth/mfa/totp/enroll", json={"current_password": PASSWORD}).status_code == 401
    enrollment = api.post("/auth/mfa/totp/enroll", json={"current_password": PASSWORD}, headers=headers).json()
    assert enrollment["otpauth_uri"].startswith("otpauth://totp/Schedule%20Maxing:alice%40example.com?secret=")
    with api.app.state.session_factory() as session:
        stored = session.scalars(select(models.MfaTotp)).one()
        assert enrollment["secret"] not in stored.secret_encrypted and stored.confirmed_at is None  # encrypted at rest
    assert "access_token" in login(api).json()  # an unconfirmed setup changes nothing about signing in

    totp = pyotp.TOTP(enrollment["secret"])
    bad = api.post("/auth/mfa/totp/confirm", headers=headers, json={"current_password": PASSWORD, "code": "000000"})
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "invalid_mfa_code"
    assert api.post("/auth/mfa/totp/confirm", headers=headers,
                    json={"current_password": "wrong", "code": totp.at(clock().timestamp())}).status_code == 403
    done = api.post("/auth/mfa/totp/confirm", headers=headers,
                    json={"current_password": PASSWORD, "code": totp.at(clock().timestamp())})
    codes = done.json()["recovery_codes"]
    assert done.status_code == 200 and len(codes) == len(set(codes)) == 10
    with api.app.state.session_factory() as session:
        hashes = set(session.scalars(select(models.MfaRecoveryCode.code_hash)))
        assert len(hashes) == 10 and not hashes & set(codes)  # stored hashed
    assert api.get("/auth/mfa", headers=headers).json() == {"available": True, "enabled": True, "recovery_codes_left": 10}
    assert "Two-step sign-in was turned on" in subjects(mail)
    assert enrollment["secret"] not in "".join(message.text for message in mail.sent)


def test_a_password_alone_gets_no_session_and_tokens_are_not_interchangeable(api, clock) -> None:
    headers = account(api)
    totp, _codes = enable_mfa(api, headers, clock)
    challenge = login(api).json()
    assert set(challenge) == {"mfa_required", "mfa_token", "expires_in"}  # no access or refresh credential
    mfa_token = challenge["mfa_token"]
    as_access = {"Authorization": f"Bearer {mfa_token}"}
    assert api.get("/me", headers=as_access).status_code == 401 and api.get("/tasks", headers=as_access).status_code == 401
    assert api.post("/auth/refresh", json={"refresh_token": mfa_token[:43]}).status_code == 401

    settings, now = api.app.state.settings, clock()
    user_id = uuid.UUID(api.get("/me", headers=headers).json()["id"])
    with pytest.raises(Exception):
        verify_access_identity(mfa_token, settings, now)
    access = issue_access_token(user_id, settings, now).token
    with pytest.raises(Exception):
        verify_mfa_token(access, settings, now)
    code = totp.at(clock().timestamp())
    assert api.post("/auth/mfa/verify", json={"mfa_token": access, "code": code}).status_code == 401
    forged = issue_mfa_token(user_id, secure_settings(jwt_secret="another-secret-" + "y" * 32), now, 0)
    assert api.post("/auth/mfa/verify", json={"mfa_token": forged, "code": code}).status_code == 401

    wrong = api.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "123456"})
    assert wrong.status_code == 401 and "access_token" not in wrong.text
    passed = api.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code})
    assert passed.status_code == 200 and api.get("/me", headers=bearer(passed.json())).status_code == 200
    stale = login(api).json()["mfa_token"]
    clock.advance(minutes=6)
    assert api.post("/auth/mfa/verify", json={"mfa_token": stale,
                                              "code": totp.at(clock().timestamp())}).status_code == 401  # too late


def test_a_code_is_never_accepted_twice_and_recovery_codes_work_once(api, clock) -> None:
    headers = account(api)
    totp, codes = enable_mfa(api, headers, clock)
    code = totp.at(clock().timestamp())

    def second_step(value: str):
        return api.post("/auth/mfa/verify", json={"mfa_token": login(api).json()["mfa_token"], "code": value})

    assert second_step(code).status_code == 200
    assert second_step(code).status_code == 401  # replayed within its validity
    earlier = totp.at(clock().timestamp() - 30)
    assert second_step(earlier).status_code == 401  # nor an older step than the newest accepted
    assert second_step(codes[0]).status_code == 200  # a recovery code instead of the authenticator
    assert second_step(codes[0]).status_code == 401  # ...once
    assert second_step(codes[0].upper().replace("-", " ")).status_code == 401
    assert api.get("/auth/mfa", headers=headers).json()["recovery_codes_left"] == 9
    clock.advance(seconds=30)
    assert second_step(totp.at(clock().timestamp())).status_code == 200


def test_second_step_guesses_are_limited_per_account(engine, clock, mail) -> None:
    app = create_app(secure_settings(rate_limit_enabled=True), engine=engine, clock=clock, mail=mail)
    with TestClient(app) as client:
        headers = account(client)
        totp, _codes = enable_mfa(client, headers, clock)
        token = login(client).json()["mfa_token"]
        guesses = [client.post("/auth/mfa/verify", json={"mfa_token": token, "code": f"{index:06d}"}).status_code
                   for index in range(7)]
        assert guesses == [401] * 5 + [429] * 2
        right = client.post("/auth/mfa/verify", json={"mfa_token": token, "code": totp.at(clock().timestamp())})
        assert right.status_code == 429  # refused before the code is even looked at


def test_removing_the_factor_needs_password_and_code_and_browser_sign_in_takes_two_steps(api, mail, clock) -> None:
    headers = account(api)
    totp, codes = enable_mfa(api, headers, clock)
    first = api.post("/auth/browser/login", json={"email": "alice@example.com", "password": PASSWORD}).json()
    assert first["authenticated"] is False and first["mfa_required"] and "sm_session" not in api.cookies
    done = api.post("/auth/browser/mfa", json={"mfa_token": first["mfa_token"], "code": totp.at(clock().timestamp())})
    assert done.status_code == 200 and done.json()["authenticated"] and "sm_session" in api.cookies
    api.cookies.clear()
    clock.advance(seconds=30)

    current = totp.at(clock().timestamp())
    assert api.post("/auth/mfa/totp/disable", headers=headers,
                    json={"current_password": "wrong", "code": current}).status_code == 403
    assert api.post("/auth/mfa/totp/disable", headers=headers,
                    json={"current_password": PASSWORD, "code": "000000"}).status_code == 403
    assert api.get("/auth/mfa", headers=headers).json()["enabled"] is True
    fresh = api.post("/auth/mfa/recovery-codes", headers=headers, json={"current_password": PASSWORD, "code": current})
    assert fresh.status_code == 200 and not set(fresh.json()["recovery_codes"]) & set(codes)  # the old ones are gone
    clock.advance(seconds=30)
    assert api.post("/auth/mfa/totp/disable", headers=headers,
                    json={"current_password": PASSWORD, "code": totp.at(clock().timestamp())}).status_code == 204
    assert "access_token" in login(api).json()
    assert "Two-step sign-in was turned off" in subjects(mail)


def test_without_the_encryption_key_mfa_cannot_be_set_up_and_never_fails_open(engine, clock, mail) -> None:
    with TestClient(create_app(secure_settings(), engine=engine, clock=clock, mail=mail)) as client:
        headers = account(client)
        totp, _codes = enable_mfa(client, headers, clock)
    keyless = create_app(secure_settings(mfa_encryption_key=""), engine=engine, clock=clock, mail=mail)
    with TestClient(keyless) as client:
        challenge = login(client).json()
        assert challenge.get("mfa_required") and "access_token" not in challenge  # still two steps
        blocked = client.post("/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"],
                                                        "code": totp.at(clock().timestamp())})
        assert blocked.status_code == 503 and "access_token" not in blocked.text
        register(client, "bob@example.com")
        bob = bearer(login(client, "bob@example.com").json())
        assert client.post("/auth/mfa/totp/enroll", json={"current_password": PASSWORD}, headers=bob).status_code == 503
    with pytest.raises(BackendConfigError):
        secure_settings(mfa_encryption_key="not-a-fernet-key")


# -- notifications ------------------------------------------------------------------------------


def test_only_a_newly_recognized_sign_in_is_notified_and_only_once(api, mail) -> None:
    register(api, "alice@example.com")
    home = {"User-Agent": "ScheduleMaxing/1.2 (Windows)"}
    for _ in range(3):
        pair = login(api, **home).json()  # the first place ever, then the same place again: not news
        api.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    for _ in range(4):
        assert login(api, password="a wrong password, repeatedly", **home).status_code == 401
    drain(api, mail)
    assert "New sign-in to your Schedule Maxing account" not in subjects(mail)
    for _ in range(3):
        login(api, **{"User-Agent": "curl/8 somewhere else"})
    drain(api, mail)
    assert subjects(mail).count("New sign-in to your Schedule Maxing account") == 1
    notice = next(message for message in mail.sent if message.subject.startswith("New sign-in"))
    assert "token" not in notice.text.lower() and "http" not in notice.text and PASSWORD not in notice.text
    with api.app.state.session_factory() as session:
        assert AccountService(session).get(session.scalar(select(models.User.id))) is not None
        stored = "".join(session.scalars(select(models.SignInFingerprint.fingerprint)))
        assert "curl" not in stored and "Windows" not in stored  # digests only
