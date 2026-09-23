# Local data contract for synchronization

Status: written in Milestone 3, prompt 1. This describes the **local**
SQLite records (schema version 4, `app/execution/db.py`) that
synchronization builds on. The server ([backend.md](backend.md)) and the
synchronization protocol and client ([sync-protocol.md](sync-protocol.md),
schema version 5) came later in the milestone; see section 11.

## 1. Synchronizable entities

Every synchronizable record has the same metadata:

| Field        | Meaning |
| ------------ | ------- |
| `id`         | Stable identity (UUID; see section 3 for the execution exception). Never reminted, never reused, not even after deletion. |
| `user_id`    | Owner. `NULL` = a local record created without an account (section 4). |
| `created_at` | UTC instant the record was first created. Never changes afterwards. |
| `updated_at` | UTC instant of the record's latest logical mutation. |
| `version`    | Local edit revision (section 5). |
| `deleted_at` | Tombstone time; `NULL` = live (section 6). |

| Table                  | Model                                   | Notes |
| ---------------------- | --------------------------------------- | ----- |
| `projects`             | `app.planning.models.Project`           | |
| `tasks` (+ child rows) | `Task`                                  | Tags, preferred dates, dependencies, and recurrence weekdays are part of the task record and versioned with it. |
| `fixed_blocks`         | `FixedBlock`                            | Gained `category` (default `"fixed"`), `user_id`, and audit fields in v4. Blocks from before v4 got the migration instant as `created_at`/`updated_at` and version 1. The earlier times are unknown, so no earlier time is guessed. |
| `scheduled_tasks`      | `ScheduledTask` (a placement)           | Always carries its task's owner. |
| `executions` (+ `work_sessions`) | `app.execution.models.TaskExecution` | An aggregate: sessions belong to their execution (section 3). |
| `preference_overrides` | `app.planning.preferences.PreferenceRecord` | The user layer (`scope='user'`) and per-date layers (`scope='date'`). `optimizer_mode` is a column. The rest of a layer is a validated JSON document that keeps the difference between an absent key, a value, and an explicit `null`. |
| `schedule_generations` | `app.planning.provenance.GenerationRecord` | Provenance of a date's saved schedule (section 8). One live record per (owner, date). |

Timestamps are ISO 8601 text. Audit timestamps are UTC. A planned instant
(a deadline, or the start or end of a block or placement) keeps the UTC
offset it was written with.

Lifecycle status belongs to its own domain. An execution's `status`
(`scheduled`, `in_progress`, `paused`, `completed`, `skipped`, `cancelled`)
is not a placement status, and it has nothing to do with `deleted_at`.
Placements have no "completed" state: a placement is either live or a
tombstone.

## 2. Local edit revisions vs. server versions

`version` is **this device's edit revision** of the record. It starts at 1
(or at the value an identity-preserving import brought with it) and
increases by exactly 1 per local logical mutation. It is **not** a server
version and must not be read as one. The synchronization client keeps
server versions separately: `sync_shadows.server_version` holds the last
acknowledged server version per account, and `sync_dirty.local_rev` counts
local changes since then (see sync-protocol.md). A local version is never
sent as a server precondition.

## 3. Identity and wire identity

- **Planning records** (projects, tasks, fixed blocks, placements,
  preference layers, generation records) have UUID ids. Their wire id is
  the id itself.
- **Executions** keep the id they were created with, exactly. Current
  executions use UUID strings, and a UUID id is its own wire id. Older
  fixture or imported executions may have non-UUID ids (for example
  `legacy-1`). Those ids are **preserved, never reminted**. Instead,
  `execution_wire_ids` maps each non-UUID id to one random UUID wire id.
  Migration v4 assigns the mapping for existing rows, and it is assigned
  at creation for new rows. The mapping is durable: it survives reopen and
  is never regenerated. `ExecutionRepository.wire_id(id)` returns the wire
  id for any execution.
