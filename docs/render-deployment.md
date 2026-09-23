# Deploying the backend to Render (manual handoff)

This guide prepares a **manual** deployment of the server backend
(`backend/`, see [backend.md](backend.md)) to Render. Nothing in this
repository has been deployed, and no Render account, service, or database
was created or contacted while writing it.

The details below were checked against Render's public documentation in
September 2026:

- Web services must bind to `0.0.0.0` on `$PORT` (default 10000).
- Health checks count any 2xx/3xx response within 5 s as healthy.
- The pre-deploy command is available on paid plans only.
- `PYTHON_VERSION` must be a full version.
- Internal and external database URLs have the form `postgresql://…`, and
  external connections require TLS.

Recheck these on render.com when you deploy, because platform details change.

Only the backend is deployed. The desktop app stays a local program. It
talks to the backend only when you configure a backend URL and sign in
([sync-protocol.md](sync-protocol.md)).

## What Render runs

| Setting | Value |
| --- | --- |
| Runtime | Python |
| Python version | Environment variable `PYTHON_VERSION=3.10.11`. It must be the full version. CI and local development use Python 3.10; Render's default is newer. |
| Build command | `pip install -r requirements-backend.txt` |
| Pre-deploy command (paid plans) | `python -m backend.migrate upgrade` |
| Start command | `uvicorn --factory backend.app:create_app --host 0.0.0.0 --port $PORT` |
| Start command on a free web service (no pre-deploy command) | `python -m backend.migrate upgrade && uvicorn --factory backend.app:create_app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |

About these settings:

- **Dependencies.** `requirements-backend.txt` contains only server
  dependencies: FastAPI, Uvicorn, SQLAlchemy, Alembic, psycopg 3,
  argon2-cffi, PyJWT, and pydantic. It has no desktop UI, pandas, or
  scikit-learn.
- **`--factory`.** Configuration is read when the app is created, not at
  import. Missing or invalid settings stop startup with an error that names
  the setting but never its value.
- **`/health` vs. `/ready`.** `/health` is liveness and never touches the
  database. `/ready` is separate: it returns 200 only when the database is
  reachable and fully migrated, and 503 otherwise (with the reason). Use
  `/ready` for your own checks after a deploy. Keeping `/health` as Render's
  health check means a short database outage does not get instances
  restarted in a loop.

## Environment variables

Set these only in Render's **Environment** settings, never in files in the
repository. [.env.example](../.env.example) lists them with placeholders.

| Variable | Required | Value |
| --- | --- | --- |
| `DATABASE_URL` | yes | The Render Postgres **Internal Database URL** (`postgresql://USER:PASSWORD@HOST/DB`). The backend switches it to the psycopg 3 driver itself. To require TLS on the internal network, append `?sslmode=require`. An **external** URL must use `?sslmode=require`. |
| `JWT_SECRET` | yes | At least 32 random characters, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Changing it signs everyone out. |
| `PYTHON_VERSION` | yes (see above) | `3.10.11` |
| `JWT_ISSUER` | no | Default `schedule-maxing` |
| `JWT_AUDIENCE` | no | Default `schedule-maxing-api` |
| `ACCESS_TOKEN_TTL_MINUTES` | no | 1–1440, default 60 |
| `API_MAX_PAGE_SIZE` | no | Default 500 |

The backend never reads a `.env` file and never logs passwords, tokens,
`Authorization` headers, `DATABASE_URL`, or `JWT_SECRET`.

## Migrations: order and failure handling

- **Order.** Migrations are versioned Alembic scripts in
  `backend/migrations/versions`, applied in order by
  `python -m backend.migrate upgrade`. That command reads only
  `DATABASE_URL`. On PostgreSQL each migration runs in a transaction, so a
  failed migration rolls back and the database stays at the previous
  revision.
- **Paid plans.** The pre-deploy command runs after the build, before the
  new version receives traffic. If it fails, Render aborts the deploy and
  the previous version keeps serving.
- **Free plan.** Migrations run at the start of the start command. If they
  fail, the new instance does not start and the deploy fails health checks.
- **Checking.** `python -m backend.migrate check` exits 0 only when the
  database is at the latest revision. `GET /ready` reports the same thing
  over HTTP.
- **Compatibility.** Keep schema changes backward compatible with the
  version that is still running: add first, remove later.

## Backups and rollback

- **Backups.**
  - Paid Render Postgres has point-in-time recovery and on-demand logical
    exports. Take an export before any deploy that includes a migration.
  - **Free Render Postgres has no backups and expires 30 days after
    creation.** Use it only for trying things out, never for data you want
    to keep.
- **Rolling back code.** Redeploy the previous commit. It keeps working as
  long as the schema changes were additive.
- **Rolling back the schema.** Downgrades are not run automatically.
  Restore from a backup, or run a specific Alembic downgrade only after
  checking it does not drop data you need.
- **Free web services.** They spin down after 15 minutes without traffic,
  so the first desktop sync afterwards may time out. The client treats that
  as a transient failure and retries with backoff.

## Connecting a desktop

