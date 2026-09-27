"""
backend/passwords.py

Account identifiers and password hashing, shared by the HTTP API
(backend/api.py via backend/security.py) and the direct desktop path
(app/persistence) through backend/accounts.py. Nothing here needs a JWT
secret or an HTTP framework.

argon2-cffi's PasswordHasher: Argon2id with the library's default
parameters and a random salt per hash. Only the encoded hash is stored;
hashes are upgraded transparently on sign-in when the library's parameters
change (needs_rehash). Nothing here logs or returns a password.
"""

from __future__ import annotations

import unicodedata
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

#: A valid hash of a random password, verified against when an account does
#: not exist, so an unknown email and a wrong password take similar time.
_DUMMY_HASH = _hasher.hash(uuid.uuid4().hex)

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024


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
