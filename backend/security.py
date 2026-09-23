"""
backend/security.py

Password hashing and access tokens, built only on maintained libraries:

    - argon2-cffi's PasswordHasher (Argon2id, library-default parameters,
      per-hash random salt). Only the encoded hash is stored; hashes are
      transparently upgraded on login when the library's parameters change.
    - PyJWT for HS256 access tokens. Verification pins the algorithm list to
      JWT_ALGORITHM (so "none" or an asymmetric-algorithm confusion is
      impossible), requires exp/iat/nbf/sub/iss/aud/jti/typ, and checks
      issuer, audience, expiry and token type. The user's identity is taken
      only from the verified `sub` claim.

Nothing here logs or returns a password, a hash, or a token.
"""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from backend.settings import JWT_ALGORITHM, BackendSettings

_hasher = PasswordHasher()

#: A valid hash of a random password, verified against when an account does
#: not exist, so an unknown email and a wrong password take similar time.
_DUMMY_HASH = _hasher.hash(uuid.uuid4().hex)

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024
TOKEN_TYPE = "access"


def normalize_identifier(value: str) -> str:
    """Canonical form of an email/username for uniqueness and lookup: NFKC, trimmed, lower-cased."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


class TokenError(Exception):
    """The token is missing, malformed, forged, expired, or not an access token for this service."""


@dataclass(frozen=True)
class IssuedToken:
    token: str
    expires_at: datetime
    expires_in: int


def issue_access_token(user_id: uuid.UUID, settings: BackendSettings, now: datetime) -> IssuedToken:
    expires_at = now + timedelta(minutes=settings.access_token_ttl_minutes)
    claims = {
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": TOKEN_TYPE,
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=JWT_ALGORITHM)
    return IssuedToken(token=token, expires_at=expires_at, expires_in=settings.access_token_ttl_minutes * 60)


def verify_access_token(token: str, settings: BackendSettings, now: datetime) -> uuid.UUID:
    """The user id of a valid access token; TokenError otherwise (never says which check failed to the client)."""
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
    if not all(isinstance(value, int) for value in times):
        raise TokenError("malformed time claims")
    exp, nbf, _ = times
    if now.timestamp() >= exp:
        raise TokenError("ExpiredSignatureError")
    if now.timestamp() < nbf:
        raise TokenError("ImmatureSignatureError")
    if claims.get("typ") != TOKEN_TYPE:
        raise TokenError("wrong token type")
    try:
        return uuid.UUID(claims["sub"])
    except (ValueError, TypeError, AttributeError):
        raise TokenError("invalid subject") from None
