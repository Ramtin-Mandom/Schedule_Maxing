"""
backend/login_guard.py

Protection of sign-in against password guessing, on top of the cheap
per-address request limit (login_ip) that runs before anything else. Only
*failed* attempts are counted here, in the shared limiter
(backend/rate_limit.py), under three subjects:

    login_fail_ip        one client address, whatever accounts it tries: a
                         single source spraying passwords over many accounts.
    login_fail_pair      one client address against one account: a single
                         source guessing one account's password.
    login_fail_account   one account, whatever the address: guessing spread
                         over many addresses. Reaching this limit never locks
                         the account -- that would let anyone lock a user out
                         by knowing an email address. Instead, while it is
                         over the limit every address is allowed only
                         TIGHT_PAIR_FAILURES failures against that account
                         (instead of login_fail_pair's), and one warning is
                         logged (without the account).

before() refuses (429 with Retry-After) before the password is verified, so
a refused attempt costs no hashing; failed() counts after a wrong password.
An address that never failed against an account is never refused for it: the
owner signs in with the right password however many other addresses are
guessing, and every refusal ends on its own when the window slides on.

The account subject comes from AccountService.account_subject(): the account
id when the email or username names an account -- so the two names share one
counter and guessing through both does not double the allowance -- and the
normalized identifier otherwise, so an unknown account is counted and
refused exactly like an existing one.

Nothing is counted or refused while rate limiting is off. A limiter failure
is the limiter's 503 (fail closed).
"""

from __future__ import annotations

import logging

from backend.rate_limit import RateLimiter

LOG = logging.getLogger("schedule_maxing.security")

#: Failures one address may make against an account whose account-wide failure limit is used up.
TIGHT_PAIR_FAILURES = 2


class LoginGuard:
    def __init__(self, limiter: RateLimiter | None, address_subject: str, account_subject: str) -> None:
        self._limiter = limiter
        self._address = address_subject
        self._account = account_subject
        self._pair = f"{address_subject}|{account_subject}"

    def before(self) -> None:
        """Refuse (429) an attempt whose address used up its failures; nothing is counted."""
        limiter = self._limiter
        if limiter is None:
            return
        limiter.require("login_fail_ip", self._address)
        guessed_widely = (limiter.estimate("login_fail_account", self._account)
                          >= limiter.limit("login_fail_account").count)
        limiter.require("login_fail_pair", self._pair, count=TIGHT_PAIR_FAILURES if guessed_widely else None)

    def failed(self) -> None:
        """Count one failed attempt under every subject."""
        limiter = self._limiter
        if limiter is None:
            return
        limiter.record("login_fail_ip", self._address)
        limiter.record("login_fail_pair", self._pair)
        before, after = limiter.record("login_fail_account", self._account)
        threshold = limiter.limit("login_fail_account").count
        if before < threshold <= after:
            LOG.warning("sign-in failures for one account reached the account-wide limit; "
                        "each address is now allowed fewer failures against it")
