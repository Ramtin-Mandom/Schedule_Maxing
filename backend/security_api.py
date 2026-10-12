"""
backend/security_api.py

Routes for what an account's owner does to its own credentials, sessions and
second factor (rules: backend/account_security.py, backend/mfa.py):

    POST   /auth/password/change         {current_password, new_password}
    GET    /auth/sessions                the account's live sessions
    DELETE /auth/sessions/{id}           end one of them
    POST   /auth/sessions/revoke-all     end all of them (this one too)
    GET    /auth/mfa                     whether two-step sign-in is on
    POST   /auth/mfa/totp/enroll         {current_password}        -> the secret, once
    POST   /auth/mfa/totp/confirm        {current_password, code}  -> recovery codes, once
    POST   /auth/mfa/totp/disable        {current_password, code}
    POST   /auth/mfa/recovery-codes      {current_password, code}  -> new recovery codes, once
    POST   /auth/email/change/request    {current_password, new_email} -> a code, mailed to the new address
    POST   /auth/email/change/confirm    {token}
    POST   /auth/mfa/verify              {mfa_token, code}         -> the app session (second step)
    POST   /auth/browser/mfa             {mfa_token, code}         -> the browser session (second step)

Everything but the two second-step routes needs a signed-in session, and
every change to a credential or the second factor also needs the current
password again (reauthentication): a borrowed, unlocked device is not enough
to change the password, add or remove an authenticator, or mint recovery
codes. Removing the factor and minting codes need a current code as well.

A session is always the caller's own: ids are looked up with the
authenticated account, so another account's session id is 404, like one
that does not exist.

Limits (backend/rate_limit.py): wrong passwords here and wrong second-step
codes are counted per account (5 per 15 minutes each); over the limit the
attempt is refused before anything is verified. Second steps are also
limited per client address.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.orm import Session

from backend import account_security, models, outbox, security_events
from backend.accounts import AccountService, AccountValidationError, InvalidCredentialsError, check_email
from backend.api import (
    BrowserSessionOut,
    current_user_id,
    get_session,
    mfa_service,
    server_now,
    start_app_session,
    start_browser_session,
    throttle,
)
from backend.auth_schemas import TokenPairOut
from backend.errors import ApiError, unauthenticated
from backend.mfa import InvalidMfaCodeError, MfaStateError, MfaUnavailableError
from backend.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, verify_password
from backend.recovery import credential_epoch
from backend.resources import Strict
from backend.security import TokenError, verify_mfa_token
from backend.verification import EmailChangeService, InvalidVerificationTokenError
from backend.verification_api import email_allowed, send_queued

security = APIRouter(tags=["accounts"])


class PasswordChangeIn(Strict):
    current_password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class ReauthIn(Strict):
    current_password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class ReauthCodeIn(ReauthIn):
    #: The authenticator's current code, or an unused recovery code.
    code: str = Field(min_length=6, max_length=64)


class SecondStepIn(Strict):
    mfa_token: str = Field(min_length=1, max_length=2048)
    code: str = Field(min_length=6, max_length=64)


class SessionOut(BaseModel):
    id: uuid.UUID
    #: "app" (a desktop or other native client) or "browser".
    kind: str
    created_at: datetime
    expires_at: datetime
    client: str | None
    current: bool


class SessionsOut(BaseModel):
    sessions: list[SessionOut]


class MfaStatusOut(BaseModel):
    available: bool
    enabled: bool
    recovery_codes_left: int


class EnrollmentOut(BaseModel):
    #: Shown once: type it into the authenticator app (or open the otpauth URI).
    secret: str
    otpauth_uri: str


class RecoveryCodesOut(BaseModel):
    #: Shown once; each works once.
    recovery_codes: list[str]


def _limited(request: Request, scope: str, user_id: uuid.UUID, *, failed: bool = False) -> None:
    limiter = request.app.state.rate_limiter
    if limiter is None:
        if failed:
            security_events.record("mfa_code_refused" if scope == "mfa_fail" else "reauthentication_failed")
        return
    if failed:
        security_events.record("mfa_code_refused" if scope == "mfa_fail" else "reauthentication_failed")
        limiter.record(scope, f"account:{user_id}")
    else:
        limiter.require(scope, f"account:{user_id}")


def reauthenticate(request: Request, session: Session, user_id: uuid.UUID, password: SecretStr) -> models.User:
    """The account, once its current password was given again; 403 reauthentication_failed otherwise."""
    _limited(request, "reauth_fail", user_id)
    user = session.get(models.User, user_id, populate_existing=True)
    stored = user.password_hash if user is not None else None
    session.rollback()
    if not verify_password(password.get_secret_value(), stored):
        _limited(request, "reauth_fail", user_id, failed=True)
        raise ApiError(403, "reauthentication_failed", "The current password is incorrect.")
    return session.get(models.User, user_id)


def _second_factor(request: Request, session: Session, user_id: uuid.UUID, code: str, now: datetime) -> None:
    """A current code of the account's second factor, or 403 invalid_mfa_code (failures are counted)."""
    _limited(request, "mfa_fail", user_id)
    try:
        accepted = mfa_service(request, session, now).verify(user_id, code)
    except MfaUnavailableError as error:
        raise ApiError(503, "mfa_unavailable", str(error)) from None
    if not accepted:
        _limited(request, "mfa_fail", user_id, failed=True)
        raise ApiError(403, "invalid_mfa_code", str(InvalidMfaCodeError()))


