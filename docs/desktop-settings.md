# Desktop Settings and account details

Settings edits the current workspace's **default** preferences. Day Preferences
edits one date. Both use the same native editor and the same persisted layering:
built-in defaults, project template, user defaults, then explicit date overrides.
Changing defaults preserves date overrides and saved placements; affected schedules
show out-of-date status until explicitly regenerated. No setting starts generation.

The scheduling modes are **Normal** (`precise_greedy`), **ADHD friendly**
(`adhd_friendly`), **Early finish** (`early_finish`), **Night owl** (`night_owl`) and
**Catch-up** (`catch_up`); see [scheduling-modes.md](scheduling-modes.md) for what each
optimizes. ADHD's short-gap fields appear only for that mode; zero weight disables its
short-gap bonus. The baseline Greedy Optimizer v1 is unchanged.

Save applies one field with a version check. Use inherited removes the field's
contribution. For category multipliers/windows, No preference explicitly stores null,
clearing a lower-layer value. Reset default overrides removes only the user layer.
Validation errors retain typed edits. Conflicts refresh the saved version while
retaining edits for an explicit retry; nothing silently overwrites concurrent changes.

Related tags use `study = reading, writing; exercise = walking`. Empty sets an empty
relation map; Use inherited removes the override. Scoring currently considers each
task's first tag, while retaining all tags in storage. Spacing is a scoring preference,
not a mandatory break. Unused category-bonus and legacy annealing/runtime controls are
not exposed. No YAML editing is needed.

Language currently offers English only. Theme and interface scale persist in
`ui_settings.json` beside the database and stay on this device. Scheduling preferences
are SQLite records and sync when associated with an account; with direct PostgreSQL
storage ([direct-postgres.md](direct-postgres.md)) they are the signed-in account's
records in PostgreSQL, read and saved in background workers. The planning timezone is
shown read-only: it is the computer's own time zone unless `SCHEDULE_MAXING_TIMEZONE` is
set at startup. The **Day window** row is the default start and end of the usable day for
every date without its own window (entered as [hour]:[minute] [AM/PM]); a date's own window
is set in the Day Window bar above the Day/Week/Month schedules and is kept when the default
changes. Unsupported overnight and ambiguous daylight-saving inputs are refused without
rounding.

Account uses the authenticated profile, masks the email, and displays a real display
name or an honest missing-name message. Plan defaults to **Normal** until the server
provides a plan field. The Performance section is intentionally reserved for a later
milestone; existing Productivity and Execute remain available. About identifies
Ramtin Rezaei as the project's creator.

Tests: `test_settings_controller.py`, `test_settings_page.py`, existing Day preference,
appearance and account suites. A backend is not required for local Settings.

## Reset All Task Data

The red **Reset all task data** card at the bottom of Settings removes the
workspace's tasks, fixed blocks, projects, schedules (placements and schedule
records) and executions with their completion history. It asks first
(**Reset all task data?**, destructive: Enter does not confirm) and cannot be
undone. Order, so the app never looks empty while the data still exists:

1. **Signed in (account workspace):** `POST /me/task-data/reset` on the server
   (`backend/task_data_reset.py`): one transaction under the account's lock;
   every record becomes a logged tombstone, so the account's other devices
   remove their copies on their next sync, and a stale queued edit there is
   refused as a conflict instead of reviving anything.
2. **Only after the server confirmed:** this device deletes its copy and the
   related sync bookkeeping (queued operations, change marks, shadows,
   conflicts) in one local transaction, and continues syncing after the reset
   (`app/planning/task_data_reset.py`, `SyncService.reset_task_data`). Every
   page re-reads.
3. **Unreachable server or expired sign-in:** an error says so and nothing is
   deleted -- neither on the server nor on this device.
4. **Ownerless local workspace (no account):** only this device's data exists
   and is deleted. **Direct PostgreSQL storage:** the database is reset in one
   transaction with the server's own code.

Kept, by design: the account, its credentials and profile, and **scheduling
settings** -- default preferences, date overrides and per-date day windows.
They configure how days are scheduled; they are not task data, and a reset
must not silently change the working day. Other users' data is never read:
the account is the one of the access token, never an id from the request.
