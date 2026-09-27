# Schedule Maxing backend

`backend/` holds the server (Milestone 3). It is a FastAPI application with
PostgreSQL persistence. It stores user-scoped, versioned copies of the
desktop's records, as described in [sync-contract.md](sync-contract.md).

The desktop app never imports the backend and keeps working offline without
any backend setting. The backend in turn imports only the pure canonical
models (`app/planning/models.py`, `preferences.py`, `provenance.py`,
`fixed_block_rules.py`, `app/execution/models.py`,
`lifecycle.py`) and the reward-settings loader the YAML template needs. It never imports Tk, the local
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
| Preference template | PyYAML, tzdata | Server-side preference resolution reads `config/task_preference.yaml` like the desktop; tzdata supplies IANA zones where the host has none |

The server-only dependencies are listed in `requirements-backend.txt` (which
includes `requirements-database.txt`, the database packages shared with the
optional direct desktop mode, [direct-postgres.md](direct-postgres.md)), and it
has no desktop packages; the desktop-only ones are in
`requirements-desktop.txt`, which has no server packages. `requirements.txt`,
used for development and CI, includes both so every test runs everywhere.

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
  - `BROWSER_SESSION_TTL_MINUTES` (5–43200, default 720)
  - `BROWSER_COOKIE_SECURE` (default `true`; `false` only for plain-http
    local development)
  - `ALLOWED_ORIGINS` (comma-separated extra origins whose cookie
    requests are accepted, e.g. a separate dev frontend)

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

The web UI's scheduling operations (snapshot, preferences view, allocation
preview, generation, reset, canonical CSV) and browser sessions are
documented in [web-api.md](web-api.md). They run the desktop's own planning
code -- `PlanningService` and `app/planning/workflow.py` -- over the server
tables through `backend/planning_repository.py`, whose writes all go
through the `Mutator` and the change log below.

All endpoints except `/health`, `/ready`, `/auth/register`, `/auth/login`,
the `/auth/browser/*` session endpoints and `/planning/capabilities` need
either an `Authorization: Bearer <access token>` header or a browser
session cookie (with `X-CSRF-Token` on unsafe requests; see web-api.md).

**Accounts**

Registration and sign-in are implemented once, framework-free, in
`backend/accounts.py` (`AccountService`); the routes below and the direct
desktop mode both call it. Password hashing and identifier normalization are
in `backend/passwords.py` (no JWT dependency; `backend/security.py`
re-exports them and adds the tokens). `backend/errors.py` (`ApiError`) is
framework-free as well; the FastAPI handlers that render it are in
`backend/http_errors.py`.

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
is invented for them. Wherever an execution's ids do resolve to the user's
own task (and a placement of that task), the database also stores them as
enforced references (see "Storage model" below).

**Change feed.** `GET /changes?after=<seq>&limit=` returns the caller's
accepted changes in commit order. Each entry carries the complete record,
or the tombstone for a delete, exactly as it was at that change (an
immutable snapshot, not the current row).

### Errors

Every error has the same shape:
`{"error": {"code", "message", ...}}`.

| Status | When |
| --- | --- |
| `404 not_found` | The record doesn't exist, **or it belongs to another user**. The two cases are indistinguishable. |
| `409 version_conflict` or `409 deleted` | A stale `base_version`, or the target is a tombstone. The body includes `supplied_version`, `current_version`, and the caller's own `current` record or tombstone. |
| `409` | `already_exists`, `in_use`, `invalid_transition`, `account_exists` |
| `422` | `validation_error` or `invalid_reference`. References to another user's records are rejected the same way as references to records that don't exist. A fixed block that breaks a write invariant is a `validation_error` with a `reason` (`sub_minute_precision`, `date_mismatch`, `outside_day_window`, `unsupported_day_window`, `invalid_interval`). |
| `409 fixed_block_overlap` | The fixed block overlaps another live block of the caller; `conflicting` is that block (never `current`, which always means the record itself). |
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
- **Fixed-block invariants.** Creating or updating a fixed block checks,
  under the user's change-log lock, the rules in
  `app/planning/fixed_block_rules.py`: a whole-minute interval that starts
  on its date in its timezone, inside that date's effective day window
  (`backend/preferences.py`: template -> the user's user layer -> the user's
  date layer), and no overlap with another live block of the user (the
  edited block excluded). Sync push uses the same path, so a pushed block
  cannot bypass them. An update that keeps the interval unchanged is not
  re-judged, so older blocks are never rewritten. Concurrent overlapping
  writes serialize on the lock: at most one succeeds (checked on real
  PostgreSQL in `tests/backend/test_postgres.py`).

### Storage model

`backend/models.py` is the one schema of the server and of the planned
direct desktop adapter. Structured content is stored relationally, never as
a JSON or text document:

- **Tasks.** Tags, preferred dates and dependency ids are ordered child rows
  (`task_tags`, `task_preferred_dates`, `task_dependencies`), so a list is
  kept exactly, repeated values included. Recurrence is scalar columns
  (`recurrence_frequency`, `_interval`, `_day_of_month`, `_end_date`,
  `_count`) plus `task_recurrence_weekdays` (a set, as the canonical model
  keeps it) -- the desktop SQLite layout. A deadline keeps its original UTC
  offset (`deadline`) next to its UTC twin (`deadline_utc`).
- **Preference layers.** The day window and every reward field are nullable
  scalar columns (NULL = this layer does not override it). Category
  multipliers and preferred windows are `preference_category_*` rows: a row
  is a present key, a row with NULL values is an explicit clear, no row is
  an absent key. `reward_tag_relations_present` distinguishes an absent
  `tag_relations` from a present, possibly empty, mapping
  (`preference_tag_relations` and ordered `preference_related_tags`; a tag
  may relate to an empty list). Mapping keys read back in sorted order.
