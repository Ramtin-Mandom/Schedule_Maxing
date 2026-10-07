# Backend API: local operation and client security

This implementation belongs to **Schedule_Maxing**, in the `backend/` directory.
No separate database repository is required. The API extends the existing
server rather than introducing another schema or sync protocol.

## Repository inspection and architecture

The inspected checkout already contains:

| Area | Existing implementation reused |
| --- | --- |
| Canonical domain | `app/planning/models.py`: Project, TaskType, Task, RecurrenceSpec, FixedBlock, ScheduledTask; `app/execution/models.py`: TaskExecution and WorkSession. `app/models.py` retains the older optimizer-facing models. |
| Local storage | SQLite repositories in `app/planning` and `app/execution`, with the local migration chain in `app/execution/db.py`. This remains the offline client store. |
| Server storage | SQLAlchemy 2 models in `backend/models.py`, PostgreSQL/psycopg in production and SQLite for portable tests. Composite `(user_id, id)` keys and foreign keys enforce owned relationships. |
| Server migrations | Alembic revisions 0001–0014 already cover normalized records, browser sessions, recurrence, recovery, task types, snapshots and project milestones. 0015 adds native refresh sessions only. |
| Domain services | `PlanningService`, recurrence/workflow, execution lifecycle, server planning repository, resource validators, and transactional `Mutator`. |
| Accounts | `AccountService`, Argon2id hashes, JWT access tokens, cookie sessions, credential epochs and password recovery. |
| Synchronization | Desktop `app/sync` outbox/shadow/conflict handling; server `/sync/push`, `/changes`, immutable revisions and persisted idempotency outcomes. Versions and tombstones are preserved. |
| History/productivity | Execution/session history and placement snapshots feed existing day summaries and planning analytics. Full desktop ML/tracker views are not separate server tables. |
| Configuration | Backend environment settings, `.env.example`, explicit env-file support for operator commands, split desktop/server/database dependencies. |
| Tests/tooling | Backend, sync, planning, execution, productivity, direct-storage and UI suites; PostgreSQL CI; migration checks and `app.persistence.verify_render`. |
| Other folders | `config` holds preference templates; `samples` holds CSV examples; `benchmarks` holds performance checks; `docs` and `.github/workflows` hold operational contracts and CI. |

The missing native refresh/logout flow is added without replacing browser
sessions, accounts, canonical validation, or storage. The module layout is
kept because existing desktop operator adapters reuse its framework-free
components:

```text
backend/
  main.py                  # conventional ASGI entry point
  app.py                   # injectable application factory and middleware
  api.py, planning_api.py   # HTTP adapters and authenticated dependencies
  auth_schemas.py          # native credential request/response schemas
  native_sessions.py       # refresh rotation and revocation service
  security.py, passwords.py, browser_sessions.py
  settings.py, database.py # configuration, engine and request sessions
  models.py                # existing ORM plus refresh-session tables
  resources.py, executions.py, record_mapping.py
  mutations.py, planning_repository.py, sync.py
  migrations/versions/0015_native_sessions.py
  inspect_db.py            # read-only administrator CLI, never an API route
tests/backend/
  test_native_sessions.py, test_inspect_db.py
```

Clients use HTTPS → FastAPI → authenticated service/repository → PostgreSQL.
The offline desktop uses local SQLite plus the existing HTTP sync transport.
No database credentials are returned by the API. The legacy `--storage
postgres` adapter remains available for trusted developer/operator use only;
it is **not a supported distributed-client deployment**. Never bundle its
environment file, PostgreSQL driver configuration or database credentials
with a desktop/Android/web release. Repository privacy is not a security control.

## Local setup (PowerShell)

Use Python 3.10 or newer. From this repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env.local
```

Edit `.env.local` locally. Use a **disposable development database**, never
copy a production connection into an example or test command. Generate a
signing secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`
and put it in `JWT_SECRET`. The API does not discover env files implicitly.

For the quickest portable local setup, use:

```text
DATABASE_URL=sqlite:///./data/backend-local.db
JWT_SECRET=<your generated random value>
ENVIRONMENT=development
ACCESS_TOKEN_TTL_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=30
ALLOWED_HOSTS=localhost,127.0.0.1
BROWSER_COOKIE_SECURE=false
```

Create `data` if needed. SQLite is for local development/tests; use a local
PostgreSQL URL to test production database semantics. Initialize using the
same migrations, then start the server:

