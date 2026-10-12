"""
backend/verification_api.py

The hosted registration and email-verification endpoints (backend/verification.py has the rules):

    POST /auth/register            {email}                      -> 202, always the same answer
    POST /auth/verify/resend       {email}                      -> 202, always the same answer
    GET  /auth/verify                                           -> the page an emailed link opens
    POST /auth/verify/confirm      {token, password, ...}       -> 201 the new account
    POST /auth/email/verification/request    (signed in)        -> 202
    POST /auth/email/verification/confirm    (signed in) {token} -> 200 the account

Registering creates no account and returns no credential: the answer is one
generic message whether the address is new, already pending, already an
account, or over its email limits, and the email is sent from the outbox
after the response -- so neither the answer nor its timing says which.
Only an address that is not an email address at all is refused (422): that
is about the text entered, not about any account.

An emailed link opens GET /auth/verify, which changes nothing: mail scanners
and link previews fetch links. The page reads the token from the URL
fragment (never sent to the server), removes it from the address bar, and
only the visitor's explicit submission -- with the password they choose --
posts it. The account exists from that moment, with those credentials.

Limits (backend/rate_limit.py): requests per client address; emails per
destination, with a cooldown between two, and for the whole service --
over an email limit nothing is queued and the answer is unchanged;
confirmations per client address.

When email is not configured (APP_PUBLIC_URL, SMTP_SENDER, a provider),
registration answers 503 email_unavailable for every address alike.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import Field
from sqlalchemy.orm import Session

from backend import outbox, security_events
from backend.accounts import AccountValidationError, check_email
from backend.api import UserOut, _user_out, current_user_id, get_session, server_now, throttle
from backend.errors import ApiError
from backend.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from backend.resources import Strict
from backend.verification import MAX_TOKEN_LENGTH, InvalidVerificationTokenError, VerificationService

GENERIC_ANSWER = ("If this address can receive email, a message with the next step is on its way. "
                  "Open it to confirm the address and choose your password.")

verification = APIRouter(tags=["accounts"])


class RegisterIn(Strict):
    email: str = Field(min_length=3, max_length=320)
    # Accepted from clients that predate email verification, and ignored: the password is chosen when the
    # address is confirmed, by whoever received the email. Never stored, hashed or logged.
    password: str | None = Field(default=None, max_length=MAX_PASSWORD_LENGTH, exclude=True, repr=False)
    username: str | None = Field(default=None, max_length=64, exclude=True)
    display_name: str | None = Field(default=None, max_length=200, exclude=True)


class ResendIn(Strict):
    email: str = Field(min_length=3, max_length=320)


class ConfirmRegistrationIn(Strict):
    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    username: str | None = Field(default=None, min_length=3, max_length=64, pattern=r"^[\w.\-]+$")
    display_name: str | None = Field(default=None, max_length=200)


class ConfirmAccountIn(Strict):
    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)


def _service(request: Request, session: Session, now: datetime) -> VerificationService:
    settings = request.app.state.settings
    return VerificationService(session, lambda: now,
                               token_ttl=timedelta(minutes=settings.verification_token_ttl_minutes),
                               pending_ttl=timedelta(hours=settings.pending_registration_ttl_hours))


def email_allowed(request: Request, destination: str) -> bool:
    """
    Whether one more email may be queued for `destination` now: its hourly limit, its cooldown and the
    service-wide limit all have room (and are then counted). Never raises 429 -- the caller answers as usual.
    """
    limiter = request.app.state.rate_limiter
    if limiter is None:
        return True
    scopes = (("email_cooldown", "to:" + destination), ("email_destination", "to:" + destination),
              ("email_global", "all"))
    for scope, subject in scopes:
        try:
            limiter.require(scope, subject)
        except ApiError as error:
            if error.status != 429:
                raise
            return False
    for scope, subject in scopes:
        limiter.record(scope, subject)
    return True


def send_queued(request: Request, background: BackgroundTasks) -> None:
    """Send what was just queued, after the response (the outbox worker retries whatever this leaves)."""
    state = request.app.state
    if state.mail is not None:
        background.add_task(outbox.process_due, state.session_factory, state.mail, state.settings, state.clock)


def _begin(email: str, scope: str, request: Request, background: BackgroundTasks, session: Session,
           now: datetime) -> dict:
    throttle(request, scope)
    try:
        address = check_email(email, check_dns=request.app.state.settings.email_dns_check)
    except AccountValidationError as error:
        raise ApiError(422, "validation_error", str(error)) from None
    if request.app.state.mail is None:
        raise ApiError(503, "email_unavailable", "This server cannot send email, so accounts cannot be created.")
    _service(request, session, now).start_registration(address, send=email_allowed(request, address))
    send_queued(request, background)
    return {"status": "pending_verification", "message": GENERIC_ANSWER}


@verification.post("/auth/register", status_code=202,
                   summary="Ask to create an account: a verification email is sent (the same answer for every address).")
def register(payload: RegisterIn, request: Request, background: BackgroundTasks,
             session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    return _begin(payload.email, "register_ip", request, background, session, now)


@verification.post("/auth/verify/resend", status_code=202,
                   summary="Send the verification email again (earlier links stay valid; the same answer always).")
def resend(payload: ResendIn, request: Request, background: BackgroundTasks,
           session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    return _begin(payload.email, "register_ip", request, background, session, now)


@verification.post("/auth/verify/confirm", status_code=201, response_model=UserOut,
                   summary="Confirm an emailed link and create the account with the password given now (single use).")
def confirm_registration(payload: ConfirmRegistrationIn, request: Request, background: BackgroundTasks,
                         session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    throttle(request, "verify_confirm_ip")
    try:
        identity = _service(request, session, now).confirm_registration(
            payload.token, payload.password, username=payload.username, display_name=payload.display_name)
    except InvalidVerificationTokenError as error:
        security_events.record("verification_token_refused", kind="registration")
        raise ApiError(400, "invalid_verification_token", str(error)) from None
    except AccountValidationError as error:
        raise ApiError(422, "validation_error", str(error)) from None
    send_queued(request, background)  # the welcome email
    return _user_out(identity)


@verification.post("/auth/email/verification/request", status_code=202,
                   summary="Email a verification code to the signed-in account's own address.")
def request_account_verification(request: Request, background: BackgroundTasks,
                                 user_id: uuid.UUID = Depends(current_user_id),
                                 session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    if request.app.state.mail is None:
        raise ApiError(503, "email_unavailable", "This server cannot send email.")
    throttle(request, "register_ip")
    service = _service(request, session, now)
    identity = service_identity(session, user_id)
    if identity is not None and not identity.email_verified and email_allowed(request, identity.email):
        service.request_account_verification(user_id)
        send_queued(request, background)
    return {"status": "accepted", "message": "If your address still needs confirming, a code is on its way to it."}


def service_identity(session: Session, user_id: uuid.UUID):
    from backend.accounts import AccountService

    identity = AccountService(session).get(user_id)
    session.rollback()
    return identity


@verification.post("/auth/email/verification/confirm", response_model=UserOut,
                   summary="Confirm the signed-in account's address with an emailed code (single use).")
def confirm_account_verification(payload: ConfirmAccountIn, request: Request,
                                 user_id: uuid.UUID = Depends(current_user_id),
                                 session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    throttle(request, "verify_confirm_ip")
    try:
        return _user_out(_service(request, session, now).confirm_account_verification(user_id, payload.token))
    except InvalidVerificationTokenError as error:
        raise ApiError(400, "invalid_verification_token", str(error)) from None


# -----------------------------------------------------------------------------
# The page an emailed link opens. Loading it does nothing; only the form's submission does.
# -----------------------------------------------------------------------------

_SCRIPT = """
const form = document.getElementById("confirm"), note = document.getElementById("note");
const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
history.replaceState(null, "", location.pathname);
if (!token) { note.textContent = "This page needs the link from your verification email."; form.hidden = true; }
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const first = form.password.value, second = form.confirm.value;
  if (first !== second) { note.textContent = "The passwords do not match."; return; }
  form.querySelector("button").disabled = true;
  try {
    const answer = await fetch("verify/confirm", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: token, password: first, display_name: form.display_name.value || null}),
      credentials: "omit", referrerPolicy: "no-referrer"});
    const body = await answer.json();
    if (answer.ok) {
      form.hidden = true;
      note.textContent = "Your account is ready. Sign in with your email address and this password.";
    }
    else { note.textContent = (body.error && body.error.message) || "The account was not created."; }
  } catch (error) {
    note.textContent = "The server could not be reached. Try again.";
  } finally {
    form.querySelector("button").disabled = false;
  }
});
"""
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode("ascii")
_PAGE = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Confirm your email - Schedule Maxing</title>
<style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;line-height:1.5}}
label{{display:block;margin-top:1rem}}input{{width:100%;padding:.5rem;font-size:1rem}}
button{{margin-top:1.25rem;padding:.6rem 1.2rem;font-size:1rem}}</style></head>
<body><h1>Confirm your email</h1>
<p>Choose the password for your new Schedule Maxing account. The account is created when you press the button.</p>
<form id="confirm" autocomplete="off">
<label>Password<input name="password" type="password" minlength="{MIN_PASSWORD_LENGTH}"
 maxlength="{MAX_PASSWORD_LENGTH}" autocomplete="new-password" required></label>
<label>Repeat it<input name="confirm" type="password" autocomplete="new-password" required></label>
<label>Display name (optional)<input name="display_name" type="text" maxlength="200" autocomplete="name"></label>
<button type="submit">Confirm and create account</button></form>
<p id="note" role="status" aria-live="polite"></p>
<script>{_SCRIPT}</script></body></html>"""
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (f"default-src 'none'; script-src 'sha256-{_SCRIPT_HASH}'; style-src 'unsafe-inline'; "
                                "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"),
}


@verification.get("/auth/verify", response_class=HTMLResponse, include_in_schema=False)
def verify_page() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers=_PAGE_HEADERS)