- **Placement extension metadata.** `optimization_metadata` is the only JSON
  column: a small, genuinely unstructured object the wire contract and the
  desktop allow on a placement. Greedy Optimizer v1 writes none; established
  fields would become columns. Its compact JSON is limited to 4096 bytes and
  may not use keys such as `tasks`, `placements` or `schedule`
  (`backend/record_mapping.py`); PostgreSQL additionally checks that it is an
  object of bounded size for every writer. A task list or schedule is never
  stored in it.
- **History.** Every change-log entry references an immutable record
  revision: a `record_revisions` header (the record's id, version, audit
  fields and tombstone) plus one typed table per entity type with the same
  content columns and child-row layout as the live table (so both use the
  same field mapping), e.g. `task_revisions` + `task_revision_tags`, and
  `execution_revisions` + `execution_revision_sessions` for work-session
  histories. The feed's JSON is built from them at the serialization
  boundary (`backend/snapshots.py`).
- **Sync outcomes.** `sync_operations` stores the status, the error code,
  message and details as columns (the version pair, the fixed-block
  `reason`, a group's `failed_op_id`), validation problems as
  `sync_operation_problems` rows, and references to the immutable
  revisions of the applied `record` or the error's `current` /
  `conflicting` record. A retry therefore answers exactly the same even
  after the record was edited later, and the push response itself is built
  from what was recorded. An error detail the schema cannot store is
  refused loudly rather than dropped.
- **Executions.** `task_id` / `scheduled_task_id` stay the historical
  identity (possibly unresolved). `linked_task_id` / `linked_placement_id`
  are the same ids when they resolve to the user's own task and a placement
  **of that task** -- composite foreign keys to `tasks (user_id, id)` and
  `placements (user_id, task_id, id)` -- and are required unless
  `historical_reference` is set. Tasks and placements are only ever
  tombstoned, so linked history stays attached to them. The execution's
  name/category/tag/priority/planned snapshot is kept as the audit record
  analytics need.

Revision rows and outcomes are written through the same `Mutator`
transaction as the change they describe; a rolled-back group leaves none.
Reads load child rows per page (SQLAlchemy `selectin`, one query per child
kind per up to 500 parents), never per record.

**Indexes added for queries** (besides the primary keys, which lead with
`(user_id, parent id)` and serve every child-row load):

| Index | Query it supports |
| --- | --- |
| `executions (user_id, task_id)` | `ServerPlanningRepository.execution_facts_for_tasks`: `WHERE user_id = ? AND task_id IN (...)` |
| unique `placements (user_id, task_id, id)` | the target of the enforced execution-to-placement reference; also serves placements-by-task queries (task-delete cascade, `active_placements_for_tasks`), so it replaces `ix_placements_user_task` |

No index was added for execution status or date, eligible-task dates, tags
or preference lookups: no server query filters on them beyond the existing
user-prefixed keys (`uq_preferences_live_scope` already serves the
preference lookups).

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

## Schema upgrades

`python -m backend.migrate upgrade` applies every pending revision in **one
transaction**: if any step fails, the database stays at the revision it had.
On SQLite (tests) the transaction is explicit and foreign keys are checked
before the commit.

### Normalized storage (0004-0006)

Revisions 0004-0006 move the structured JSON of revision 0003 into the
storage model above:

1. **0004 expand** -- creates the child, revision and outcome tables and the
   new nullable columns, foreign keys and indexes. Existing values are not
   touched.
2. **0005 backfill** -- converts `tasks.tags / preferred_dates / recurrence`,
   `preferences.overrides`, every `change_log.payload` (tombstones and
   execution session histories included) and every `sync_operations.result`
   (applied, conflict with `current`/`conflicting`, rejected with or without
   problems), and sets the execution links, in keyset-paginated batches of
   500 rows so memory stays bounded. UUIDs, versions, timestamps,
   tombstones, deadline offsets, ownership, `users.change_seq` and op ids
   are kept as they are.
3. **0006 validate and contract** -- rebuilds every original JSON value from
   the rows actually stored and compares it (instants as instants), checks
   the child-row counts, the placement metadata bound, and that every
   non-historical execution is linked; only then adds the new constraints
   and drops the six JSON columns and the redundant `ix_placements_user_task`.

Data that cannot be converted exactly (for example tags that are not a list
of strings, metadata over the bound, or a non-historical execution whose
task does not exist) stops the upgrade with an error naming the table, the
row's key and the field -- never the value -- and rolls everything back.
Nothing is deleted, rounded or de-duplicated to make it fit. The conversion
code (`backend/migrations/normalized_storage.py`) imports nothing from the
application, so the migrations keep their meaning if the models change.

**Writers must stop.** 0006 removes columns the previous application version
reads and writes, so this upgrade is not backward compatible with a running
older server: stop (or suspend) every process of the previous version before
the upgrade starts, run the upgrade, then start this version. Take a backup
or logical export first.

**Downgrades** rebuild the JSON columns losslessly from the relational rows,
but are for tests and disposable databases only: never run
`backend.migrate` downgrades against a real database; restore a backup
instead.

## Tests

- `python -m pytest tests/backend` runs the whole API against in-memory
  SQLite databases created by the real migrations. No server is needed.
- `tests/backend/test_normalized_migration.py` upgrades a *populated*
  revision-0003 database (every JSON shape, tombstones, histories, recorded
  sync outcomes) and checks that every record, change and sync replay reads
  back the same; `tests/backend/test_normalized_storage.py` covers the
  storage model itself (enforced ownership and references, exact lists and
  preference states, rollback, bounded queries for 1000 tasks, and a schema
  check that forbids structured JSON/text blobs).
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