# -----------------------------------------------------------------------------
# Password
# -----------------------------------------------------------------------------


@security.post("/auth/password/change",
               summary="Change the password (needs the current one); every session ends, this one included.")
def change_password(payload: PasswordChangeIn, request: Request, background: BackgroundTasks,
                    user_id: uuid.UUID = Depends(current_user_id), session: Session = Depends(get_session),
                    now: datetime = Depends(server_now)) -> dict:
    _limited(request, "reauth_fail", user_id)
    try:
        account_security.change_password(session, user_id, payload.current_password.get_secret_value(),
                                         payload.new_password.get_secret_value(), now)
    except InvalidCredentialsError:
        _limited(request, "reauth_fail", user_id, failed=True)
        raise ApiError(403, "reauthentication_failed", "The current password is incorrect.") from None
    except AccountValidationError as error:
        raise ApiError(422, "validation_error", str(error)) from None
    send_queued(request, background)
    return {"status": "changed", "message": "Your password was changed. Sign in again with the new password."}


# -----------------------------------------------------------------------------
# Sessions
# -----------------------------------------------------------------------------


@security.get("/auth/sessions", response_model=SessionsOut, summary="The account's live sessions.")
def sessions(request: Request, user_id: uuid.UUID = Depends(current_user_id),
             session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    current = getattr(request.state, "session_ref", None)
    return {"sessions": account_security.list_sessions(session, user_id, now, current=current)}


@security.delete("/auth/sessions/{session_id}", status_code=204, response_class=Response,
                 summary="End one of the account's sessions; its tokens are refused from the next request on.")
def revoke_session(session_id: uuid.UUID, user_id: uuid.UUID = Depends(current_user_id),
                   session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> Response:
    if not account_security.revoke_session(session, user_id, session_id, now):
        raise ApiError(404, "not_found", "The account has no such session.")
    return Response(status_code=204)


@security.post("/auth/sessions/revoke-all", status_code=204, response_class=Response,
               summary="Sign out everywhere: every session of the account ends at once, this one included.")
def revoke_all_sessions(user_id: uuid.UUID = Depends(current_user_id), session: Session = Depends(get_session),
                        now: datetime = Depends(server_now)) -> Response:
    account_security.sign_out_everywhere(session, user_id, now)
    return Response(status_code=204)


# -----------------------------------------------------------------------------
# Two-step sign-in
# -----------------------------------------------------------------------------


@security.get("/auth/mfa", response_model=MfaStatusOut, summary="Whether two-step sign-in is on for the account.")
def mfa_status(request: Request, user_id: uuid.UUID = Depends(current_user_id),
               session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    service = mfa_service(request, session, now)
    return {"available": request.app.state.mfa_cipher is not None, "enabled": service.enabled(user_id),
            "recovery_codes_left": service.recovery_codes_left(user_id)}


@security.post("/auth/mfa/totp/enroll", response_model=EnrollmentOut,
               summary="Start setting up an authenticator app (needs the current password).")
def enroll(payload: ReauthIn, request: Request, user_id: uuid.UUID = Depends(current_user_id),
           session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    user = reauthenticate(request, session, user_id, payload.current_password)
    email = user.email
    try:
        secret, uri = mfa_service(request, session, now).begin_enrollment(user_id, email)
    except MfaUnavailableError as error:
        raise ApiError(503, "mfa_unavailable", str(error)) from None
    except MfaStateError as error:
        raise ApiError(409, "mfa_state", str(error)) from None
    return {"secret": secret, "otpauth_uri": uri}


@security.post("/auth/mfa/totp/confirm", response_model=RecoveryCodesOut,
               summary="Turn two-step sign-in on with a code from the authenticator (needs the current password).")
def confirm(payload: ReauthCodeIn, request: Request, background: BackgroundTasks,
            user_id: uuid.UUID = Depends(current_user_id), session: Session = Depends(get_session),
            now: datetime = Depends(server_now)) -> dict:
    user = reauthenticate(request, session, user_id, payload.current_password)
    email = user.email
    _limited(request, "mfa_fail", user_id)
    try:
        codes = mfa_service(request, session, now).confirm_enrollment(user_id, payload.code)
    except MfaUnavailableError as error:
        raise ApiError(503, "mfa_unavailable", str(error)) from None
    except MfaStateError as error:
        raise ApiError(409, "mfa_state", str(error)) from None
    except InvalidMfaCodeError as error:
        _limited(request, "mfa_fail", user_id, failed=True)
        raise ApiError(403, "invalid_mfa_code", str(error)) from None
    account_security.notify(session, outbox.MFA_ENABLED, user_id, email, now,
                            dedupe_key=f"mfa_enabled:{user_id}:{int(now.timestamp())}")
    session.commit()
    send_queued(request, background)
    return {"recovery_codes": codes}


@security.post("/auth/mfa/totp/disable", status_code=204, response_class=Response,
               summary="Turn two-step sign-in off (needs the current password and a current code).")
def disable(payload: ReauthCodeIn, request: Request, background: BackgroundTasks,
            user_id: uuid.UUID = Depends(current_user_id), session: Session = Depends(get_session),
            now: datetime = Depends(server_now)) -> Response:
    user = reauthenticate(request, session, user_id, payload.current_password)
    email = user.email
    service = mfa_service(request, session, now)
    if not service.enabled(user_id):
        raise ApiError(409, "mfa_state", "Two-step sign-in is not on for this account.")
    _second_factor(request, session, user_id, payload.code, now)
    service.disable(user_id)
    account_security.notify(session, outbox.MFA_DISABLED, user_id, email, now,
                            dedupe_key=f"mfa_disabled:{user_id}:{int(now.timestamp())}")
    session.commit()
    send_queued(request, background)
    return Response(status_code=204)


@security.post("/auth/mfa/recovery-codes", response_model=RecoveryCodesOut,
               summary="Replace the recovery codes (needs the current password and a current code).")
def new_recovery_codes(payload: ReauthCodeIn, request: Request, user_id: uuid.UUID = Depends(current_user_id),
                       session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    reauthenticate(request, session, user_id, payload.current_password)
    _second_factor(request, session, user_id, payload.code, now)
    try:
        return {"recovery_codes": mfa_service(request, session, now).regenerate_recovery_codes(user_id)}
    except MfaStateError as error:
        raise ApiError(409, "mfa_state", str(error)) from None


# -----------------------------------------------------------------------------
# Email address
# -----------------------------------------------------------------------------


class EmailChangeIn(ReauthIn):
    new_email: str = Field(min_length=3, max_length=320)


class EmailChangeConfirmIn(Strict):
    token: str = Field(min_length=1, max_length=128)


@security.post("/auth/email/change/request", status_code=202,
               summary="Ask to change the account's email address (needs the current password): a code is mailed "
                       "to the new address.")
def request_email_change(payload: EmailChangeIn, request: Request, background: BackgroundTasks,
                         user_id: uuid.UUID = Depends(current_user_id), session: Session = Depends(get_session),
                         now: datetime = Depends(server_now)) -> dict:
    if request.app.state.mail is None:
        raise ApiError(503, "email_unavailable", "This server cannot send email.")
    user = reauthenticate(request, session, user_id, payload.current_password)
    current = user.email
    session.rollback()
    try:
        address = check_email(payload.new_email, check_dns=request.app.state.settings.email_dns_check)
    except AccountValidationError as error:
        raise ApiError(422, "validation_error", str(error)) from None
    if address == current:
        raise ApiError(422, "validation_error", "That is already the account's email address.")
    EmailChangeService(session, lambda: now).request(user_id, address, send=email_allowed(request, address))
    send_queued(request, background)
    return {"status": "accepted", "message": "If that address can be used, a code is on its way to it. Enter the "
                                             "code here, signed in, to finish the change."}


@security.post("/auth/email/change/confirm",
               summary="Finish changing the email address with the code mailed to the new one; every session ends.")
def confirm_email_change(payload: EmailChangeConfirmIn, request: Request, background: BackgroundTasks,
                         user_id: uuid.UUID = Depends(current_user_id), session: Session = Depends(get_session),
                         now: datetime = Depends(server_now)) -> dict:
    throttle(request, "verify_confirm_ip")
    try:
        EmailChangeService(session, lambda: now).confirm(user_id, payload.token)
    except InvalidVerificationTokenError as error:
        raise ApiError(400, "invalid_verification_token", str(error)) from None
    send_queued(request, background)  # the notice to the previous address
    return {"status": "changed", "message": "The email address was changed. Sign in again with the new address."}


def _second_step(payload: SecondStepIn, request: Request, session: Session, now: datetime):
    """The account whose password step `mfa_token` proves, once `code` passed; 401 otherwise."""
    throttle(request, "mfa_ip")
    try:
        user_id, epoch = verify_mfa_token(payload.mfa_token, request.app.state.settings, now)
    except TokenError:
        raise unauthenticated("Sign in again: the second step took too long or is not valid.") from None
    if credential_epoch(session, user_id) != epoch:
        raise unauthenticated("Sign in again: the second step took too long or is not valid.")
    session.rollback()
    _limited(request, "mfa_fail", user_id)
    try:
        accepted = mfa_service(request, session, now).verify(user_id, payload.code)
    except MfaUnavailableError as error:
        raise ApiError(503, "mfa_unavailable", str(error)) from None
    if not accepted:
        _limited(request, "mfa_fail", user_id, failed=True)
        raise unauthenticated("That code is not valid.")
    identity = AccountService(session).get(user_id)
    session.rollback()
    return identity


@security.post("/auth/mfa/verify", response_model=TokenPairOut,
               summary="The second step of signing in: a code for the token /auth/login answered with.")
def verify_second_step(payload: SecondStepIn, request: Request, session: Session = Depends(get_session),
                       now: datetime = Depends(server_now)) -> dict:
    return start_app_session(request, session, _second_step(payload, request, session, now), now)


@security.post("/auth/browser/mfa", response_model=BrowserSessionOut,
               summary="The second step of a browser sign-in: sets the session cookie.")
def browser_second_step(payload: SecondStepIn, request: Request, response: Response,
                        session: Session = Depends(get_session), now: datetime = Depends(server_now)) -> dict:
    return start_browser_session(request, response, session, _second_step(payload, request, session, now), now)
