# Schedule Maxing backend

`backend/` holds the server (Milestone 3). It is a FastAPI application with
PostgreSQL persistence. It stores user-scoped, versioned copies of the
desktop's records, as described in [sync-contract.md](sync-contract.md).

The desktop app never imports the backend and keeps working offline without
any backend setting. The backend in turn imports only the pure canonical
models (`app/planning/models.py`, `preferences.py`, `provenance.py`,
`app/execution/models.py`, `lifecycle.py`). It never imports Tk, the local
SQLite layer, pandas, or scikit-learn. A test checks this.

## Package choices

All packages are maintained and support Python 3.10 (the CI version):

| Concern | Package | Why |
| --- | --- | --- |
| HTTP API and OpenAPI | FastAPI (+ Starlette), Uvicorn | Typed request validation with the pydantic v2 models the app already uses |
| Database access | SQLAlchemy 2.0 | ORM and core; PostgreSQL in production, SQLite for the ordinary test suite |
| PostgreSQL driver | psycopg 3 (`psycopg[binary]`) | The maintained PostgreSQL driver |
| Migrations | Alembic | Versioned migration scripts in `backend/migrations/versions` |
| Password hashing | argon2-cffi | Argon2id with a random salt per hash; hashes are upgraded automatically on login |
| Access tokens | PyJWT | HS256 only, with every required claim verified |

The server-only dependencies are listed in `requirements-backend.txt`, which
has no desktop packages. `requirements.txt`, used for development and CI,
includes it so the backend tests run everywhere.

## Configuration

The backend reads its settings only from environment variables. See
[.env.example](../.env.example), which contains placeholders only.
`backend/settings.py` validates them.

- **Required:**
  - `DATABASE_URL`: the server database.
  - `JWT_SECRET`: at least 32 characters.
- **Optional:**
  - `JWT_ISSUER`
  - `JWT_AUDIENCE`
  - `ACCESS_TOKEN_TTL_MINUTES` (1–1440, default 60)
  - `API_MAX_PAGE_SIZE` (default 500)

If a setting is missing or invalid, startup fails with `BackendConfigError`.
The error names the problem settings, never their values. Nothing logs
passwords, tokens, `Authorization` headers, or configuration values, and
validation errors never echo submitted values.

## Running it locally

```bash
python -m pip install -r requirements.txt
# Set DATABASE_URL and JWT_SECRET in your shell (see .env.example), then:
python -m backend.migrate upgrade          # apply migrations (needs only DATABASE_URL)
python -m backend.migrate check            # exit code 0 only when the database is at head
uvicorn --factory backend.app:create_app --host 127.0.0.1 --port 8000
```

Use `--factory` so that importing the module never reads configuration.

- `GET /health` is liveness. It never touches the database.
- `GET /ready` returns 200 only when the database is reachable and at the
  latest migration. Otherwise it returns 503 with the reason.
- The interactive OpenAPI docs are served at `/docs`.

## API summary

All endpoints except `/health`, `/ready`, `/auth/register`, and
`/auth/login` need an `Authorization: Bearer <access token>` header.

**Accounts**

- `POST /auth/register` takes `{email, password, username?, display_name?}`.
  Emails and usernames are normalized (NFKC, trimmed, case-folded), and the
  database's unique constraints enforce uniqueness. A duplicate, including
  one from two concurrent registrations, gets `409 account_exists`.
- `POST /auth/login` takes `{email | username, password}` and returns
  `{access_token, token_type, expires_in, expires_at}`. Bad credentials get
  one generic `401`.
- `GET /me` returns the profile. `PATCH /me` takes
  `{base_version, display_name}`.

**Tokens.** A token is accepted only if:

- its HS256 signature is valid (no other algorithm is accepted);
- it has every required claim: `exp`, `iat`, `nbf`, `sub`, `iss`, `aud`,
  `jti`, `typ`;
- its issuer and audience match the configured values;
- `typ` is `access`;
- it has not expired, judged by the server clock;
- `sub` names an existing user.

The user id comes **only** from the verified token.

**Resources.** The same endpoints exist for each of: `projects`, `tasks`,
`fixed-blocks`, `placements`, `preferences` (user and per-date layers,
including `optimizer_mode`), and `schedule-generations` (schedule
provenance and freshness).

| Method and path | Purpose |
| --- | --- |
| `GET /<resource>?limit=&cursor=&include_deleted=` | List; bounded, keyset-paginated |
| `GET /<resource>/{id}` | Read one record |
| `POST /<resource>` | Create; the client may choose the UUID `id` |
| `PUT /<resource>/{id}` | Update; takes the full content plus `base_version` |
| `DELETE /<resource>/{id}?base_version=N` | Soft delete; returns the tombstone |

