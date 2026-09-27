# Direct desktop-to-PostgreSQL storage

The desktop app normally stores everything in its local SQLite database and
works offline. **Direct mode** is an optional alternative for private
development: the desktop process connects straight to the server's
PostgreSQL database and uses the same schema, account rules, planning
repository and mutation path as the HTTP API -- in-process, with no FastAPI,
no HTTP, no JWT secret and no server to run.

> **Only for private development.** Direct mode needs the database
> credentials on the machine running the app. Anyone with that `.env` file
> (or the app configured with it) can read and change every account's data.
> Never distribute it to untrusted users; give them the HTTP API instead,
> which uses the same schema and services.

## Install

```bash
python -m pip install -r requirements-direct.txt
```

`requirements-direct.txt` is the desktop runtime plus the database packages
shared with the backend (`requirements-database.txt`: SQLAlchemy, Alembic,
psycopg 3, argon2-cffi, python-dotenv). It does not install FastAPI,
Uvicorn, HTTPX or PyJWT. Offline desktop use still needs only
`requirements-desktop.txt`. Without the packages, opening direct mode fails
with a message naming the missing package.

## Configuration

Direct mode reads one variable, `DATABASE_URL`, from the process environment
or from an env file that an entry point names explicitly (for example
`--env-file .env`). Rules:

- Nothing is read when modules are imported, and there is no search for
  `.env` files in other folders. Ordinary test runs never read `.env`.
- The process environment overrides the env file. Values are used verbatim
  (no `${...}` interpolation), so passwords with `$`, `%` escapes or other
  special characters keep working; the URL object is handed to the driver
  unrendered, so its escaping is preserved.
- `postgres://` and `postgresql://` use the psycopg 3 driver.
- **TLS.** For any host other than `localhost`, `127.0.0.1`, `::1` or a Unix
  socket, the connection must be encrypted. Without `sslmode` the app uses
  `sslmode=require` (in memory; the file is not changed). `require`,
  `verify-ca` and `verify-full` are kept. `disable`, `allow` and `prefer` are
  refused. Use `verify-full` with `sslrootcert=...` when you have the
  server's CA certificate.
- The pool is small (3 connections + 2 overflow), pre-pinged, with a
  10-second connect and pool timeout; SQL parameters are hidden from logs and
  errors and SQL echo is off.
- No `JWT_SECRET` is needed.

Error messages name settings and causes, never the URL, host, user name,
password or SQL parameters. Examples: `DATABASE_URL asks for sslmode=disable
...`, `The database server could not be reached. Check the network
connection, the server's inbound IP allowlist, TLS settings and DATABASE_URL,
then try again.`, `The database refused the credentials in DATABASE_URL.`

## Schema: explicit migrations only

The app checks the schema revision when it connects and refuses to run on a
database that is not at the latest revision. It never migrates by itself.
Migrate explicitly (take a backup of a populated database first; see
[backend.md](backend.md#schema-upgrades)):

```bash
python -m backend.migrate --env-file .env current
python -m backend.migrate --env-file .env upgrade
python -m backend.migrate --env-file .env check
```

`--env-file` goes before the action. With it, the same TLS rules apply;
without it, the command behaves as before (environment only). Expected
failures print one `error: ...` line and exit with code 2, without a
traceback.

## The desktop app in direct mode

```bash
python -m app.app --storage postgres --env-file .env
```

(`SCHEDULE_MAXING_STORAGE=postgres` and `SCHEDULE_MAXING_ENV_FILE=.env` do the same;
`--env-file` is refused with local storage, which reads no env file.)

- The window opens signed out on the Account page and checks the connection and
  schema revision in a background worker. Register or sign in there; **Check
  database** repeats the check.
- After sign-in every page -- Day, Week, Month, Projects, Allocation,
  Productivity, Settings, CSV import/export, generation and execution -- works on
  that account's records through the same controllers as local mode. Storage
  calls run in background workers; widgets are only updated on the Tk thread.
- Signing out (or signing in as someone else) rebuilds the pages. A result of
  work started for the previous account is dropped, and that account's services
  refuse further calls.
- There is no synchronization, outbox, association or conflict review, and no
  local copy. Nothing about your records is stored on the computer; only the
  window's appearance settings are kept in the local data folder.
- If the database cannot be reached, the schema is not current, or a change
  conflicts, the page shows a safe message and nothing is saved -- never in the
  local database instead.
- Closing waits for background work, then signs out and closes the connection
  pool. The local database's single-process lock does not apply.

## Services (for the desktop integration)

```python
from app.persistence import load_direct_settings, open_direct_backend

settings = load_direct_settings(env_file=".env")    # at the entry point only
backend = open_direct_backend(settings)             # one Engine per process; schema checked
backend.register(email=..., password=..., display_name=...)
account = backend.sign_in(email=..., password=...)  # InvalidCredentialsError: same for unknown/wrong
planning = account.planning_service()               # PlanningService (+ app/planning/workflow.py)
executions = account.execution_service()            # the ExecutionService API
productivity = account.productivity_service()       # ProductivityService
account.sign_out()
backend.close()                                     # after background workers finished
```

- **Accounts** use `backend/accounts.py`, the code behind `/auth/register`
  and `/auth/login`: NFKC/case-folded unique emails and usernames (the
  database decides a race), Argon2id hashes upgraded on sign-in, one generic
  failure for unknown accounts and wrong passwords, and an `AccountIdentity`
  result without any password, hash or ORM object. The app keeps no password
  after the call returns (Python cannot promise to erase the caller's string
  from memory).
- **Scope.** An `AccountSession` exists only through `sign_in()` and binds
  every service to that verified account; another user's records behave as
  missing, and after `sign_out()` the services refuse to run.
- **Units of work.** Each operation uses its own Session from the shared
  pool, rolled back and closed when it ends -- after reads and errors too.
  Writes run through `backend/mutations.py` (the per-user lock,
  server versions, one change-log entry per change), so HTTP API clients see
  direct writes in `GET /changes`. `PlanningService.transaction()` groups
  calls into one transaction; nested ones are savepoints. Sessions are
  thread-local, never shared between workers or kept between user actions.
  Scheduling reads its inputs in one transaction, runs the optimizer outside
  any transaction, and saves in a short, re-checked write transaction.
- **Executions** go through the server's execution path, which uses the
  shared transition table and completion metrics (`app/execution/lifecycle.py`)
  and validates session history. Ids are the server's UUIDs; "reset history"
  tombstones the account's executions rather than purging them.

## Verifying a real database

`python -m app.persistence.verify_render` checks the connection (`--check-only`)
or seeds and reads back an idempotent sample (`--write-sample`); see
[render-direct-desktop.md](render-direct-desktop.md). It only runs when invoked.

## Tests

`tests/direct` covers configuration, accounts and services on a file-based
SQLite database by default. On a disposable PostgreSQL database:

```bash
BACKEND_TESTS_ON_POSTGRES=1 TEST_DATABASE_URL=postgresql://USER@localhost:5432/schedule_maxing_test python -m pytest tests/direct
```

`TEST_DATABASE_URL` must name a database whose name contains `test`; it is
never taken from `DATABASE_URL` or `.env`.
