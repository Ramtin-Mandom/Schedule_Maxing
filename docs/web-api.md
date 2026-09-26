# Web application API (Milestone 4)

This is the contract the web frontend is built against. It is served in two
deployment profiles with the same planning and record endpoints:

| Profile | Process | Storage | Authentication |
| --- | --- | --- | --- |
| **hosted** | `uvicorn --factory backend.app:create_app` | PostgreSQL on the server | browser session cookie (or bearer token) |
| **local** | `python -m app.web` on this device, loopback only | this device's SQLite database (the desktop app's), synchronized by the existing sync client | the local session (see [Local profile](#local-profile)) |

`GET /planning/capabilities` says which one a page is talking to. The hosted
profile is below; the local profile's differences and its `/local/...`
endpoints are at the end.

The hosted profile is served by the backend (FastAPI + PostgreSQL,
[backend.md](backend.md)). The exact
request and response schemas are also published as OpenAPI at `/openapi.json`
(interactive docs at `/docs`); the names below (`GenerateOut`, ...) are the
schema names there.

Everything scheduling-related is computed on the server by the same Python
code the desktop uses (`app/planning/workflow.py` over `PlanningService`):
one allocator, one day engine, one preference resolution, one set of
occurrence rules, one provenance recipe. The frontend never schedules.

## Starting the server

```bash
python -m pip install -r requirements-backend.txt   # server-only dependencies
# Set DATABASE_URL and JWT_SECRET (see .env.example), then:
python -m backend.migrate upgrade
uvicorn --factory backend.app:create_app --host 127.0.0.1 --port 8000
```

For plain-http local development of the web UI set `BROWSER_COOKIE_SECURE=false`
(cookies are otherwise `Secure`), and if the frontend is served from another
origin list it in `ALLOWED_ORIGINS` (e.g. `http://localhost:5173`).

## Authentication

Two ways to authenticate; every endpoint below accepts either.

| Client | How | CSRF |
| --- | --- | --- |
| Browser | `POST /auth/browser/login` sets the `sm_session` cookie (HttpOnly, SameSite=Strict, Secure) | Every unsafe request (POST/PUT/PATCH/DELETE) sends `X-CSRF-Token`; a present `Origin` must be the server's own or listed in `ALLOWED_ORIGINS` |
| Desktop sync, scripts | `POST /auth/login` returns a bearer token; send `Authorization: Bearer ...` | Not needed |

- `POST /auth/browser/login` `{email | username, password}` -> `BrowserSessionOut`
  `{authenticated, user, csrf_token, expires_at}`. Wrong credentials: one generic 401.
- `GET /auth/browser/session` -> `BrowserSessionOut` (never 401; `authenticated: false`
  when signed out). Use it on page load to learn whether the user is signed in and
  to get the CSRF token again.
- `POST /auth/browser/logout` (with `X-CSRF-Token`) -> 204. The session is revoked on
  the server, so the cookie stops working immediately.
- `POST /auth/register` `{email, password, username?, display_name?}` -> 201 `UserOut`.
- `GET /me`, `PATCH /me` `{base_version, display_name}`.

Rules for the frontend: keep the CSRF token in memory only; never put the
password, a bearer token or the CSRF token in `localStorage`,
`sessionStorage`, a URL or a log. There is no token refresh: when a session
expires (`BROWSER_SESSION_TTL_MINUTES`, default 12 hours) the user signs in
again.

Auth errors: `401 unauthenticated` (signed out, unknown or revoked session,
bad token), `401 session_expired`, `403 csrf_failed`, `403 origin_not_allowed`.

## Records

Projects, tasks, fixed blocks and preference layers are created and edited
through the record endpoints described in [backend.md](backend.md):
`/projects`, `/tasks`, `/fixed-blocks`, `/preferences` (and `/executions`).
Every update and delete sends `base_version` (the version it read); a stale
one is `409 version_conflict` with the `current` record. Fixed blocks must be
whole-minute intervals inside their date's day window and must not overlap
(`422 validation_error` with a `reason`, or `409 fixed_block_overlap` with the
`conflicting` block).

## Planning operations

All dates are ISO dates; `timezone` is the IANA zone the dates are planned in
(typically the browser's). A range spans at most 62 days (`max_range_days`).
`scope` is `planned` (default: tasks planned on these dates plus eligible
undated ones) or `eligible` (every task that could go on these dates).

### `GET /planning/capabilities` -> `CapabilitiesOut`

`profile` (`hosted`), `persistence` (`server`: accepted changes are stored on
the server directly), `reports_device_pending_changes` (`false`: the hosted
server cannot see changes still waiting on a desktop device; those arrive when
that device synchronizes -- the page must not show local pending counts),
`engines`, `generation_modes`, `max_range_days`, `csv_format_version`,
`max_csv_bytes`, `auth`. No authentication needed.

### `GET /planning/snapshot?start_date&end_date&timezone&scope` -> `SnapshotOut`

`tasks` (the range's tasks plus the tasks of its placements, `TaskOut`),
`projects`, `fixed_blocks`, `placements`, and `days`: one `DayStateOut` per
date:

- `status`: `none` (nothing saved), `current` (the saved schedule matches
  today's inputs), `stale` (it does not; `stale_reason` is `no_provenance`,
  `inputs_changed` or `placements_changed`);
- `generated_at`, `engine_mode`, `range_start`/`range_end` (the allocation
  range it was generated from), `placement_count`, `total_score`;
- `unscheduled_count`: how many tasks that generation could not place. Their
  *reasons* are not stored, so after the generation response they are gone;
  the page shows the count, never invented reasons.

### `GET /planning/preferences?start_date&end_date&timezone` -> `PreferencesOut`

`template` (the read-only YAML layer), `user_layer` and `date_layers`
(`PreferenceOut`, edit them through `/preferences` with `base_version`), and
per date `effective` (defaults -> template -> user layer -> date layer, exactly
what scheduling uses) and `inherited` (the same without the date layer, i.e.
what deleting that layer would give), plus the `engines`.

### `POST /planning/allocation/preview` `RangeIn` -> `AllocationPreviewOut`

Assigns tasks to dates from the persisted inputs. **Never** generates or saves
placements, and nothing is kept on the server. Returns `fingerprint` (of every
input read), `assignments` (task -> date), `unallocated` (with `reason_code`,
`explanation`, `required`, `proven_infeasible`), `capacity` (free minutes left
per date), `diagnostics`, and the `days` states.

### `POST /planning/generate` `GenerateIn` -> `GenerateOut`

Body: the range (`start_date`, `end_date`, `timezone`, `scope`), optionally
the dates to generate (`generate_start`/`generate_end`, a contiguous part of
the range; default the whole range), `mode`, and `expected_fingerprint` (the
preview's fingerprint: if the inputs changed since, `409 inputs_changed`).

- If every generated date is already `current`, the response is
  `status: already_current` and **nothing is written** (no id, version,
  timestamp, provenance or change-feed entry changes). Its `unscheduled` is
  `null` (unknown), `unscheduled_count` comes from the saved record.
- `mode: full` (explicit regeneration): each date is generated from scratch;
  an unchanged placement keeps its id. Work whose execution has started or
  finished is never moved or duplicated: it stays exactly where it is and the
  rest is scheduled around it. Replaced placements of the same occurrence on
  other dates are listed in `superseded_placement_ids`
  (`history_protected_placement_ids` were kept because their execution
  started or finished); replaced placements that execution history refers to
  are listed in `removed_with_history_placement_ids` (the history is kept).
- `mode: incremental`: every saved placement of the date is kept exactly (id,
  interval, version, execution links) and only new work -- tasks allocated to
  the date that are not scheduled anywhere yet -- is fitted around it. If a
  kept placement no longer fits (task deleted or moved, duration changed,
  window, fixed block, engine mode, deadline or dependency change), the
  response is `409 regenerate_required` with `problems` (`placement_id`,
  `task_id`, `date`, `reason`, `explanation`) and nothing is saved; the user
  must choose an explicit `mode: full` regeneration.
- Concurrency: the engine runs outside any write transaction; the save then
  re-reads every input under the user's write lock and compares fingerprints
  and placement versions. Any change since (task, fixed block, preference,
  external dependency, placement) is `409 inputs_changed` /
  `409 version_conflict`, and the previous schedule stays untouched.
- A required task that cannot be placed is `422 generation_failed` with
  `date` and `failures` (`task_id`, `reason_code`, `explanation`,
  `proven_infeasible`); nothing is saved. Optional tasks that do not fit are
  returned in `days[].unscheduled`.
- A date whose day window spans a daylight-saving change is
  `422 unsupported_day_window` (not supported yet).

`GenerateOut.days[]`: `date`, `engine_mode`, `placements`,
`kept_placement_ids`, `unscheduled`, `unscheduled_count`, `total_score`;
`unallocated` lists what allocation could not place in the range.

### Reset: `POST /planning/reset/preview` then `POST /planning/reset`

Preview `{start_date, end_date}` -> `ResetPreviewOut`: what would be deleted --
the range's placements and schedule records, fixed blocks, non-recurring tasks
planned on those dates, their placements on *other* dates
(`cascade_placement_ids`, show them to the user), and the date preference
layers (those dates then inherit again). Also: `placements_with_history_ids` /
`tasks_with_history_ids` (execution history is always kept),
`protected_recurring_task_ids` (recurring templates are never deleted by a
range reset), `blocking_dependents` / `blocked` (a task outside the range
depends on one inside: the reset will be refused), and `token`.

Commit `{start_date, end_date, confirmation: <token>}` -> `ResetResultOut`
(`deleted` per kind). If anything in the previewed set changed, `409` and
nothing is deleted; preview again. Any refusal rolls the whole reset back.
Undated tasks, projects, the user layer and execution history are never
touched.

### Canonical CSV (format version 2)

- `GET /planning/csv/export?start_date&end_date&include_deleted&timezone`:
  `text/csv` download (both dates, or neither for everything).
- `POST /planning/csv/preview?allow_updates=` with the raw file as the body
  (`Content-Type: text/csv`, UTF-8, at most `max_csv_bytes`) -> `CsvResultOut`
  (`applied: false`): what importing would create/update/delete/leave
  unchanged per record kind. Nothing is written.
- `POST /planning/csv/import?allow_updates=` -> `CsvResultOut` (`applied:
  true`). The whole file is validated first and applied in one transaction, or
  not at all.

Import rules: records keep their ids; a record identical to the stored one is
unchanged; a different one needs `allow_updates=true` and the file's `version`
equal to the stored one; a deleted record is never revived. The server owns
versions and timestamps (new records start at version 1). Every record must
belong to the signed-in account: records of another account -- and ownerless
local records, whose claiming is a separate, explicit association step -- are
refused with `422 out_of_scope`. Invalid files are `422 invalid_csv` with
`problems` (line, message).

## Error shape

Every error is `{"error": {"code", "message", ...details}}`. Planning codes:
`inputs_changed` (409, `expected_fingerprint`, `current_fingerprint`),
`regenerate_required` (409, `problems`), `version_conflict` / `deleted` (409),
`in_use` (409), `already_exists` (409), `fixed_block_overlap` (409),
`generation_failed` (422), `unsupported_day_window` (422), `invalid_csv`
(422), `out_of_scope` (422), `invalid_reference` (422), `validation_error`
(422), `too_large` (413), `not_found` (404).

## Local profile

```bash
python -m app.web --data-dir DIR --timezone Europe/Berlin [--port 8765] [--backend-url https://...] [--static-dir DIST]
```

- Serves `127.0.0.1:<port>` only (the launcher refuses any non-loopback
  address). `--db-path FILE` instead of `--data-dir`; without either it opens
  the desktop app's own database. The desktop app and this service never use
  one database at the same time: whichever starts second is refused
  (`app/execution/instance_lock.py`, [desktop-web-boundaries.md](desktop-web-boundaries.md)).
  It runs fully offline without a backend.
- It prints a one-time link `http://127.0.0.1:<port>/#bootstrap=<code>`. The
  page reads the code from the URL fragment (never sent to a server) and calls
  `POST /local/session {bootstrap_code}`, which sets the HttpOnly
  `sm_local_session` cookie and returns a CSRF token; the code works once.
  `GET /local/session` returns `SessionOut {session, csrf_token, workspace}`
  (never 401). Every other request needs the cookie, unsafe ones also
  `X-CSRF-Token`; a request whose `Host` is not this service, or whose
  `Origin` is another site, is refused (`400 host_not_allowed`,
  `403 origin_not_allowed`).
- The planning endpoints (`/planning/...`) and the record endpoints
  (`/projects`, `/tasks`, `/fixed-blocks`, `/preferences`) have exactly the
  hosted paths, schemas and errors. Versions are this device's local edit
  revisions. Placements are read through `/planning/snapshot`; executions are
  not exposed by the local profile yet.
- **Workspace scope.** Each request works in one scope, fixed when it starts:
  the selected account's records (signed in, or asked to sign in again), or,
  when signed out, the ownerless records of this device. A signed-in user's
  new records belong to the account; ownerless records are never shown in, or
  claimed by, an account scope except through the association step.
- Cloud tokens stay in the local process's memory; the browser never receives
  one. Stopping the service (Ctrl+C) waits for requests in progress, then
  closes the database.

`/local` endpoints (all JSON; `SyncStatusOut`, `ConflictOut`, ... in OpenAPI):

| Method and path | Purpose |
| --- | --- |
| `GET /local/backend` | `{configured, backend_url, reachable, checked_at, error}` |
| `PUT /local/backend {backend_url or null}` | switch backend (or go offline); ends the account session first and saves the (non-secret) URL; `422` for an invalid URL |
| `POST /local/backend/check` | probe the backend's `/health` |
| `POST /local/account/register {email, password, username?, display_name?}` | create an account on the backend (does not sign in); `409 account_exists` |
| `POST /local/account/sign-in {email, password}` | sign in; returns the workspace and `unassociated_records`. Never claims or uploads ownerless records. `401 invalid_credentials`, `503 backend_unreachable` |
| `GET /local/account` | the workspace plus the backend profile (`profile_error` when it could not be fetched) |
| `PATCH /local/account/profile {base_version, display_name}` | update the backend profile |
| `POST /local/account/sign-out` | drop the token; local data and pending changes stay |
| `GET /local/association/preview` | the ownerless records per type that association would claim, `problems` (e.g. a preference layer for a scope the account already has) and `token`. Writes nothing |
| `POST /local/association {confirmation}` | claim exactly the previewed records; `409 preview_changed` if they changed (preview again), `409 association_blocked` while problems exist |
| `GET /local/sync/status` | `local_api`, `backend` (configured, reachable, checked_at, error), `signed_in`, `auth_required`, `account`, `in_progress`, `pending` (records waiting for the backend, each counted once), `conflicts`, `last_successful_sync_at` (persisted), `last_status`, `last_error` |
| `POST /local/sync` | Sync now (push, then pull) through the existing `SyncService`; `409 sync_in_progress` for a duplicate request |
| `GET /local/conflicts?status=open/resolved/all`, `GET /local/conflicts/{id}` | conflicts with local and remote records, `remote_deleted`, `allowed_actions` and `unavailable_actions` (why `keep_local` is not offered: a remote deletion, or another record owning the scope). There is no merge |
| `POST /local/conflicts/{id}/resolve {choice}` | `accept_remote` or `keep_local`; `409 resolution_refused` when not allowed |

**Getting local data onto the hosted web.** The hosted page cannot see this
device's database and never probes localhost. To move local work to the
hosted server: open the local profile, sign in, review and confirm the
association, and Sync now; then sign in on the hosted web -- the records are
there with the same ids. Until a device synchronizes, the hosted profile has
no knowledge of its pending changes.

## Limits and not yet supported

- A request covers at most 62 days; a snapshot of a larger period is several
  requests.
- No scheduling across a daylight-saving change; no recurrence expansion.
- No password recovery, token refresh, billing or rate limiting.