- **Work sessions** have local `INTEGER AUTOINCREMENT` ids. These are
  **local only**: two devices will produce colliding integers. Sessions
  sync as part of their execution aggregate, as an ordered list (by
  `started_at`) inside the execution. A session is identified within that
  aggregate by its execution's wire id plus its `started_at` instant. A
  session has no version of its own. Opening or closing one is part of the
  execution mutation that does it, and that mutation advances the
  execution's version.

## 4. Ownership

- `user_id` is the owner. `NULL` means a local, ownerless record: one
  created offline before any account existed. The claim step
  (`SyncService.associate_local_data()`, sync-protocol.md) assigns the
  signed-in owner to ownerless records exactly once, as its own explicit
  logical mutation (version + 1), before the first upload. It is never an
  implicit side effect of signing in or of a normal edit.
- Ownership cannot change through an update. `PlanningService` rejects an
  update whose `user_id` differs from the stored one.
- Derived records follow their parent. The service stamps a placement with
  its task's owner. An imported placement must already have its task's
  owner.
- A batch import (canonical CSV, and later a sync pull) must have a single
  owner, and that owner must match every stored record the batch touches
  or references.

## 5. Versions and preconditions

- **Planning records** (`PlanningService`). Each logical mutation that
  changes a record's content adds 1 to `version` and sets `updated_at`.
  Saving unchanged content is a no-op, and its precondition is still
  checked. Soft deletion is a logical mutation too (+1). The `version`
  field of a model passed in is never trusted as a precondition, so it can
  never push the stored version forward.
- **Executions** (`ExecutionService`). Each logical mutation adds exactly 1
  to `version`, however many rows it writes. The logical mutations are
  start, pause, resume, complete, skip, cancel, record_feedback, and
  delete_execution. For example, start writes the status change, a new
  work session, and the first-start time, and still adds only 1. Creation
  stores version 1.
- **Preconditions.** Every update and delete takes `expected_version`: the
  version the caller last read. For a batch it takes `expected_versions`.
  The write is one atomic SQL compare-and-update
  (`... WHERE id = ? AND version = ? AND deleted_at IS NULL`). A stale
  write raises a structured conflict and leaves the stored record exactly
  as it was:
  - `app.planning.errors.VersionConflictError`, with `kind`, `entity_id`,
    `expected_version`, `current_version`, and `deleted`;
  - `app.execution.errors.ExecutionVersionConflictError`, with the same
    information.

  `record_feedback` and `delete_execution` require a precondition. The
  execution state transitions accept one (the desktop Execute tab passes
  the version it shows). Without one, the transition table is still
  checked against the stored status inside the same transaction.
- **Range operations.** Reset (`clear_range`) and range rescheduling read
  whatever is stored inside one transaction. Rescheduling requires the
  placements its generation read (`expected_versions`), so a schedule
  saved after generation started causes a conflict, not an overwrite.

## 6. Soft deletion (tombstones)

- Deleting a project, task, fixed block, placement, preference layer,
  generation record, or single execution sets `deleted_at`, sets
  `updated_at`, and adds 1 to `version`. The row stays. For a task, its
  child rows stay too. Every normal read skips tombstones. Tombstoned ids
  can never be created again.
- Deleting a task also tombstones its live placements. The existing
  deletion policy still applies: a task that live tasks depend on, or a
  project that still has live tasks, is refused.
- A new execution can only link to a live task and a live placement (the
  v4 trigger).
