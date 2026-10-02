"""
backend/recovery_api.py

The hosted password-recovery endpoints (backend/recovery.py has the rules):

    POST /auth/recovery/request  {identifier}         -> 202, always the same answer
    POST /auth/recovery/reset    {token, new_password} -> 200, or 400 invalid_recovery_token
    GET  /auth/recovery/reset                          -> the reset page a delivered link opens

No login is needed for any of them, and none returns a recovery credential.
A request answers 202 with one generic message whether or not an account
matches; the link is delivered in the background after the response, so
neither the answer nor its timing depends on the account or on delivery.
When recovery is not configured (RECOVERY_PUBLIC_URL, SMTP_HOST,
SMTP_SENDER), a request answers 503 recovery_unavailable for every
identifier alike. A reset needs only the token, so it works whenever the
server runs (the desktop's code-entry flow uses it directly).

The reset page is self-contained (no third-party content), sent with
Cache-Control: no-store, Referrer-Policy: no-referrer, a restrictive
Content-Security-Policy (its one inline script is allowed by hash) and
frame denial. It reads the token from the URL fragment, removes it from the
address bar at once, and posts it with the new password; it never redirects
anywhere. After a reset, the user signs in again (no automatic sign-in).

Throttling (backend/rate_limit.py): requests per client address and per
identifier; resets per client address.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import Field
from sqlalchemy.orm import Session

from backend.accounts import AccountValidationError
from backend.api import get_session, server_now, throttle
from backend.errors import ApiError
from backend.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from backend.rate_limit import identifier_subject
from backend.recovery import MAX_TOKEN_LENGTH, InvalidRecoveryTokenError, RecoveryService
from backend.recovery_delivery import deliver_quietly
from backend.resources import Strict

GENERIC_ANSWER = ("If an account matches, a password recovery link has been sent to its email address. "
                  "The link works once and expires soon.")

recovery = APIRouter(tags=["accounts"])


class RecoveryRequestIn(Strict):
    #: The account's email address or username.
    identifier: str = Field(min_length=1, max_length=320)


class RecoveryResetIn(Strict):
    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


def _service(request: Request, session: Session, now: datetime) -> RecoveryService:
    settings = request.app.state.settings
    return RecoveryService(session, lambda: now, timedelta(minutes=settings.recovery_token_ttl_minutes))


@recovery.post("/auth/recovery/request", status_code=202,
               summary="Ask for a password recovery link (the same answer whether or not the account exists).")
def request_recovery(
    payload: RecoveryRequestIn,
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    now: datetime = Depends(server_now),
) -> dict:
    adapter = request.app.state.recovery_delivery
    if adapter is None:
        raise ApiError(503, "recovery_unavailable", "Password recovery is not configured on this server.")
    throttle(request, "recovery_ip")
    throttle(request, "recovery_identifier", identifier_subject(payload.identifier))
    delivery = _service(request, session, now).request(payload.identifier)
    if delivery is not None:
        background.add_task(deliver_quietly, adapter, request.app.state.settings, delivery)
    return {"status": "accepted", "message": GENERIC_ANSWER}


@recovery.post("/auth/recovery/reset", summary="Set a new password with a recovery token (single use).")
def reset_password(
    payload: RecoveryResetIn,
    request: Request,
    session: Session = Depends(get_session),
    now: datetime = Depends(server_now),
) -> dict:
    throttle(request, "reset_ip")
    try:
        _service(request, session, now).reset(payload.token, payload.new_password)
    except InvalidRecoveryTokenError as error:
        raise ApiError(400, "invalid_recovery_token", str(error)) from None
    except AccountValidationError as error:
        raise ApiError(422, "validation_error", str(error)) from None
    return {"status": "reset", "message": "Your password was changed. Sign in with the new password."}


_SCRIPT = """
const form = document.getElementById("reset"), note = document.getElementById("note");
const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
history.replaceState(null, "", location.pathname);
if (!token) { note.textContent = "This page needs the link from your recovery email."; form.hidden = true; }
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const first = form.password.value, second = form.confirm.value;
  if (first !== second) { note.textContent = "The passwords do not match."; return; }
  form.querySelector("button").disabled = true;
  try {
    const answer = await fetch("reset", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: token, new_password: first}), credentials: "omit", referrerPolicy: "no-referrer"});
    const body = await answer.json();
    note.textContent = answer.ok ? body.message : (body.error && body.error.message) || "The password was not changed.";
    if (answer.ok) { form.hidden = true; }
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
<meta name="referrer" content="no-referrer"><title>Reset password - Schedule Maxing</title>
<style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;line-height:1.5}}
label{{display:block;margin-top:1rem}}input{{width:100%;padding:.5rem;font-size:1rem}}
button{{margin-top:1.25rem;padding:.6rem 1.2rem;font-size:1rem}}</style></head>
<body><h1>Choose a new password</h1>
<form id="reset" autocomplete="off">
<label>New password<input name="password" type="password" minlength="{MIN_PASSWORD_LENGTH}"
 maxlength="{MAX_PASSWORD_LENGTH}" autocomplete="new-password" required></label>
<label>Repeat it<input name="confirm" type="password" autocomplete="new-password" required></label>
<button type="submit">Change password</button></form>
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


@recovery.get("/auth/recovery/reset", response_class=HTMLResponse, include_in_schema=False)
def reset_page() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers=_PAGE_HEADERS)