On the desktop, set `SCHEDULE_MAXING_BACKEND_URL=https://<your-service>.onrender.com`
before starting the app. With it unset, the app stays offline. There is no
sign-in screen yet (Milestone 4). Until then, use the service API from a
Python shell in the checkout. It works on the same local database the app
uses, so close the desktop app first:

```python
from app.ui.app_services import open_app_services
services = open_app_services()                    # uses SCHEDULE_MAXING_BACKEND_URL
sync = services.sync_service
sync.sign_in("you@example.com", "your password")  # the token stays in memory only
sync.associate_local_data()                       # explicit: claims this device's existing records
print(sync.sync_now())                            # SyncReport(status='ok', ...)
print(sync.list_conflicts())
services.close()
```

## Manual checklist

Do these yourself, in order. None of them was performed while preparing this
guide.

1. **Create the database.** In the Render dashboard, choose
   **New → Postgres**. Pick a name, a region, a PostgreSQL version (17 is
   used by this project's tests), and a plan (paid if the data matters; see
   Backups). Wait until it is *Available*, then copy its **Internal Database
   URL**.

2. **Create the web service.** Choose **New → Web Service** and connect
   this repository. Select the branch to deploy, the **same region** as the
   database, and the **Python** runtime. Then:
   - Build command: `pip install -r requirements-backend.txt`
   - Pre-deploy command (paid plans): `python -m backend.migrate upgrade`
   - Start command: `uvicorn --factory backend.app:create_app --host 0.0.0.0 --port $PORT`.
     On a free web service, use
     `python -m backend.migrate upgrade && uvicorn --factory backend.app:create_app --host 0.0.0.0 --port $PORT`
     instead.
   - Health check path: `/health`

3. **Enter the environment variables** under the service's **Environment**
   settings:
   - `DATABASE_URL`: the Internal Database URL from step 1.
   - `JWT_SECRET`: generate one locally with
     `python -c "import secrets; print(secrets.token_urlsafe(48))"` and paste
     it. Do not commit it.
   - `PYTHON_VERSION`: `3.10.11`.
   - Leave the optional variables at their defaults unless you need to
     change them.

4. **Deploy.** Watch the logs: the build, then
   `Database is at revision …` from the migration, then Uvicorn listening on
   `0.0.0.0:$PORT`.

5. **Verify health, readiness, and the database** from your own machine.
   Replace the placeholder URL:

   ```bash
   curl -sS https://YOUR-SERVICE.onrender.com/health
   curl -sS https://YOUR-SERVICE.onrender.com/ready
   ```

   You should see `{"status":"ok"}`, and a `ready` response with
   `"migrations":"current"`.

6. **Verify authentication with a throwaway account**, e.g.
   `test-account@example.com`:

   ```bash
   curl -sS -X POST https://YOUR-SERVICE.onrender.com/auth/register -H "Content-Type: application/json" -d "{\"email\": \"test-account@example.com\", \"password\": \"CHOOSE-A-TEST-PASSWORD\"}"
   curl -sS -X POST https://YOUR-SERVICE.onrender.com/auth/login -H "Content-Type: application/json" -d "{\"email\": \"test-account@example.com\", \"password\": \"CHOOSE-A-TEST-PASSWORD\"}"
   curl -sS https://YOUR-SERVICE.onrender.com/me -H "Authorization: Bearer PASTE-THE-ACCESS-TOKEN"
   curl -sS https://YOUR-SERVICE.onrender.com/me
   ```

   Expected results, in order: `201`, a token, your profile, and `401`
   without a token. Do not paste real tokens into shared places.

7. **Connect a test desktop.** Copy the test database first, or point
   `SCHEDULE_MAXING_DATA_DIR` at an empty folder. Then set
   `SCHEDULE_MAXING_BACKEND_URL` and run the Python snippet in
   [Connecting a desktop](#connecting-a-desktop) with the test account.
   Create a task on the desktop, run `sync.sync_now()`, and check that it
   appears in `GET /tasks` with the test token.

8. **Afterwards:**
   - Delete the test account's data if you do not need it; there is no
     account deletion endpoint yet, so this is done at the database level.
   - Keep `JWT_SECRET` only in Render.
   - Set a database backup routine on paid plans.

## Verifying locally before deploying

The same commands work against a local PostgreSQL. Use the disposable one in
`docker-compose.postgres-test.yml`, or any local server, with a database
whose name contains `test`:

```bash
docker compose -f docker-compose.postgres-test.yml up -d
# On Windows PowerShell, set variables with $env:NAME="value" instead of NAME=value.
DATABASE_URL=postgresql://sm_test@127.0.0.1:55432/schedule_maxing_test python -m backend.migrate upgrade
DATABASE_URL=postgresql://sm_test@127.0.0.1:55432/schedule_maxing_test JWT_SECRET=local-only-secret-at-least-32-characters-long uvicorn --factory backend.app:create_app --host 127.0.0.1 --port 8000
TEST_DATABASE_URL=postgresql://sm_test@127.0.0.1:55432/schedule_maxing_test python -m pytest -m postgres
TEST_DATABASE_URL=postgresql://sm_test@127.0.0.1:55432/schedule_maxing_test BACKEND_TESTS_ON_POSTGRES=1 python -m pytest tests/backend tests/sync
docker compose -f docker-compose.postgres-test.yml down -v
```