```powershell
New-Item -ItemType Directory -Force data | Out-Null
# Uvicorn's explicit env-file loader supplies settings to the application.
# For SQLite migrations, load only the selected file into this command's environment:
python -c "from dotenv import load_dotenv; load_dotenv('.env.local', interpolate=False); from backend.migrate import main; raise SystemExit(main(['upgrade']))"
python -m uvicorn backend.main:app --env-file .env.local --reload --host 127.0.0.1 --port 8000
```

For PostgreSQL, `python -m backend.migrate --env-file .env.local upgrade`
also supports an explicit file and enforces remote TLS. Process environment
variables take precedence; clear stale variables before switching databases.
Open [local API documentation](http://127.0.0.1:8000/docs). `/health` checks
the process; `/ready` checks connectivity and current migration revision.
Neither returns credentials or connection URLs. No schema is created on
application startup.

## Authentication contract

| Method | Endpoint | Authentication | Purpose |
| --- | --- | --- | --- |
| GET | `/health`, `/ready` | Public | Liveness/readiness |
| POST | `/auth/register` | Public, rate limited | Register an Argon2id-backed account |
| POST | `/auth/login` | Password, rate limited | Create a native-client login session and token pair |
| POST | `/auth/refresh` | Refresh credential, rate limited | Consume and rotate a refresh credential |
| POST | `/auth/logout` | Refresh credential, rate limited | Revoke that login session; idempotent 204 |
| GET | `/auth/me`, `/me` | Access token or browser session | Return the authenticated profile |
| PATCH | `/me` | Access token or browser session | Version-checked profile update |
| POST | `/auth/browser/login` | Password, rate limited | Issue HttpOnly browser cookie and CSRF token |
| GET | `/auth/browser/session` | Browser cookie | Browser session status |
| POST | `/auth/browser/logout` | Browser cookie plus CSRF/origin checks | Revoke browser session |
| POST | `/auth/recovery/request`, `/auth/recovery/reset` | Recovery protocol, rate limited | Request/reset a password when SMTP is configured |
| GET/POST | `/tasks`, `/task-types`, `/projects`, `/fixed-blocks`, `/placements`, `/preferences`, `/schedule-generations` | Authenticated | List/create owned resources |
| GET/PUT/DELETE | The same resource paths plus `/{id}` | Authenticated | Read/update/soft-delete with version preconditions |
| GET/POST | `/executions` | Authenticated | Read/upload execution history |
| GET/DELETE | `/executions/{id}` | Authenticated | Read/soft-delete execution |
| POST | `/executions/{id}/actions/{action}`, `/executions/{id}/feedback` | Authenticated | Validated lifecycle changes/feedback |
| GET | `/changes` | Authenticated | User-scoped committed change feed |
| POST | `/sync/push` | Authenticated | Idempotent sync operations |
| GET/POST | `/planning/*`, `/days/*`, `/placements/{id}/outcome` | Authenticated, except public capabilities | Existing generation, recurrence, rescheduling, analytics and outcomes |

Resource paths keep their existing names: canonical `ScheduledTask` is
stored in `placements`, and `TaskExecution` in `executions`. Updates use
the existing `PUT` plus `base_version` contract; no incompatible PATCH or
duplicate `/scheduled-tasks` storage is introduced. See [backend.md](backend.md)
and [web-api.md](web-api.md) for complete resource contracts.

Login accepts `{email, password}` or `{username, password}` and returns
`access_token`, `token_type`, `expires_in`, `expires_at`, `refresh_token`,
and `refresh_expires_at`. Existing clients may ignore the additive refresh
fields and continue signing in when their access token expires. The desktop sync adapter automatically rotates refresh credentials. With
**Keep me signed in**, it saves the current refresh credential in the OS
credential store and restores the session after restart. Access tokens remain
in memory. See [desktop accounts](desktop-accounts.md).

Send access tokens as `Authorization: Bearer <token>`. Native credentials
should be held in OS-protected storage (Android Keystore-backed storage,
Windows Credential Manager or the platform equivalent), never in URLs or
logs. Browser clients should use the existing Secure/HttpOnly/SameSite cookie
flow with CSRF checks, rather than storing refresh tokens in browser storage.

Refresh accepts `{refresh_token}`. A token is 256 random bits, stored only
as a SHA-256 digest. A transaction consumes it and issues a replacement.
Sessions have an absolute configured lifetime (default 30 days); rotation
does not extend it. Replaying a consumed credential revokes its entire
session, including access tokens. **Serialize refresh requests.** If a
refresh response is lost, sign in again; do not blindly retry it. Logout
accepts the same body and revokes only the associated session, even when
the supplied token was consumed. Unknown credentials also return 204.

Access tokens from these logins carry a session UUID; each API request
checks its signature, issuer, audience, times, credential epoch and live
session. Password reset invalidates every older session through the epoch.
Legacy access tokens without a session claim remain valid until their
existing expiration or a password reset, for backward compatibility.

## Authorization and abuse controls

The server derives identity from verified credentials. Request schemas
reject ownership and server-version mass assignment. Reads and writes use
the authenticated owner; foreign-owned IDs are indistinguishable from
missing IDs. Composite database foreign keys prevent cross-owner references.
Transactions, optimistic versions and tombstones preserve sync semantics.

Rate limits use the existing shared database counters, including refresh
and logout. CORS uses explicit configurable origins and does not authenticate
clients. Production settings require PostgreSQL and an explicit host list;
remote database connections require TLS. Authentication responses have
`Cache-Control: no-store`. Central error handling omits submitted secrets,
SQL and stack traces. Database engines hide SQL parameter values.

## Local administrator inspection and backups

```powershell
python -m backend.inspect_db --env-file .env.local
python -m backend.inspect_db --env-file .env.local --samples 2
```

This read-only tool reports connectivity, schema revision, table/column
descriptions, counts, foreign keys and orphan counts. Samples contain only
IDs and lifecycle metadata; no private task content or credential hashes.
Exit codes: 0 current and consistent; 1 pending migrations or broken
references; 2 inspection/configuration failure. Keep reports private even
though contents are minimized. Counts are exact and may be expensive on a
large database; PostgreSQL queries have a 30-second statement limit. Use a
read-only database role for additional protection. It never repairs data.

Retain `python -m backend.migrate check` and the existing operator-only
`python -m app.persistence.verify_render --env-file .env.local --check-only`.
For PostgreSQL backups use `pg_dump --format=custom --file=<private-path>`
with `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, TLS settings and a protected
PostgreSQL password file; avoid passwords in command arguments. Restore only
into a separate disposable database first and verify the result. Database
exports are never offered as public API endpoints. Back up SQLite only when
the app is stopped, or with SQLite's online backup API, not by copying a
live WAL database file alone.

## Verification commands

```powershell
python -m pytest
python -m pytest tests/backend
python -m pytest -m dev
python -m ruff check .
python -m compileall -q app backend config tests
```

Real PostgreSQL checks require a disposable `TEST_DATABASE_URL` whose database
name contains `test`. Run `python -m pytest -m postgres tests/backend`, or set
`BACKEND_TESTS_ON_POSTGRES=1` for the shared backend suite. Existing CI runs
both profiles. Production data is never a test fixture.

## Render later (manual deployment only)

Use this same repository and its server dependencies. Configure a Python
Web Service, `pip install -r requirements-backend.txt` as build command, and
`python -m uvicorn backend.main:app --host 0.0.0.0 --port $PORT` as start
command. Run `python -m backend.migrate upgrade` as a separate release step
before traffic. Set `DATABASE_URL` for server-side PostgreSQL, a strong
`JWT_SECRET`, `ENVIRONMENT=production`, `ACCESS_TOKEN_TTL_MINUTES=15`,
`REFRESH_TOKEN_EXPIRE_DAYS=30`, your public `ALLOWED_HOSTS`, and exact
`CORS_ORIGINS`/`ALLOWED_ORIGINS` for any web frontend. Keep secure cookies
enabled and configure trusted proxy networks deliberately. Check `/ready`
after migrations; use `/health` for process liveness. Configure SMTP separately
if recovery is needed. Restrict database access to the backend/operator network.

See [render-deployment.md](render-deployment.md) for the existing handoff;
verify current Render plan/runtime settings when deploying. This task does
not create cloud resources or change a Render dashboard.

The handoff follows Render's official [FastAPI deployment guide](https://render.com/docs/deploy-fastapi),
[deploy lifecycle](https://render.com/docs/deploys), and
[health checks](https://render.com/docs/health-checks), checked on 2026-10-05.

Before public operation, test PostgreSQL concurrency, SMTP delivery, HTTPS
and proxy configuration in staging, configure monitoring and backups, and
test native-client protected credential storage. Expired native-session rows
and their consumed digests should be pruned by an operator retention job
after the session expiration; never prune consumed digests of a live family,
because they are needed for replay detection.
