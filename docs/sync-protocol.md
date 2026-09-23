# Synchronization protocol

Milestone 3 connects the desktop's local SQLite records
([sync-contract.md](sync-contract.md)) to the server backend
([backend.md](backend.md)).

- **Server side:** `backend/sync.py` handles pushes; `GET /changes` serves
  pulls.
- **Desktop side:** the `app/sync/` package.
- **User interface:** there is no sync or conflict screen yet. The service
  API below is what a later milestone's UI will call.

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
- **Local display is device-wide.** The desktop shows every local record
  whatever its owner. Filtering the view by account is a later UI concern.

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
 "action": "start|pause|resume|complete|skip|cancel", "payload": {...}, "group": "<uuid>|null"}
```

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
  sessions, versions, or change-log entries.
- **Reusing** an `op_id` for a different operation returns `op_id_reused`.

### Acknowledgement

The answers are recorded in one SQLite transaction:

- **Applied:** the shadow becomes the returned record and the operation
  leaves the outbox. The dirty mark is cleared **only if `local_rev` is
  unchanged**. An edit made while the request was in flight stays pending
  and is sent next time, against the new shadow version.
- **Conflict or rejected:** it becomes a `sync_conflicts` row, and that
  record's operations are removed. Other records keep synchronizing.

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
| `keep_local` | The remote version becomes the new precondition and the local change is sent again. It can conflict again. Refused when the server record is a tombstone, because that would silently revive it, and when another record owns the scope. |

Every decision is kept on the conflict row: `resolution` (the choice, the
time, and the base and remote versions) and `resolved_at`.

## Failures and background operation

| Failure | Handling |
| --- | --- |
| `TransportError` (network, timeout, 5xx) | Operations stay in the durable outbox, also across restarts, and are resent with the same `op_id`. The next run waits `min(backoff_max, backoff_base · 2^(failures-1))`. |
| `AuthenticationError` (401) | The token is dropped. Status is `auth_required`. |
| `ProtocolError` (other 4xx for the whole request) | Operations are marked `blocked`. Status is `error`. No automatic retry. |
| Per-operation conflicts and rejections | Stored as conflicts (above). |

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
