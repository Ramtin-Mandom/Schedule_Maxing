# Synchronization protocol

Milestone 3 connects the desktop's local SQLite records
([sync-contract.md](sync-contract.md)) to the server backend
([backend.md](backend.md)).

- **Server side:** `backend/sync.py` handles pushes; `GET /changes` serves
  pulls.
- **Desktop side:** the `app/sync/` package.
- **User interface:** the local web profile (`app/web`, Milestone 4) exposes
  this service over loopback HTTP for the web UI (`/local/...`, see
  [web-api.md](web-api.md#local-profile)); the desktop still has no sync
  screen.

## Inert by default

The desktop runs offline unless **both** of these are true:

- `SCHEDULE_MAXING_BACKEND_URL` names a backend (or `open_app_services(backend_url=...)` is used);
- an account has signed in through `SyncService.sign_in(email, password)`.

Otherwise `sync_now()` returns `inert`, nothing is sent, and no login,
network, or backend setting is needed. An invalid backend URL is logged and
ignored; it never blocks offline startup.

## Credentials

- **Password:** sent once, to `/auth/login`. Never stored.
- **Access token:** kept only in the `SyncService`'s memory. It is never
  written to SQLite, files, or logs.
- **When the token is dropped:** on `sign_out()`, when the server answers
  401 (status `auth_required`), or when the app closes. Afterwards you sign
  in again.

## Accounts, ownership, and association

Each (backend URL, server user) pair is one `sync_accounts` row, keyed
`<url>#<user id>`. Its pull cursor, shadows, outbox operations, and
conflicts are stored under that key, so two accounts or two backends never
mix.

- **Signing in does not claim existing ownerless records.** Those records
  (`user_id IS NULL`) are only uploaded after the explicit
  `SyncService.associate_local_data()` step. That step assigns the signed-in
  account as owner (one logical mutation per record: version + 1). Records
  already owned by another account are never touched.
- **The active account.** After association, the account is *active* on
  this device. Records created locally while it is active are owned by it
  (a v5 trigger stamps them). Signing out, or signing in as someone else,
  deactivates it. Signing back in to an account that was associated here
  reactivates it.
- **Push and pull are owner-scoped.** A push sends only records owned by the
  current account. A pull stores records as that account's.
- **Local work happens in one workspace** (Milestone 4,
  `SyncService.workspace_scope()`): the account selected in this session,
  else the account active on this device, else the ownerless records. The
  desktop and the local web service show, generate, export and reset only
  that workspace's records; records created in an account workspace are the
  account's. See [desktop-web-boundaries.md](desktop-web-boundaries.md).

## Local change capture

Schema v5 adds triggers on every synchronizable table (projects, tasks,
fixed blocks, placements, preference layers, schedule records, executions)
and on work sessions, which mark their execution.

Each trigger bumps the record's `sync_dirty.local_rev` **in the same
transaction** as the write, whichever code path makes it:

- service edits, deletes, and tombstones;
- CSV import;
- rescheduling cleanup;
- execution actions, sessions, and feedback;
- the history reset;
- preferences;
- provenance.

So a rolled-back mutation leaves no mark, and a committed one cannot be
missed.

While pulled records are applied, `sync_control.applying_remote = 1`
suppresses capture. As a result pulled data never echoes back to the server.

## Local revision vs. server version

| Value | Where | Meaning |
| --- | --- | --- |
| `version` column | every local record | The local edit revision (docs/sync-contract.md). Never sent. |
| `sync_dirty.local_rev` | per changed record | Counts local changes since the last acknowledgement. Never sent. |
| `sync_shadows.server_version` | per account and record | The version of the last server state this device acknowledged, from a push result or a pull. |

Every update, delete, or action is sent with
`base_version = shadow.server_version`. Several offline edits are therefore
sent as **one** change against the last acknowledged server version,
never as a local counter.

## Push

`POST /sync/push` takes `{"operations": [...]}`, with at most 200
operations. Each operation has this shape:

```json
{"op_id": "<uuid>", "entity_type": "task", "entity_id": "<uuid>",
 "kind": "create|update|delete|action|feedback", "base_version": 3,
 "action": "start|pause|resume|complete|skip|cancel|reopen|reschedule", "payload": {...}, "group": "<uuid>|null"}
```

An execution the server has as `completed` or `skipped` that a device moved
back to the Day board's Tasks column is sent as `reopen` (then, if it was
finished again differently, the new `complete` or `skip`); see
`app/sync/mapping.execution_changes`.

`reschedule` is the one placement action (Milestone 5,
[execution-rescheduling.md](execution-rescheduling.md#7-synchronization)):
the move, its replacement and the cancellation of the never-started
execution are applied as one unit, and the applied result lists the other
records it changed in `related`. A placement `delete` may carry
`{"removal_reason", "superseded_by_id"}`.

### What the client sends

`SyncEngine.prepare` looks at each dirty record of the current account.
Each record gets at most one pending chain of operations; a record that has
unanswered operations or an open conflict waits.

| Server copy (shadow) | Local record | Operation |
| --- | --- | --- |
| none | live | `create` with its current content |
| none | deleted, or never pushed | nothing (created and deleted offline) |
| exists | live | `update` with `base_version` = the shadow version |
| exists | deleted, or physically removed | `delete` |
| tombstone | live | a conflict; the record is never revived |

- **Executions.** A local execution with changes becomes a sequence of
  lifecycle `action`s with the local session times as `at`, then
  `feedback`. The sequence is sent as one **atomic group**, with base
  versions `v, v+1, …`. Local history the server cannot express (sessions
  that differ from the server's) becomes a `diverged_history` rejection.
- **History reset.** A local reset physically deletes executions. The next
  push sends a delete for each execution this account had synchronized.
- **Moves.** A placement moved locally (a tombstone with reason
  `rescheduled` whose shadow is live) is sent as one `reschedule` action,
  never as a delete and a create. Its replacement(s) and the moved
  placement's execution send nothing of their own until that action is
  answered; a pulled change to them only refreshes their shadow. Other
  placement tombstones send their removal reason with the delete.
  A plan created and then moved or regenerated before it ever reached the
  server is uploaded as history (a `create` with its removal reason and
  successor, stored as a tombstone), after its successor.
- **Order.** Creates and updates go in this order: projects, tasks (each
  after the tasks it depends on), fixed blocks, placements, preferences,
  schedule records, executions. Deletes follow, in reverse order
  (dependents first).
- **Stable ids.** Each operation is stored in `sync_outbox` with a stable
  `op_id` and the `local_rev` it was built from. A group is never split
  across requests.

### What the server does

It applies the operations in order, through the same `Mutator` as the REST
API: user scoping, preconditions, relationship rules, server versions, and
the change log all apply.

- **Independent operations** succeed or fail individually.
- **A group** is all-or-nothing. If one operation fails, every operation in
  the group reports the failure (the others with `group_failed`), and
  nothing is applied.
- **Results:** `applied` (with the server `record`), `conflict` (a 409 case,
  with `error.current` when there is one), or `rejected` (422/404).

### Idempotency

The server records each operation's outcome under `(user, op_id)`.

- An **applied** outcome is recorded in the same transaction as the
  mutation.
- A **conflict or rejection** is recorded after its mutation rolled back.
- A **retry** with the same `op_id` — including after a lost response —
  returns the recorded result. Nothing is applied twice: no new records,
  sessions, versions, or change-log entries. The recorded result
  references immutable snapshots of its records, so it stays the same
  even after those records are edited later.
- **Reusing** an `op_id` for a different operation returns `op_id_reused`.

### Acknowledgement

The answers are recorded in one SQLite transaction:

- **Applied:** the shadow becomes the returned record (and each `related`
  record the shadow of its own record) and the operation leaves the outbox. The dirty mark is cleared **only if `local_rev` is
  unchanged**. An edit made while the request was in flight stays pending
  and is sent next time, against the new shadow version.
- **Conflict or rejected:** it becomes a `sync_conflicts` row, and that
  record's operations are removed. Other records keep synchronizing.

### Recurring series ([recurrence.md](recurrence.md))

- `GET /sync/capabilities` answers `{protocol_version, features,
  max_push_operations}`; an older server answers 404, which the client reads as
  protocol 1 without features. Records carrying recurrence data are pushed only
  when `features` includes `recurrence_occurrences`; otherwise they stay pending
  and the sync report says how many are held.
- A series is sent before its occurrences, a segment after its predecessor. All
  pending task operations of one lineage (its segments, their occurrences, the
  moves of those occurrences and the deletes of superseded ones with their
  placements) form one atomic group.
- A create repeating an occurrence the server has with the same content is
  `applied` with the stored record (no new version, no change-log entry); with
  different content it is an `already_exists` conflict. A pulled occurrence equal
  to this device's pending copy converges without a conflict.
- **Series precondition (Milestone 6).** A device numbers its own versions, so
  the precondition of a *new* live occurrence is its series' current state: the
  series is live, the slot is still one of its dates, and an occurrence that
  follows its series (no exception state) carries the series' current content
  (name, category, tags, estimate, priority, points, required, preferred window,
  project). Otherwise the create is a `series_changed` conflict whose `current`
  is the series. So an occurrence expanded offline from an older rule -- or
  under a series another device deleted -- can never get around the series
  edit. A tombstone create (a skipped or deleted slot) needs no precondition: it
  only keeps a slot reserved.
- An occurrence removed before it ever synced is pushed as a create carrying its
  tombstone `occurrence_state`; a pulled occurrence tombstone is stored even if
  never seen locally. A task delete may carry `{"occurrence_state"}`.
- A reschedule that moves an occurrence to another date returns the re-dated
  occurrence task among its `related` records.

### Manual placements ([execution-rescheduling.md](execution-rescheduling.md))

Placement `origin`/`preserved` and execution `cancel_reason` (including the
`cancel_reason` of a pushed `cancel` action) are sent only to a server whose
capabilities list `manual_placements`. Against an older server they are left
out of payloads, and a pulled placement record without them keeps the local
values, so an older server can never erase manual intent. The server keeps
both fields when an update omits them (an older client) and never lets an
update grant intent.

## Pull

`GET /changes?after=<cursor>&limit=` returns the account's change feed.

- **Order.** The feed is ordered by a per-user sequence number that is
  gap-free and follows commit order (see backend.md). It is never ordered
  by timestamp, so records with equal timestamps cannot be skipped, and a
  change committed after an earlier cursor cannot be skipped either.
- **Contents.** Each entry carries the complete record, or the tombstone for
  a delete.
- **Atomic pages.** A page and its new cursor are applied in **one SQLite
  transaction**. A crash before commit leaves the cursor unchanged, so the
  page is fetched again.
- **Safe replay.** A change whose version is not newer than the shadow is
  skipped. This covers our own echoes and replayed pages.
- **Pending local changes win the race.** A change to a record that has a
  local change or an open conflict is stored as a **pull conflict** instead
  of overwriting local work.
- **Collisions.** Another local record may own the same unique scope: a
  preference layer for the same scope, a schedule record for the same date,
  or an execution for the same placement. That is a conflict too.
- **Executions** are stored with their sessions. A `legacy_id` becomes the
  local id again, with its wire-id mapping.
- **Schedule freshness** is never taken from another device. A pulled
  schedule record is current on this device only if its inputs fingerprint
  matches the inputs recomputed here (tasks, fixed blocks, preferences
  including this device's YAML layer, timezone) and the saved placements
  match.

`sync_now()` pushes until the outbox is drained, then pulls until caught up.
Both loops are bounded.

## Conflicts

`SyncService` provides these calls:

- `list_conflicts(status="open"|"resolved"|None)`
- `get_conflict(id)`
- `resolve_conflict(id, "accept_remote"|"keep_local")`

Each conflict stores:

- its kind (`push_conflict`, `push_rejected`, or `pull_conflict`);
- the operation id;
- the base version;
- the local record (the intent);
- the remote record or tombstone;
- the server error.

Conflicts are durable across restarts. A record with an open conflict is not
retried automatically; everything else continues.

| Resolution | Effect |
| --- | --- |
| `accept_remote` | The server state replaces the local record. If the server never had the record (a rejected create), or another record owns its scope, the local record is discarded as a local tombstone and is not pushed. |
| `keep_local` | The remote version becomes the new precondition and the local change is prepared as a **new** operation (a new `op_id`; an `op_id` is never reused with a different payload) against that displayed server revision. The server applies its usual rules (constraints, ownership); if the record changed again before the push, that is another conflict. Refused when the server record is a tombstone, because that would silently revive it, and when another record owns the scope. |

Accepting the server's placement over a local move also undoes the rest of
that move: the replacement the server never had is discarded locally, and the
execution the move cancelled returns to its last acknowledged server state.

Milestone 6 additions:

- **`series_changed`** (above): an occurrence this device only expanded (no
  exception state) is not the user's intent, so it follows the server's series
  at once, without a conflict -- re-derived and sent again, or retired -- and
  other occurrence creates of that series that failed only because their unit
  did are sent again on their own (members of any other compound change never
  are). A local exception, or a pending local change of the series itself, is
  a conflict: `keep_local` is refused. `accept_remote` stores
  the server's series (unless it has a pending change of its own, which its own
  conflict decides) and re-derives the occurrence from it -- it is sent again --
  or, when its date is no longer part of the series or the series was deleted,
  retires it here as `superseded` together with its never-synchronized
  placements that have no recorded work. Nothing is resurrected.
- **Execution history is never discarded.** `accept_remote` on an execution
  first keeps the work sessions only this device recorded as a separate
  historical execution (same snapshot, no placement link, just those
  sessions, derived metrics unknown), which is then pushed as a create;
  `resolution.kept_history_execution_id` names it. `keep_local` is refused
  when the local sessions do not continue the server's (they could not be
  expressed as lifecycle actions).
- **Refused requests** (see Failures) arrive as `push_rejected` conflicts with
  code `request_refused`.
- The desktop conflict view (`app/ui/account_controller.conflict_context`)
  explains each conflict in words: which occurrence of a series and its
  original date, exception states on each side, whether a placement is the
  user's manual placement, work sessions only one side has, and what the
  choice does to related records.

### Retention and cursors

- The server never prunes its change feed, record revisions, sync operation
  results or tombstones (`change_log`, `record_revisions`, `sync_operations`,
  `sync_operation_related_records`). A device that reconnects after any time
  replays from its cursor: a deleted series, a skipped or deleted occurrence
  and every placement lineage arrive as tombstones and stay suppressed, and a
  retried `op_id` is answered from its stored result. Revision histories needed
  for execution history and placement lineage are kept.
- The client keeps acknowledged tombstones' shadows and stored occurrence
  tombstones (their slots stay reserved); it deletes an outbox operation only
  when it is answered (applied, conflict or rejected) and a conflict row never
  (resolved ones keep their decision).
- The cursor advances only in the transaction that applies its page's records;
  a crash anywhere before that commit replays the page.

Every decision is kept on the conflict row: `resolution` (the choice, the
time, and the base and remote versions) and `resolved_at`.

## Status, association preview, and account switches (Milestone 4)

- `SyncService.status()` reports backend reachability (from the last request
  or `/health` probe), sign-in state (`auth_required` when the account is
  selected but its token was refused), a running sync, `pending` (the
  account's dirty or queued records, each counted once), open conflicts, the
  last successful sync (`sync_accounts.last_synced_at`, schema v6) and the
  last error.
- `association_preview()` lists what association would claim and any record
  that cannot be claimed as it is (an ownerless preference layer or schedule
  record for a scope the account already holds).
  `associate_local_data(confirmation=token)` applies exactly that preview.
- `sign_in`, `sign_out`, `set_transport` (switching backends) and association
  wait for a running sync, so a sync always finishes for the account and
  backend it started with; a 401 drops only the token that sync used.
- `conflict_actions(conflict)` says which resolutions will work and why the
  others would be refused.

## Failures and background operation

| Failure | Handling |
| --- | --- |
| `TransportError` (network, timeout, 5xx) | Operations stay in the durable outbox, also across restarts, and are resent with the same `op_id`. The next run waits `min(backoff_max, backoff_base · 2^(failures-1))`. |
| `AuthenticationError` (401) | The token is dropped. Status is `auth_required`. |
| `AuthenticationError` during a sync | The outbox and conflicts stay. Nothing is sent until the same account signs in again; another account's token never sends this account's queue (operations are per account). |
| `ProtocolError` on a push (other 4xx for the whole request) | The batch is narrowed unit by unit (a group is one unit) under the same `op_id`s until the refused units are found; those become `push_rejected` conflicts with code `request_refused` (actionable in the conflict view, never retried automatically). Everything else is sent normally. |
| `ProtocolError` on a pull | Reported (status `error`); the cursor does not move. |
| Per-operation conflicts and rejections | Stored as conflicts (above). |

Recovery matrix (each is covered by `tests/sync`):

| Interruption | Outcome |
| --- | --- |
| Lost push response (the server committed) | Resent with the same `op_id`s; answered from the stored result -- no new version or change-log entry. |
| Crash after a push answer, before it was acknowledged locally | Same as a lost response. |
| Crash while applying a pull page | The page's transaction rolls back with the cursor; the page is fetched again. |
| A page delivered in part, or replayed | Every change not newer than its shadow is skipped. |
| Restart with pending operations, conflicts or a stale token | All durable; the next sync resumes in dependency order without duplicate occurrences, placements, executions or sessions. |
| Account switched while a request is in flight | The answer is recorded for the account that sent it; the new account's records and UI are never touched by it. |

`SyncService.start()` runs `sync_now()` on a daemon thread every
`interval`, or after the backoff delay. `wake()` triggers a run early.
`stop()` ends the loop and waits for a running sync.

`AppServices.close()` stops sync before shutting down background workers and
closing the database. No SQLite transaction or lock is held during a
network call: each step (prepare, acknowledge, apply page) is its own short
transaction on the shared connection lock. Desktop edits proceed
concurrently.

## Tests

- `tests/sync/` runs the client against an in-process backend, using fake
  or failure-injecting transports. It includes two independent SQLite
  devices.
- `tests/backend/test_sync_push.py` covers the server side of push.
- With `BACKEND_TESTS_ON_POSTGRES=1` and a disposable `TEST_DATABASE_URL`,
  both suites run on PostgreSQL. Concurrent commit ordering is covered in
  `tests/backend/test_postgres.py`.
