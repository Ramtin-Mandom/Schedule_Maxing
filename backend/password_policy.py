"""
backend/password_policy.py

The one rule for a *new* password -- signup, recovery reset and password
change all call check_new_password (through accounts.check_password), and
nothing else decides. Signing in applies no rule: an existing account's
password keeps working whatever it is, until its owner changes it.

Following NIST SP 800-63B for password-only authentication:
    - at least MIN_PASSWORD_LENGTH (15) characters, at most
      MAX_PASSWORD_LENGTH (1024); counted in characters, never truncated;
    - any characters: spaces, Unicode, emoji, pasted password-manager output.
      No "one uppercase, one symbol" rules and no forced periodic change;
    - not a password that is trivially guessed although long: one character
      or a short unit repeated, a keyboard or alphabet run, a well-known
      long password (COMMON, compared ignoring case and spacing), or the
      account's own email address or username;
    - not a password known from breaches, when a breach checker is
      configured (PASSWORD_BREACH_CHECK). HibpRangeChecker asks the Pwned
      Passwords range API with k-anonymity: only the first 5 hex digits of
      the password's SHA-1 leave the server (with padding requested), never
      the password or its full hash. If the service cannot be reached the
      check is skipped -- an outage must not stop signups and resets -- and
      that is logged.

Messages never contain the password.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request
from collections.abc import Callable

LOG = logging.getLogger("schedule_maxing.security")

MIN_PASSWORD_LENGTH = 15
MAX_PASSWORD_LENGTH = 1024

#: Long passwords that are guessed first anyway (lower case, without spaces).
COMMON = frozenset({
    "passwordpassword", "passwordpassword1", "password123456789", "password1234567890", "mypasswordispassword",
    "thisismypassword", "thisisapassword", "iloveyouiloveyou", "letmeinletmeinletmein", "qwertyuiopasdfghjkl",
    "qwertyuiopasdfghjklzxcvbnm", "1q2w3e4r5t6y7u8i9o0p", "correcthorsebatterystaple", "schedulemaxingpassword",
    "administratoradministrator", "welcomewelcomewelcome", "changemechangemechangeme", "trustno1trustno1trustno1",
    "aaaaaaaaaaaaaaaaaaaa", "nopasswordnopassword", "defaultpassword123", "passw0rdpassw0rdpassw0rd",
    "letmein1234567890", "iloveyou1234567890", "qwerty1234567890", "abc123abc123abc123", "football1234567890",
    "monkeymonkeymonkey", "dragondragondragon", "sunshinesunshine", "princessprincess", "superman1234567890",
})
_RUNS = ("abcdefghijklmnopqrstuvwxyz" * 2, "0123456789" * 4, "qwertyuiopasdfghjklzxcvbnm" * 2,
         "1234567890" * 4, "qwertzuiopasdfghjklyxcvbnm" * 2)

#: password -> True (known breached), False (not known), None (could not be checked).
BreachChecker = Callable[[str], "bool | None"]
_breach_checker: BreachChecker | None = None


class WeakPasswordError(ValueError):
    """The password breaks the policy; the message says how, without the password."""


def configure(breach_checker: BreachChecker | None) -> None:
    """Set this process's breach checker (the server does, at startup; None: no breach lookup)."""
    global _breach_checker
    _breach_checker = breach_checker


def _repeats_a_short_unit(text: str) -> bool:
    return any(len(text) > unit and text == (text[:unit] * (len(text) // unit + 1))[:len(text)] for unit in range(1, 5))


def _is_a_run(text: str) -> bool:
    return any(text in run or text[::-1] in run for run in _RUNS)


def check_new_password(password: str, *, email: str | None = None, username: str | None = None) -> None:
    """WeakPasswordError unless `password` may be set as a new password for the account named by the context."""
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"The password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters long. A few "
            "unrelated words make a good one.")
    folded = password.casefold()
    squeezed = "".join(folded.split())
    if _repeats_a_short_unit(folded) or _is_a_run(squeezed) or squeezed in COMMON:
        raise WeakPasswordError("That password is too easy to guess. Choose one that is not a common phrase, "
                                "a repeated pattern or a keyboard run.")
    for own in (email, username):
        if own and folded in (own.casefold(), own.casefold() * 2):
            raise WeakPasswordError("The password must not be your email address or username.")
    if _breach_checker is not None and _breach_checker(password):
        raise WeakPasswordError("That password appears in known data breaches, so it would be guessed quickly. "
                                "Choose a different one.")


class HibpRangeChecker:
    """Pwned Passwords by k-anonymity: sends 5 hex digits of the SHA-1, compares the rest locally."""

    endpoint = "https://api.pwnedpasswords.com/range/"

    def __init__(self, timeout: float = 3.0, fetch: Callable[[str], str] | None = None) -> None:
        self._timeout = timeout
        self._fetch = fetch or self._download

    def _download(self, prefix: str) -> str:
        request = urllib.request.Request(self.endpoint + prefix, headers={
            "Add-Padding": "true", "User-Agent": "schedule-maxing-backend"})
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - fixed https URL
            return response.read().decode("utf-8", "replace")

    def __call__(self, password: str) -> bool | None:
        digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
        prefix, suffix = digest[:5], digest[5:]
        try:
            listing = self._fetch(prefix)
        except (OSError, urllib.error.URLError, ValueError) as error:
            LOG.warning("the breached-password lookup was skipped (%s)", type(error).__name__)
            return None
        for line in listing.splitlines():
            candidate, _, count = line.strip().partition(":")
            if candidate.upper() == suffix:
                return count.strip().isdigit() and int(count) > 0
        return False