**Executions.**

- `POST /executions` uploads a whole aggregate, including sessions, as long
  as it is consistent.
- `POST /executions/{id}/actions/{start|pause|resume|complete|skip|cancel}`
  takes `{base_version, at?}`.
- `POST /executions/{id}/feedback` takes `{base_version, ...}`.
- `DELETE /executions/{id}?base_version=N` soft-deletes the execution.

There is no generic update, so snapshots, references, and past sessions
cannot be overwritten. Transitions and completion metrics are the desktop's
own (`app/execution/lifecycle.py`). An execution uploaded from history whose
task or placement was never persisted sets `historical_reference: true`.
Its ids are then kept as history and never resolved, and no parent record
is invented for them.

**Change feed.** `GET /changes?after=<seq>&limit=` returns the caller's
accepted changes in commit order. Each entry carries the complete record,
or the tombstone for a delete.

### Errors

Every error has the same shape:
`{"error": {"code", "message", ...}}`.

| Status | When |
| --- | --- |
| `404 not_found` | The record doesn't exist, **or it belongs to another user**. The two cases are indistinguishable. |
| `409 version_conflict` or `409 deleted` | A stale `base_version`, or the target is a tombstone. The body includes `supplied_version`, `current_version`, and the caller's own `current` record or tombstone. |
| `409` | `already_exists`, `in_use`, `invalid_transition`, `account_exists` |
| `422` | `validation_error` or `invalid_reference`. References to another user's records are rejected the same way as references to records that don't exist. |
| `401 unauthenticated` | Missing or bad credentials. The response includes `WWW-Authenticate: Bearer`. |

## Data model, ownership, and concurrency

- **Ownership.** Every user table's primary key is `(user_id, id)`, and
  every relationship is a composite foreign key that includes `user_id`.
  The database itself therefore makes cross-user references impossible,
  and a client-chosen UUID can neither collide with nor reveal another
  user's record.
- **Versions and audit fields.** On create, the server sets `version = 1`
  and `created_at`/`updated_at` from its UTC clock. Each accepted change
  adds 1 to `version`. A change that alters nothing is accepted without a
  new version. Clients cannot send `version`, `user_id`, or audit fields;
  unknown fields are rejected.
- **Optimistic concurrency.** Every update and delete needs `base_version`.
  It is compared while the user's change-log lock is held (below), and
  again by SQLAlchemy's `version_id_col`, which adds
  `AND version = <loaded version>` to every `UPDATE`.
- **Soft deletion.** Deleted records are kept as tombstones and never
  physically removed. Deletes follow the local contract's relationship
  policies:
  - a project with live tasks, or a task other live tasks depend on, cannot
    be deleted;
  - deleting a task tombstones its live placements;
  - execution history never cascades.

### The shared mutation path and change ordering

Every accepted mutation goes through `backend/mutations.py`'s `Mutator`: the
REST endpoints now, and batch sync in the next milestone step. In one
transaction it:

1. takes the user's change-log lock:
   `UPDATE users SET change_seq = change_seq WHERE id = :user`. This is a
   row lock on PostgreSQL and the database write lock on SQLite, held until
   commit or rollback;
2. checks the precondition and applies the change;
3. appends one `change_log` row per changed record, with sequence numbers
   taken from `users.change_seq`. Compound changes (a task delete and its
   placement tombstones, or an execution action with its session) get one
   entry per record;
4. writes the counter back and commits.

Because a second mutation for the same user cannot allocate a sequence
number until the first commits or rolls back, each user's sequence numbers
are gap-free and in commit order. A reader who has everything up to `N` can
continue from `after=N` and never skip a change that commits later. The
feed never orders by `updated_at`. `tests/backend/test_postgres.py` checks
this on real PostgreSQL with concurrent transactions. Every feed is per
user, so no global order is needed.

## Tests

- `python -m pytest tests/backend` runs the whole API against in-memory
  SQLite databases created by the real migrations. No server is needed.
- **Real PostgreSQL** (optional). Use a disposable database whose name
  contains `test`; any other name is refused. Each run works in a private
  schema that is dropped afterwards.

  ```bash
  TEST_DATABASE_URL=postgresql://USER@localhost:5432/schedule_maxing_test python -m pytest -m postgres
  BACKEND_TESTS_ON_POSTGRES=1 TEST_DATABASE_URL=postgresql://USER@localhost:5432/schedule_maxing_test python -m pytest tests/backend
  ```

  The first command runs the PostgreSQL-specific tests: types, partial
  unique and composite-key constraints, concurrent registrations, and
  change-log ordering under concurrent transactions. The second runs every
  backend test on PostgreSQL.

SQLite-backed test runs are **not** PostgreSQL verification.
