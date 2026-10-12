# Account security

Passwords, recovery, sessions, two-step sign-in and security notifications.
Code: `backend/password_policy.py`, `backend/recovery.py`,
`backend/account_security.py`, `backend/mfa.py`, `backend/security_api.py`,
`backend/native_sessions.py`, `backend/login_guard.py`. Email verification is
in [email.md](email.md); request limits and proxies in
[backend.md](backend.md#protections).

## Password policy

One rule, applied wherever a password is *set* -- signup confirmation,
recovery reset, password change (`accounts.check_password`):

- 15 to 1024 characters. Any characters: spaces, Unicode, pasted
  password-manager output. No composition rules, no truncation, no forced
  periodic change.
- Not trivially guessable although long: one character or a short unit
  repeated, a keyboard or alphabet run, a well-known long password, or the
  account's own email address or username.
- Not known from breaches, when `PASSWORD_BREACH_CHECK` is on (the default
  from the environment): the Pwned Passwords range API is asked with
  k-anonymity -- 5 hex digits of the password's SHA-1 leave the server, never
  the password or its hash. If that service cannot be reached the lookup is
  skipped and logged; signups and resets are not blocked by its outage.

**Existing users.** Signing in applies no rule: a password chosen under the
old 8-character minimum keeps working. The new rule applies the next time
that user sets a password. Nothing is migrated and no hash changes.

## Recovery

- The public answer is one generic message; the email is queued in the
  outbox and sent after the response, so neither the answer nor its timing
  depends on the account or on the provider. A known and an unknown
  identifier run the same limiter work and no password hashing.
- A new request does not cancel earlier links. At most 5 are valid per
  account (the oldest is revoked beyond that), each for
  `RECOVERY_TOKEN_TTL_MINUTES`; a successful reset revokes the rest.
- Consuming the token and replacing the password is one transaction; of
  concurrent resets with one token exactly one succeeds.
- **Somebody else's requests cannot lock the owner out.** There is no
  per-identifier request limit to exhaust. Emails are limited per
  destination (4 per hour, 60 s apart) and for the whole service; the emails
  an attacker triggers go to the owner, and their links stay valid, so the
  owner can use any of them.
- Separate limits: requests per client address (`recovery_ip`), emails sent
  (`email_*`), and reset attempts -- token guessing -- per client address
  (`reset_ip`).
- A reset ends every session, notifies the owner, verifies the address, and
  does **not** remove two-step sign-in.

## Password change and sessions

| Request | Effect |
| --- | --- |
| `POST /auth/password/change {current_password, new_password}` | Needs the current password. Ends every session, this one included; the client signs in again. |
| `GET /auth/sessions` | The account's live app and browser sessions (id, kind, created, expires, client, current). |
| `DELETE /auth/sessions/{id}` | Ends one. Another account's id is `404`. |
| `POST /auth/sessions/revoke-all` | Ends all of them and raises the credential epoch. |

**What "revoked" means for access tokens.** An access token names its
session, and every request checks that session's row and the account's
credential epoch. So after a revocation, a sign-out everywhere, a password
change or a reset, an already-issued access token is refused on its *next
request* -- it does not stay valid until it expires. What the server cannot
do is reach into an offline device: that device keeps showing its local data
until it next contacts the server, and nothing is fetched or accepted
meanwhile.

**Refresh rotation.** A refresh credential works once and is replaced. A
consumed credential presented again within 10 seconds is treated as the same
client racing itself: `409 refresh_superseded`, no credential issued, the
session continues with the pair the first request received. Presented later,
it is replay: the whole session is revoked and a new sign-in is needed.

The desktop keeps its refresh credential in the operating system's credential
store only ("Keep me signed in"); there is no file fallback.

## Two-step sign-in (TOTP)

Optional, per account, with any authenticator app.

- **Setup** (signed in, current password each time): `enroll` returns the
  secret once; `confirm` with a code from the app turns it on and returns 10
  recovery codes once. An unconfirmed setup changes nothing.
- **Storage.** The secret is encrypted with `MFA_ENCRYPTION_KEY` (Fernet), a
  key used for nothing else. Recovery codes (80 random bits) are stored as
  SHA-256 digests.
- **Signing in.** A correct password answers with a 5-minute `mfa_token` --
  its own signing key and type; it is not an access token and opens nothing.
  No session, cookie or access token exists until `POST /auth/mfa/verify`
  (or `/auth/browser/mfa`) succeeds with a code.
- **Codes.** The current 30-second step or one either side; never accepted
  twice (the accepted step is recorded atomically). A recovery code works
  once.
- **Limits.** 5 wrong codes per account per 15 minutes; then attempts are
  refused before a code is looked at.
- **Removal / new recovery codes** need the current password and a current
  code. **Password reset does not remove it.**
- **Lost authenticator:** a recovery code. Lost both: there is no
  self-service bypass by design; an operator must remove the `mfa_totp` row
  after verifying the person out of band.
- **Without `MFA_ENCRYPTION_KEY`** setup answers `503 mfa_unavailable`; an
  account that already has a factor cannot complete sign-in (it fails closed).
  Losing or changing the key therefore locks those accounts until it is
  restored or their factor is removed by an operator. Back the key up.

## Notifications

Queued in the outbox, deduplicated by key, containing what happened and
when -- never a credential, a code, a token or a sign-in link:

- password changed (change or reset) -- once per credential change;
- two-step sign-in turned on / off;
- a sign-in from a device or network the account had not used before -- once
  per place. The first sign-in ever, repeat sign-ins, refreshes and failed
  attempts send nothing. Recognition stores only a digest of account, network
  (IPv4 address or IPv6 /64) and client.

## Compatibility

- New passwords need 15 characters; older desktop builds validate 8 locally
  and will see the server's refusal instead.
- A desktop build older than this change does not know the second step: an
  account that turns two-step sign-in on must use an updated app.
- `POST /auth/login` may now answer `{mfa_required, mfa_token, expires_in}`
  instead of a token pair.
- A replayed refresh credential inside the 10-second grace answers `409`
  rather than ending the session.

## Migrations and settings

Migration `0018` (additive): `mfa_totp`, `mfa_recovery_codes`,
`sign_in_fingerprints`, and a `client_label` on both session tables.

```
MFA_ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
MFA_ISSUER=Schedule Maxing
PASSWORD_BREACH_CHECK=true
```
