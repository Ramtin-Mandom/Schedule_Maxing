# API security and authorization

What the hosted API guarantees about who can reach what, and what the
operator has to supply. Tests: `tests/backend/test_api_security.py`,
`tests/backend/test_isolation.py`.

## Account isolation

There is one ownership model: a record belongs to exactly one account. There
is no sharing, no roles and no route that names another account -- the test
suite asserts that no request or response field is called `user_id` or
`owner_id`.

How it is enforced:

- The account is derived only on the server, from a verified access token or
  a live browser session (`backend/api.py current_user_id`). No route reads an
  owner from the request.
- Every synchronizable table's primary key is `(user_id, id)`. Records are
  fetched as `session.get(Model, (authenticated_user_id, id))` or filtered by
  `user_id`, so knowing or guessing an id opens nothing: another account's
  record is `404`, exactly like one that does not exist. The same client-chosen
  id under two accounts is two unrelated records.
- Input schemas forbid unknown fields (`extra="forbid"`): `user_id`,
  `owner_id`, `id`, `version`, timestamps, `email_verified`, roles and the like
  are refused with `422`, on REST routes, in `/sync/push` payloads and in
  `PATCH /me`.
- References (a task's project, a placement's task, an execution's placement)
  must be live records of the caller.
- All queries are SQLAlchemy expressions with bound parameters; there is no
  string-built SQL.

Endpoint coverage (every route of the OpenAPI schema; `*` = public by design):

| Area | Routes | Scope |
| --- | --- | --- |
| Health | `/health`*, `/ready`* | no data; readiness says only ready / which stage is not |
| Sign-up, sign-in, recovery | `/auth/register`*, `/auth/verify/*`*, `/auth/login`*, `/auth/browser/login`*, `/auth/mfa/verify`*, `/auth/browser/mfa`*, `/auth/refresh`*, `/auth/logout`*, `/auth/recovery/*`* | credential-bearing; rate limited; generic answers |
| Account | `/me`, `/auth/me`, `/auth/sessions*`, `/auth/password/change`, `/auth/mfa*`, `/auth/email/*` | the caller's own account only; a session id of another account is `404` |
| Records | `/projects`, `/tasks`, `/task-types`, `/fixed-blocks`, `/placements`, `/preferences`, `/schedule-generations`, `/executions` (+ `/{id}`, actions, feedback, outcome) | `(user_id, id)` |
| Planning | `/planning/*` (generate, snapshot, reschedule, release, occurrences, series, recurrence, allocation, analytics, CSV preview/import/export, reset) | built on a repository bound to the caller |
| Days | `/days/summary`, `/days/{day}/outcome` | the caller's dates |
| Sync | `/sync/push`, `/sync/capabilities`, `/changes` | each operation runs in the caller's mutation scope; the change log and its cursor are per account |
| Destructive | `/me/task-data/reset`, `/planning/reset` | the caller's data only |
| Webhook | `/webhooks/email`* | provider signature; touches only email delivery records |

The adversarial test seeds one account, then has a second account call every
mutation route with the first one's ids (plus mixed-owner sync batches, forged
owner fields and the first account's change cursor), and asserts both that no
response contains the first account's data and that everything the first
account can read is byte-for-byte unchanged afterwards.

**Result of the audit:** no route was found that reads or writes across
accounts.

## Tokens, cookies, CSRF, CORS

- **Access tokens**: HS256 only (`algorithms=[...]` is pinned, so `none` and
  algorithm confusion are impossible); `exp`, `nbf`, `iat`, `iss`, `aud`,
  `sub`, `jti` and `typ` are required and checked; the credential epoch and the
  session row are checked on every request.
- **Purpose separation**: a second-step (MFA) token has its own key and
  `typ`, lives 5 minutes and is refused as an access token; refresh
  credentials, recovery, verification and email-change tokens are random
  values stored only as purpose-bound digests, not JWTs.
- **Browser cookie**: `HttpOnly`, `Secure` (required in production),
  `SameSite=Strict`, `Path=/`, a random 256-bit value stored only as a digest.
- **CSRF**: every cookie-authenticated unsafe request needs `X-CSRF-Token` (an
  HMAC of the session token under a key of its own) and, when the browser sends
  `Origin`, an allowed origin. Bearer requests carry no ambient credential.
- **CORS** (`CORS_ORIGINS`): exact origins only, never `*`. CORS decides which
  pages may *read* a response; it is not authentication or authorization, and
  an allowed origin without credentials is still `401` (and with a cookie
  still needs the CSRF token).

## Keys and rotation

| Key | Used for | Source |
| --- | --- | --- |
| `JWT_SECRET` | signing access tokens | environment |
| CSRF key | `X-CSRF-Token` | derived from `JWT_SECRET` (HMAC-SHA256, label `schedule-maxing/csrf/v1`) |
| MFA-token key | second-step tokens | derived from `JWT_SECRET` (label `schedule-maxing/mfa/v1`) |
| `MFA_ENCRYPTION_KEY` | encrypting TOTP secrets at rest (Fernet) | environment, independent |
| `EMAIL_WEBHOOK_SECRET` | verifying provider webhooks | issued by the provider |

Derived keys are cryptographically separate from `JWT_SECRET` and from each
other (a value signed for one purpose never verifies for another), but they
rotate with it.

Rotation:

- **`JWT_SECRET`**: set a new value and restart. Access tokens stop verifying;
  apps silently exchange their refresh credential (stored server-side, not
  signed) for a new pair, so nobody has to sign in again. Browser pages fetch a
  new CSRF token from `GET /auth/browser/session`; a second step in progress
  starts over. Do it at once after any suspicion that the value leaked.
- **`MFA_ENCRYPTION_KEY`**: set the new key and move the old one to
  `MFA_ENCRYPTION_KEY_PREVIOUS`. Secrets are decrypted with either and
  re-encrypted under the new key the next time their owner passes a code.
  Remove the previous key when no `mfa_totp.secret_encrypted` is left under it
  (or after asking the remaining users to re-enroll). Never drop the only key:
  those accounts could not finish signing in.
- **`EMAIL_WEBHOOK_SECRET`**: roll it at the provider, then update the
  environment; events signed with the old secret are refused from then on.
- **Database passwords**: change the role's password, update `DATABASE_URL`,
  restart.

## Limits

| Limit | Value |
| --- | --- |
| Request body | `MAX_REQUEST_BYTES` (8 MiB), refused with `413` before parsing |
| Sync push | 200 operations per request |
| Pages | default 100, at most `API_MAX_PAGE_SIZE` (500) |
| Task lists | 50 tags, 366 preferred dates, 200 dependencies; bounded field lengths |
| CSV import | 5 MiB |
| Generation | 2000 tasks; `GENERATION_TIME_LIMIT_SECONDS` |
| Ranges | 62 days; recurrence expansion bounded (docs/recurrence.md) |
| Password work | bounded concurrency and queue (docs/backend.md) |
| Authentication | rate limits (docs/backend.md, docs/account-security.md) |

## Errors and headers

- Unexpected failures answer `500 internal_error` with no detail: no
  traceback, SQL, setting, credential or connection string. Validation errors
  never echo submitted values.
- Every response: `Cache-Control: no-store`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and
  `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` (the
  two HTML pages set their own script-hash policy; the documentation pages are
  exempt). `Strict-Transport-Security` is sent when `ENVIRONMENT=production`.

## Public surface

- `/health` and `/ready` are public and minimal (`/ready` no longer reports the
  schema revision; `python -m backend.migrate check` tells the operator).
- `/docs`, `/redoc` and `/openapi.json` are on by default in development and
  off by default in production; `API_DOCS_ENABLED=true` publishes them.
  Publishing the documentation is a choice, not a vulnerability: every route it
  describes still authenticates and authorizes.

## Database credentials

The server needs only to read and write rows. Give it a role that cannot
change the schema, and run migrations with a different one:

```sql
-- once, as the database owner
CREATE ROLE sm_migrate LOGIN PASSWORD '<secret>';   -- owns the schema; used only by migrations
CREATE ROLE sm_app     LOGIN PASSWORD '<secret>';   -- the server
GRANT CONNECT ON DATABASE <db> TO sm_app;
GRANT USAGE ON SCHEMA public TO sm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE sm_migrate IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE sm_migrate IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sm_app;
-- after the first migration run as sm_migrate, also for the tables that already exist:
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sm_app;
```

- `DATABASE_URL` = the `sm_app` role (the running server).
- `MIGRATION_DATABASE_URL` = the `sm_migrate` role; read only by
  `python -m backend.migrate`. If it is unset, migrations use `DATABASE_URL` as
  before.

This is infrastructure the repository cannot create: it was not applied to any
database from here. On Render, a managed Postgres has one default user; create
the second role with `psql` as shown.

## Direct PostgreSQL mode

Connecting the desktop app straight to PostgreSQL is a development tool:
`DATABASE_URL` reaches every account's data. It is refused in the installed
(frozen) application, the Windows build installs no database driver and
excludes `.env` files and the `backend` package
(`packaging/windows/check_bundle.py` fails the build otherwise), and no
database address is embedded in any build.
