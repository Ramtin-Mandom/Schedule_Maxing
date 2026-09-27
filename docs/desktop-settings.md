# Desktop Settings and account details

Settings edits the current workspace's **default** preferences. Day Preferences
edits one date. Both use the same native editor and the same persisted layering:
built-in defaults, project template, user defaults, then explicit date overrides.
Changing defaults preserves date overrides and saved placements; affected schedules
show out-of-date status until explicitly regenerated. No setting starts generation.

The engine labels are **Normal** (`precise_greedy`) and **ADHD friendly**
(`adhd_friendly`). ADHD's short-gap fields appear only for that engine; zero weight
disables its short-gap bonus. The baseline Greedy Optimizer v1 is unchanged.

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
shown read-only; configure `SCHEDULE_MAXING_TIMEZONE` at startup. Unsupported overnight
and ambiguous daylight-saving inputs are refused without rounding.

Account uses the authenticated profile, masks the email, and displays a real display
name or an honest missing-name message. Plan defaults to **Normal** until the server
provides a plan field. The Performance section is intentionally reserved for a later
milestone; existing Productivity and Execute remain available. About identifies
Ramtin Rezaei as the project's creator.

Tests: `test_settings_controller.py`, `test_settings_page.py`, existing Day preference,
appearance and account suites. A backend is not required for local Settings.