- `ExecutionService.reset_all_history` (the Productivity page's "Reset
  local history") is an explicit **local purge**, not a synchronizable
  deletion: it physically deletes executions, sessions, and wire-id
  mappings. It is not a local tombstone. Synchronization (Milestone 3,
  [sync-protocol.md](sync-protocol.md)) treats it as a decision to remove
  the history: the next push deletes (tombstones) on the server every
  execution the signed-in account had synchronized.
- Execution history never cascades: deleting or replacing a task or
  placement leaves its executions and their snapshots untouched.

## 7. Legacy and historical data

- **Ownerless offline records.** `user_id IS NULL`. See section 4. Nothing
  is claimed automatically.
- **Legacy non-UUID execution ids.** These are kept and mapped. See
  section 3.
- **Historical missing parents.** An execution's `task_id` and
  `scheduled_task_id` are historical identity, not foreign keys. Rows from
  before v3 may name a task or placement that was never persisted. Later
  rows may name one that has since been deleted (a tombstone now) or
  replaced. `ExecutionRepository.list_executions_with_unresolved_links()`
  lists them. They must sync as they are: the snapshot fields (name,
  category, planned date and instants) are the record of what was planned.
  The references must be accepted as dangling, and no placeholder parent
  may be made up.
- **Unknown historical dates.** Rows without a canonical planned date keep
  none. No date is ever guessed from `created_at`.

## 8. Persisted schedule provenance

`schedule_generations` records, for each date, what its saved placements
were generated from:

- the allocation range and scope;
- the timezone;
- the engine mode;
- the allocation id;
- an **inputs fingerprint**;
- a **digest of the saved placements**.

The inputs fingerprint (`app/planning/provenance.py`, `FINGERPRINT_VERSION`
1) is a SHA-256 of canonical JSON. It covers the range's tasks (content
only), their dependencies, the fixed blocks of every date in the range, the
resolved per-date preferences (YAML -> user -> date, including the engine
mode), and the resolved state of every dependency outside the range.

After a restart, a date is:

- **current** if its record matches the recomputed fingerprint and
  digest, even if it placed nothing;
- **stale** if they differ (`INPUTS_CHANGED` or `PLACEMENTS_CHANGED`);
- **stale, `NO_PROVENANCE`** if it has placements but no record, which is
  the case for all pre-v4 data. Such a date is never assumed current.

Records are saved in the same transaction as the placements. Because the
fingerprint includes the device's YAML layer, a record received from
another device only counts as current if the inputs really match.

## 9. Rescheduling and occurrence identity

`app/planning/occurrence.py` defines what a placement is *for*:

- A non-recurring task has one occurrence: `(task_id, None)`.
- A recurring template has one occurrence per date: `(task_id, date)`.
  Recurrence is model-only, and there is no expansion engine.

Make Schedule (`PlanningService.reschedule_range`) does the following in
one transaction:

- It replaces the range's placements.
- It tombstones active placements **outside** the range, but only those
  that a new placement **supersedes** (the same occurrence). It never
  touches placements of tasks the run did not place, other dates of a
  recurring template, or placements whose execution has started or
  finished.
- It saves the provenance.

## 10. The canonical planning CSV (format version 2)

`app/planning/csv_export.py` writes the file and
`app/planning/csv_canonical.py` together with
`PlanningService.apply_record_batch` imports it. Each file row carries one
record with its id, owner, relationships, version, timestamps, and
tombstone. The import applies the same collision rules a sync merge will
need:

- A record that is not stored is created as given. A record that arrives
  deleted is stored as a tombstone.
- A record identical to the stored one is a no-op. So is a record that
  arrives deleted when it is already deleted here.
- A record that arrives live for a stored tombstone is rejected. An import
  never revives a deleted record.
- A record that diverges from the stored one is rejected unless
  `allow_updates` is set **and** the row's `version` equals the stored
  version. It is then applied as one logical mutation.

The whole batch is validated (format, ownership, relationships, cycles,
fixed-block overlaps) before its single transaction commits. Legacy
schedule CSVs without ids still import with fresh UUIDs. Format version 1
exports are refused, because importing them would silently clear ownership,
project, and recurrence.

## 11. Implemented since, and still not done

Implemented later in Milestone 3:

- the server ([backend.md](backend.md));
- the synchronization protocol and desktop client
  ([sync-protocol.md](sync-protocol.md));
- the claim step, as `SyncService.associate_local_data()`;
- server versions, held per account in `sync_shadows` alongside the local
  revision.

Still not implemented:

- a sync or conflict-resolution UI;
- recurrence expansion;
- preference or provenance records in the CSV. Preferences are a per-user
  configuration, and provenance can be recomputed.
