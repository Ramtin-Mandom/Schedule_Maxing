"""
backend/security.py

Password hashing and access tokens, built only on maintained libraries:

    - argon2-cffi's PasswordHasher (Argon2id): backend/passwords.py, re-exported
      here for the HTTP API's existing imports.
    - PyJWT for HS256 access tokens. Verification pins the algorithm list to
      JWT_ALGORITHM (so "none" or an asymmetric-algorithm confusion is
      impossible), requires exp/iat/nbf/sub/iss/aud/jti/typ, and checks
      issuer, audience, expiry and token type. The user's identity is taken
      only from the verified `sub` claim. A token also carries `cep`, the
      account's credential epoch when it was issued; the API refuses it once
      the stored epoch has moved on (a password reset). A token issued before
      epochs existed has no `cep` and is read as epoch 0 -- valid until the
      account's first reset.

Nothing here logs or returns a password, a hash, or a token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt

# Password hashing and identifiers live in backend/passwords.py (no JWT dependency); re-exported here.
from backend.passwords import (  # noqa: F401
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    normalize_identifier,
    verify_password,
)
from backend.settings import JWT_ALGORITHM, BackendSettings

TOKEN_TYPE = "access"


class TokenError(Exception):
    """The token is missing, malformed, forged, expired, or not an access token for this service."""


@dataclass(frozen=True)
class IssuedToken:
    token: str
    expires_at: datetime
    expires_in: int


def issue_access_token(
    user_id: uuid.UUID, settings: BackendSettings, now: datetime, credential_epoch: int = 0,
    *, session_id: uuid.UUID | None = None,
) -> IssuedToken:
    expires_at = now + timedelta(minutes=settings.access_token_ttl_minutes)
    claims = {
        "cep": int(credential_epoch),
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": TOKEN_TYPE,
    }
    if session_id is not None:
        claims["sid"] = str(session_id)
    token = jwt.encode(claims, settings.jwt_secret, algorithm=JWT_ALGORITHM)
    return IssuedToken(token=token, expires_at=expires_at, expires_in=settings.access_token_ttl_minutes * 60)


def verify_access_token(token: str, settings: BackendSettings, now: datetime) -> uuid.UUID:
    """The user id of a valid access token; TokenError otherwise (never says which check failed to the client)."""
    return verify_access_token_claims(token, settings, now)[0]


def verify_access_token_claims(token: str, settings: BackendSettings, now: datetime) -> tuple[uuid.UUID, int]:
    """(user id, credential epoch) of a valid access token; TokenError otherwise."""
    user_id, epoch, _ = verify_access_identity(token, settings, now)
    return user_id, epoch


def verify_access_identity(token: str, settings: BackendSettings, now: datetime) -> tuple[uuid.UUID, int, uuid.UUID | None]:
    """Verified identity and optional native-session id; legacy tokens remain compatible."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[JWT_ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={
                "require": ["exp", "iat", "nbf", "sub", "iss", "aud", "jti", "typ"],
                "verify_exp": False, "verify_nbf": False, "verify_iat": False,
            },
        )
    except jwt.PyJWTError as error:
        raise TokenError(type(error).__name__) from None
    # Time claims are checked against the injectable server clock (not PyJWT's wall clock), with no leeway.
    times = [claims.get(name) for name in ("exp", "nbf", "iat")]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in times):
        raise TokenError("malformed time claims")
    exp, nbf, iat = times
    if now.timestamp() >= exp:
        raise TokenError("ExpiredSignatureError")
    if now.timestamp() < nbf or now.timestamp() < iat:
        raise TokenError("ImmatureSignatureError")
    if claims.get("typ") != TOKEN_TYPE:
        raise TokenError("wrong token type")
    epoch = claims.get("cep", 0)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise TokenError("malformed credential epoch")
    try:
        session_id = uuid.UUID(claims["sid"]) if "sid" in claims else None
        return uuid.UUID(claims["sub"]), epoch, session_id
    except (ValueError, TypeError, AttributeError):
        raise TokenError("invalid subject") from None
