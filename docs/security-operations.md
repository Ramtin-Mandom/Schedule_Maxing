# Security operations

Running the hosted backend safely over time: cleanup and retention, what to
watch, dependencies, backups, and the checks that can only be done against a
real deployment. Related: [backend.md](backend.md#protections),
[email.md](email.md), [account-security.md](account-security.md),
[api-security.md](api-security.md), [render-deployment.md](render-deployment.md).

## Cleanup and retention

`backend/maintenance.py` removes expired authentication leftovers, in bounded
batches (500 rows, 20 batches per table per run, one short transaction each).
The server's background worker runs it about hourly (`OUTBOX_WORKER_ENABLED`);
by hand: `python -m backend.maintenance`.

| Data | Removed |
| --- | --- |
| Pending registrations (and their tokens) | when expired |
| Verification, email-change and recovery tokens | 1 day after expiry |
| Browser sessions | 1 day after expiry |
| App sessions with all their refresh credentials | 1 day after the session's absolute expiry |
| Rate-limit buckets | when expired |
| Webhook event ids | after 7 days |
| Email outbox rows (sent or given up) | after 90 days |

**Refresh-reuse evidence.** Replay of a refresh credential is detected by
finding its *consumed* digest. Consumed digests are therefore kept as long as
their session could still be refreshed, and go only with the whole session
after its absolute expiry (`REFRESH_TOKEN_EXPIRE_DAYS`, 30) plus a day.

**User data is not cleaned up.** Accounts, tasks, schedules, execution
history, preferences, the change log and sign-in recognition digests are kept
until the owner deletes them (record deletion, or the task-data reset). The
desktop's local database is the user's own file and is never pruned by the
server.

## What to watch

`backend/security_events.py` writes one JSON line per event on the
`schedule_maxing.security.events` logger and counts them per process; the
totals are logged hourly (`{"event": "counters", "totals": {...}}`).

| Event | Meaning |
| --- | --- |
| `sign_in_failed` | a wrong password (or unknown account) |
| `rate_limited` (`scope`) | a limit refused a request: `login_ip`, `login_fail_ip`, `login_fail_pair`, `register_ip`, `recovery_ip`, `reset_ip`, `mfa_fail`, `reauth_fail`, ... |
| `mfa_code_refused`, `reauthentication_failed` | second-step or current-password failures |
| `verification_token_refused`, `recovery_token_refused` | invalid, expired or replayed links |
| `refresh_replay` | a spent refresh credential was presented again: the session was revoked |
| `email_not_accepted`, `email_given_up` (`kind`, `error`) | the provider refused or kept failing |
| `password_work_saturated` (`reason`) | the Argon2 gate was full: raise capacity or investigate a flood |
| `webhook_refused` | an unauthenticated or stale provider webhook |

Events carry only `scope`, `kind`, `error` and `reason`, and only short
identifier-like values; there is no field for a password, token, code, link,
email address, IP address or database URL, so none can be logged through
them. The access log redacts tokens and passwords in query strings and
`Authorization` headers; no application log line contains a raw token, a
complete verification or recovery URL, or a credential.

Worth an alert: a sustained rise of `sign_in_failed` or
`rate_limited|scope=login_fail_ip`; any `refresh_replay`; `email_given_up`;
`password_work_saturated`.

## Dependencies

- **Lock.** `requirements.lock` pins every installed distribution (direct and
  transitive) to the versions the suite was run with. Install reproducibly:

  ```
  python -m pip install -r requirements.txt -c requirements.lock            # development / CI
  python -m pip install -r requirements-backend.txt -c requirements.lock    # the server
  ```

  Regenerate after changing a requirements file, from a clean virtual
  environment: `python -m pip install -r requirements.txt` then
  `python -m pip freeze > requirements.lock`. It was produced on Windows with
  Python 3.10; a constraints file only pins what gets installed, so it is
  usable on Linux too, but that install was not run from here. It records
  versions, not hashes (hash-pinning needs `pip-tools`, which the repository
  does not use).
- **Scanning (CI job `security-scan`).** `pip-audit` checks the lock against
  known vulnerabilities; `detect-secrets` checks every tracked file against the
  reviewed baseline `secret-scan-baseline.json` (regenerate it with
  `detect-secrets scan > secret-scan-baseline.json` only after reading each new entry:
  the baseline lists test fixtures and placeholders, never a real secret). It
  scans the working tree, not the history. On Windows set `PYTHONUTF8=1` for
  the scan and write `/` in the recorded paths: without it every file with a
  non-ASCII character is silently skipped, and CI (Linux) then reports its
  entries as new.

Handling policy:

| Finding | Action |
| --- | --- |
| Vulnerability with a fixed version | upgrade, regenerate the lock, run the suite; within 7 days for high/critical, 30 days otherwise |
| No fix available | check whether the vulnerable code path is used; record the decision in the pull request and ignore that id explicitly (`pip-audit --ignore-vuln <id>`), with a date to revisit |
| A real secret in the repository | rotate it first (see api-security.md, "Keys and rotation"), then remove it; history rewriting is secondary -- a pushed secret is compromised |
| A false positive | allowlist the exact finding in the scanner's configuration with a comment, never a whole path |

## Backups and restoration

The server's database holds every account. Back it up and test restoring it.

- **Managed PostgreSQL (Render).** Use the platform's backups / point-in-time
  recovery where the plan has them, and additionally export:

  ```
  pg_dump --format=custom --no-owner --file=schedule-maxing-$(date +%F).dump "$MIGRATION_DATABASE_URL"
  ```

  Store dumps encrypted, away from the database's account. A dump contains
  password hashes, session digests and encrypted TOTP secrets: treat it as
  secret.
- **Restore** into a new, empty database, then point the server at it:

  ```
  pg_restore --no-owner --dbname="$NEW_DATABASE_URL" schedule-maxing-<date>.dump
  DATABASE_URL="$NEW_DATABASE_URL" python -m backend.migrate check
  ```

  After a restore to an earlier point: sessions created since then are gone
  (users sign in again), and so are records synchronized since then -- the
  desktop clients still hold their local copies and push them again. Keep
  `JWT_SECRET` and above all `MFA_ENCRYPTION_KEY` with the backup: without the
  latter, accounts with two-step sign-in cannot finish signing in.
- **Restoration test.** `tests/backend/test_security_operations.py` backs up a
  database with accounts and records, restores the copy and checks that the
  server on the restored copy is ready, signs the same user in and serves the
  same record. It runs on SQLite with throwaway data. A `pg_dump` /
  `pg_restore` round trip against the real provider was **not** performed from
  here; do it once on a staging database (checklist below).
- **Desktop.** The local database is backed up automatically before each
  schema migration (`app/execution/backup.py`, tested in
  `tests/execution/test_backup.py`).

## Build artifacts and examples

- `packaging/windows/check_bundle.py` fails the Windows build if the package
  contains a `.env` file, text such as `DATABASE_URL=`, `JWT_SECRET=` or a
  private key, or any server module (`backend`, SQLAlchemy, a database driver,
  FastAPI).
- `.env.example` holds placeholders only; `.env` and `.env.*` are git-ignored.
- The desktop app's only built-in address is the public API URL; credentials
  are entered at sign-in and kept in the OS credential store.

## Staging checklist

These need a real deployment and were **not** observed from this repository.
Tick each only after seeing it.

Email (docs/email.md):

- [ ] SPF, DKIM and DMARC records published; the provider shows the domain verified.
- [ ] `python -m backend.email_diagnostic send` reports "configuration: passed" and "accepted by the provider: yes".
- [ ] The test message is actually in the test inbox (not spam) with the code the tool printed.
- [ ] `python -m backend.email_diagnostic status <id>` shows delivery evidence (the webhook reached `/webhooks/email` and was accepted).
- [ ] A throwaway address completes register -> email -> confirm -> one welcome email.
- [ ] A request for a mailbox that does not exist produces a bounce record, not an error to the requester.

Proxy and client addresses (docs/render-deployment.md):

- [ ] The start command ends with `--no-proxy-headers`; `FORWARDED_ALLOW_IPS` is unset.
- [ ] The log line naming the untrusted forwarding peer was read; Render confirmed the range; `TRUSTED_PROXIES` is exactly that.
- [ ] A request with forged `X-Forwarded-For` / `CF-Connecting-IP` is limited by its real address.
- [ ] Six wrong sign-ins from one network are limited; a different network is not.

Resources:

- [ ] `WEB_CONCURRENCY` equals the real worker count and `PASSWORD_HASH_MEMORY_BUDGET_MB` fits the instance.
- [ ] Under a burst of concurrent sign-ins the service answers 503 `temporarily_unavailable` instead of restarting (watch memory and `password_work_saturated`).
- [ ] The hourly cleanup line appears in the log; table sizes stay flat over a week.

Accounts and keys:

- [ ] Migrations ran with `MIGRATION_DATABASE_URL`; the server's role cannot `CREATE TABLE`.
- [ ] `MFA_ENCRYPTION_KEY` is set and stored with the backups; enrolling, signing in with a code and with a recovery code work.
- [ ] `/docs` is unavailable in production unless deliberately enabled; responses carry `Strict-Transport-Security`.
- [ ] A `pg_dump` was restored into a scratch database and the server started on it.
