# Schedule Maxing

A Python schedule optimization project that builds daily, weekly, and monthly schedules from user-defined tasks. The app supports fixed tasks, flexible tasks, preferred time windows, task dependencies, reward-based scoring, CSV input/output, and a CustomTkinter desktop interface.

The main idea of this project is to combine **hard scheduling constraints** with a **reward function**. Hard constraints decide whether a task placement is allowed, while the reward function decides how good a valid placement is. The optimizer then searches for strong task placements and returns a final schedule with scheduled and unscheduled tasks.

---

## Project Overview

This project is designed around a simple scheduling problem:

> Given a list of fixed tasks and flexible tasks, place the flexible tasks into the available time slots while respecting constraints and maximizing the schedule score.

The optimizer currently uses a **greedy reward-based scheduling algorithm**, frozen as the protected baseline **Greedy Optimizer v1** (Milestone 0). It places fixed tasks first, then repeatedly chooses the best currently valid placement for one flexible task at a time. For each flexible task, the optimizer scans possible start times in 30-minute increments, scores each valid placement, and keeps the highest-scoring option.

The project also includes a dependency system inspired by PERT-style precedence constraints. If one task depends on another task, the optimizer tries to place the dependent task only after the prerequisite task has finished. In the legacy pipeline, missing dependency names are ignored so that the app remains usable even if the input contains a dependency that is not present in the current task list; the persistence-backed CSV importer used by the desktop app and CLI is stricter and rejects such a file instead (see [Saved data](#saved-data-sqlite-import-export-reset-and-backups-milestone-2)).

Since Milestone 2, the desktop app and the CLI keep all planning data (tasks, fixed blocks, generated schedules) and execution history in one local SQLite database. Nothing lives only in memory, and nothing is loaded from a CSV unless you explicitly import one.

---

## Main Features

- Add fixed tasks that cannot be moved, such as sleep, classes, work, or meals.
- Add flexible tasks with duration, priority, category, tag, preferred time window, and dependencies.
- Optimize a day, week, or month schedule.
- Use a reward function to prioritize important tasks and preferred time windows.
- Support task dependencies using PERT-style graph logic.
- Ignore missing dependencies instead of crashing the program (legacy pipeline).
- Save every task, fixed block, generated schedule, and execution locally in SQLite; reopening the app restores them.
- Import legacy CSV schedules transactionally (append, or replace the dates the file covers).
- Export stored planning data to CSV with exact intervals and ids, plus the legacy half-hour CSV and an exact JSON export.
- Display schedules visually in a desktop UI with real calendar dates.
- Track unscheduled tasks and show why they could not be placed.
- Record actual task execution locally (start/pause/resume/complete/skip) with optional focus/energy/interruption feedback.
- View personal productivity insights (completion rates, duration accuracy, best-supported times of day) with an evidence/sample-count label on every figure.
- Get a historical duration suggestion (with its evidence and reasoning) when entering a new task, without ever overwriting your own entry automatically.

---

## Current Project Structure

```text
.
├── .github/
│   └── workflows/
│       └── ci.yml            # pytest, python -m compileall ., and ruff check . on push/PR
├── .venv/
├── app/
│   ├── app.py
│   ├── constraints.py
│   ├── data_processor.py
│   ├── execution/            # SQLite infrastructure (db.py: location, migrations, transactions) + execution tracking
│   ├── main.py                # canonical CLI (Task 6): import -> allocate -> generate one selected day
│   ├── models.py               # legacy (Milestone 0) input/output models
│   ├── optimizer.py            # Greedy Optimizer v1 (legacy) + generate_day_schedule (canonical, Task 4)
│   ├── pert.py                  # name-based (legacy) + ID-based (canonical) dependency helpers
│   ├── planning/                # canonical models/time/compat/preferences/allocation/service (Tasks 1, 3, 5)
│   │                            #   + repository.py/application.py: persisted planning data (Milestone 2)
│   ├── productivity/         # analytics built on execution history (stats, predictions, insights, reporting)
│   ├── reward.py
│   └── ui/                   # desktop-UI controllers/widgets, incl. the Tk-free PlanningController (Task 6)
├── benchmarks/                # reproducible performance scripts + recorded results/method docs (not run by CI)
├── config/
│   ├── settings.py
│   └── task_preference.yaml
├── data/                     # pre-Milestone-2 runtime data location (gitignored; only read once, see below)
├── samples/
│   ├── inputs/                          # ~20 descriptive scenario CSVs (see "samples/inputs/" below)
│   │   ├── valid_single_day_basic.csv
│   │   ├── dependency_chain_linear.csv
│   │   ├── fixed_blocks_overlap_invalid.csv
│   │   └── ...
│   └── outputs/
│       ├── valid_single_day_basic.csv         # legacy CLI export, regenerated by `python -m app.main`
│       └── valid_single_day_basic.exact.json  # canonical exact-interval export (Task 6), same regeneration
├── tests/
│   ├── execution/
│   ├── planning/               # canonical models/time/compat/preferences/allocation/service tests
│   ├── productivity/
│   ├── ui/                     # incl. test_planning_controller.py (Tk-free)
│   ├── conftest.py            # shared Task/FixedBlock/DaySchedule/ScheduledTask builders
│   ├── test_constraints.py
│   ├── test_data_processor.py
│   ├── test_day_engine.py      # canonical day engine (precise_greedy/adhd_friendly, mandatory scheduling)
│   ├── test_end_to_end.py      # cross-cutting integration tests spanning Tasks 1-6
│   ├── test_main_cli.py        # canonical CLI integration tests
│   ├── test_main_time_formatting.py
│   ├── test_optimizer.py       # Greedy Optimizer v1 (legacy, unchanged)
│   ├── test_pert.py
│   └── test_reward.py
├── .editorconfig
├── .env
├── .gitattributes
├── .gitignore
├── README.md
├── pyproject.toml            # Ruff configuration (target-version py310, line-length 130, select E/F)
└── requirements.txt
```

> `tests/` contains an automated suite (`python -m pytest`) covering the optimizer's core modules (constraints, PERT/dependencies, reward scoring, the optimizer itself, CSV loading, and CLI time formatting), the canonical planning layer (models, time, compat, preferences, allocation, the day engine, the CLI), execution tracking, productivity analytics, and the UI-facing controllers.

---

## File Descriptions

### `app/models.py`

Defines the main data structures used throughout the project. These models represent time windows, tasks, fixed blocks, day schedules, scheduled tasks, unscheduled tasks, and final schedule outputs.

Important models include:

- `TimeWindow`: Stores `start_time` and `end_time` in minutes from midnight.
- `Task`: Represents a flexible task with priority, duration, preferred time, category, tag, and dependencies.
- `FixedBlock`: Represents a task that already has a fixed time.
- `DaySchedule`: Stores one day of fixed blocks and flexible tasks.
- `ScheduledTask`: Represents a task after it has been placed into the schedule.
- `UnscheduledTask`: Represents a task that could not be scheduled.
- `DayScheduleOutput`: Stores the final result for one day.

---

### `app/data_processor.py`

Loads schedule data from CSV files and converts rows into project models.

It reads fixed and flexible tasks differently:

- Fixed rows become `FixedBlock` objects.
- Non-fixed rows become `Task` objects.

For flexible tasks, the CSV `start_time` and `end_time` are treated as the task's preferred time window, not its final scheduled placement.

---

### `app/constraints.py`

Contains the hard time-based constraint checks used by the scheduling system.

This file checks things like:

- Whether two time intervals overlap.
- Whether a task is inside the allowed day window.
- Whether a task placement matches its duration.
- Whether a task overlaps fixed blocks.
- Whether a task overlaps already scheduled tasks.
- Whether fixed blocks are valid.
- What free time slots remain after fixed blocks are removed.

These functions answer the question:

> Is this placement allowed?

They do not score how good the placement is.

---

### `app/pert.py`

Handles dependency logic between tasks.

This file builds a dependency graph where the direction is:

```text
dependency -> task
```

For example, if `Math Review` depends on `Math Exam`, the graph stores:

```text
Math Exam -> Math Review
```

The PERT module checks for circular dependencies, computes valid dependency order, and validates whether scheduled tasks respect prerequisite timing.

Current behavior:

- Existing dependencies are enforced.
- Missing dependencies are ignored.
- Dependency cycles are treated as invalid.
- A dependent task must start after its prerequisite finishes.

---

### `app/reward.py`

Contains the scoring logic for valid task placements.

The reward function answers:

> Given that this placement is allowed, how good is it?

The score can depend on:

- Task priority.
- Preferred time window.
- Category weights.
- Exact task weights.
- Related tags.
- Spacing between neighboring tasks.
- Fragmentation penalties for awkward small gaps.

The reward system reads values from a YAML file. An explicit `config_path` passed to `load_reward_settings()`/`optimize_day_schedule()` is always used directly; otherwise, default discovery looks for `config/task_preference.yaml` (singular) in this project's own `config/` directory, anchored to `app/reward.py`'s own file location rather than the current working directory (so it works the same regardless of where the CLI/UI/tests are run from, and never walks ancestor or home directories). If that canonical filename is absent, a recognized legacy filename in the same directory is used instead (`task_preferences.yaml`, `task_prefrence.yaml`, `task_prefrence.yml`, `task_preference.yml`), for compatibility with a file created under an older supported name. Since neither the CLI (`app/main.py`) nor the desktop UI (`app/app.py`) needs to pass an explicit `config_path` for this default discovery to work, editing `config/task_preference.yaml` does affect `python -m app.main`/`python -m app.app` today. If no config file is found (or a found file is incomplete), safe built-in defaults fill in the rest.

---

### `app/optimizer.py`

Contains the main greedy scheduling algorithm, protected as **Greedy Optimizer v1**.

The optimizer works in this order:

1. Validate fixed blocks (`app.constraints.validate_fixed_blocks`): each must have `start_time < end_time`, fit inside the day window, and not overlap any other fixed block. An invalid fixed block raises `ValueError` immediately, before any scheduling happens.
2. Load reward settings.
3. Read the schedule's day start and day end.
4. Convert fixed blocks into already scheduled tasks.
5. Collect all flexible tasks.
6. Repeatedly consider every remaining flexible task.
7. For each task, find the best valid time slot.
8. Pick the task-placement pair with the highest reward score.
9. Lock that task into the schedule.
10. Repeat until all tasks are scheduled or no progress can be made.
11. Return scheduled tasks, unscheduled tasks, and total score.

This is a greedy algorithm, so it is designed to find good schedules quickly. It does not guarantee a mathematically optimal schedule because it does not try every possible full schedule and it does not move tasks after locking them in.

---

### `app/main.py`

The command-line entry point, a thin consumer of the same persistence-backed planning service as the desktop app. Plain startup (`python -m app.main`) only summarizes what is stored; it never imports sample data. With `--select-date` it allocates and generates that one date from the stored tasks and saves the result. With `--import-csv FILE --anchor-date YYYY-MM-DD` it first imports a legacy CSV (append or replace). `--demo` runs the sample fixture explicitly, in a throwaway in-memory database unless `--db-path` is given. See [Run the command-line scheduler](#3-run-the-command-line-scheduler).

Exports: the original `time,task` 30-minute-block CSV (legacy, lossy), the exact-interval JSON of the generated day, and the stored-planning CSV (`--export-planning-csv`).

---

### `app/app.py`

Contains the CustomTkinter desktop UI. On startup it opens the application database (`app/ui/app_services.py`) and builds every page on that one connection; if the database cannot be opened it shows the error instead of a scheduler whose changes could not be saved.

- Day, Week (7 days), and Month (30 days) pages, each showing real dates from an explicit, editable start date, in the configured timezone.
- A task form that creates and edits flexible tasks and fixed blocks. Dependencies are picked from the task table by row, so they are stored as task ids, never names.
- An Added Tasks table keyed by task/fixed-block id (Edit Selected / Remove Selected), so duplicate names are fine.
- **Make Schedule**, which allocates the page's dates, generates each date with the canonical day engine, and saves the whole range in one transaction.
- **Upload CSV...** (append or replace), **Export CSV...**, and a **Reset...** with explicit scope.
- An Execute tab for the saved placements, and the Productivity page.

Every widget callback is a thin call into a Tk-free presenter (`app/ui/schedule_page_controller.py`); the page is then redrawn from a fresh read of SQLite. Widgets contain no SQL and no scheduling logic.

---

### `app/execution/`

The local task-execution domain, used by both the desktop UI and the CLI report tool: `models.py` (execution/session/status models), `db.py` (the application database: per-user location and legacy adoption, ordered transactional migrations — including the v3 planning tables — and the `transaction()`/lock helpers every repository shares), `repository.py` (the only execution module with raw, parameterized SQL), `service.py` (the state machine — start/pause/resume/complete/skip, duplicate prevention, active-duration and start-delay calculations), and `exporters.py` (CSV/JSON export of raw execution history). See [Task Execution Tracking & Productivity Insights](#task-execution-tracking--productivity-insights-local-only) for usage and storage location.

---

### `app/productivity/`

Turns execution history into statistics and predictions: `data_prep.py` (flattens executions into analysis-ready records), `stats.py`/`segments.py` (aggregate statistics and groupings, each carrying its own sample count and evidence label), `prediction.py` (the median duration estimator and its documented fallback hierarchy — the production predictor), `insights.py` (structured, template-generated observations), `trends.py` (recent-vs-baseline comparison), `reporting.py` (the `ProductivityService` boundary and report/dashboard bundles), `exporters.py`, and `report_cli.py` (`python -m app.productivity.report_cli`).

The evidence-gated ML duration predictor (see [Evidence-gated ML duration predictor](#evidence-gated-ml-duration-predictor-experimental-disabled-by-default)) lives in its own small, focused modules: `ml_features.py` (leakage-safe feature construction), `ml_split.py` (chronological train/test splitting), `ml_model.py` (the sklearn `Pipeline` definition and fitting), `ml_metrics.py` (MAE/median-AE/tolerance-rate calculations), `ml_evaluation.py` (the three-way comparison and activation gate), `ml_persistence.py` (saving/loading the model artifact, never raising), `ml_prediction.py` (the runtime, fail-safe prediction path), and `predictor_comparison_cli.py` (`python -m app.productivity.predictor_comparison_cli`).

---

### `app/ui/`

Desktop-UI-facing controllers and widgets that connect `app/app.py` to `app/planning/`, `app/execution/`, and `app/productivity/`, keeping persistence, scheduling, and analytics logic out of UI callbacks:

- **Startup:** `app_services.py` opens the shared connection and closes it on shutdown.
- **Planning:** `planning_controller.py` delegates to `PlanningService` and holds allocation plus per-day generated/stale state; `schedule_page_controller.py` is the Tk-free presenter behind each schedule page.
- **Execution and productivity:** `execution_controller.py` and `productivity_controller.py`.
- **Background work:** `background.py` runs database and optimizer calls off the UI thread and tracks them so shutdown can wait for them.
- **Widgets:** `execution_panel.py`, `feedback_dialog.py`, `duration_suggestion.py`, `productivity_page.py`, `productivity_charts.py`.

---

### `config/settings.py`

Stores global project settings, such as:

- Time slot size.
- Default day start and end.
- Hard constraint toggles.
- Reward weight constants.
- Simulated annealing constants kept for future expansion.
- Neighbor generation settings kept for future expansion.
- The local data directory (`DATA_DIR`: the per-user application-data directory, or `SCHEDULE_MAXING_DATA_DIR`).

Some settings may be older or reserved for future optimizer versions. The current optimizer mainly uses the 30-minute slot structure and reward configuration loaded through `reward.py`.

---

### `config/task_preference.yaml`

Stores user-adjustable reward preferences. This is the canonical, auto-discovered filename (see `app/reward.py`'s discovery precedence above) — it is found by default without passing `config_path` explicitly.

This file can define:

- Reward weights.
- Maximum preferred-time distance.
- Category weights (the checked-in template lists the five primary categories — `health`, `enjoyment`, `study`, `work`, `chores` — at a neutral `1.0` each; any other category name remains valid and falls back to `1.0`).
- Exact task weights.
- Exact task preferred windows.
- Category preferred windows.
- Related tags.

This lets the optimizer's behavior be tuned without rewriting Python code: editing this file directly changes what `python -m app.main`/`python -m app.app` schedule, since default discovery finds it automatically. A file under one of the older supported names (`task_preferences.yaml` plural, `task_prefrence.yaml`/`.yml`, `task_preference.yml`) is still recognized as a legacy variant if this canonical filename is absent, but the canonical filename always wins when both exist.

---

### `samples/inputs/`

Contains ~20 descriptive, scenario-focused CSV input files for manually exercising specific behaviors of the scheduler. Each filename describes the *intended* scenario, not proof that every path currently rejects or handles it exactly as its name implies (some are deliberately invalid inputs used to inspect current error handling). Representative examples:

- `valid_single_day_basic.csv` / `valid_multi_day_two_days.csv` — ordinary valid schedules.
- `dependency_chain_linear.csv` / `multiple_dependencies_single_task.csv` / `dependency_missing_reference.csv` / `dependency_cycle_invalid.csv` / `dependency_name_contains_hyphen.csv` — dependency ordering, missing references, cycles, and the hyphen-splitting limitation (see [Dependency Behavior](#dependency-behavior)).
- `fixed_blocks_overlap_invalid.csv` / `day_fully_packed_fixed_blocks.csv` — fixed-block validation and a fully-booked day.
- `task_duration_exceeds_available_window.csv` / `end_of_day_boundary_1440.csv` — day-window and end-of-day boundary cases.
- `zero_duration_task_invalid.csv` / `priority_out_of_range_invalid.csv` — CSV rows rejected by `Task`'s Pydantic validation (duration must be `> 0`; priority must be `1`-`10`).

`app/main.py` currently loads `valid_single_day_basic.csv` by default.

---

### `samples/outputs/`

Contains generated schedule output files.

Currently, `valid_single_day_basic.csv` is used as the exported result from `app/main.py`.

---

### `tests/`

Contains the automated test suite, run with `python -m pytest`. It covers:

- `test_constraints.py`: overlap detection, day-window bounds, duration and fixed-block checks, free-slot computation.
- `test_pert.py`: dependency graph construction, cycle detection, topological order, and dependency-ready scheduling helpers.
- `test_reward.py`: YAML-backed reward configuration loading and each scoring component (priority, preferred-time, neighboring-tag, and fragmentation-penalty scoring).
- `test_optimizer.py`: the greedy optimizer end to end (fixed-block preservation, non-overlap, exact durations, day-window boundaries, dependency ordering and cycles, and unscheduled-task reporting).
- `test_data_processor.py`: CSV loading for fixed and flexible tasks, dependency parsing, and malformed/missing input handling.
- `test_main_time_formatting.py`: CLI time-display and CSV export helpers, including the `minutes_to_time(1440)` midnight/noon boundary.
- `execution/`, `productivity/`, `ui/`: the execution-tracking domain, productivity analytics, and desktop-UI-facing controllers.
- `execution/test_migration_v3.py`, `test_storage_location.py`, `test_transactions.py`: schema upgrades, rollback, legacy database adoption, and transaction/thread safety.
- `planning/test_planning_repository.py`, `test_application_service.py`, `test_csv_import.py`, `test_csv_export.py`: persisted planning data, deletion/replacement policy, and the CSV boundaries.
- `ui/test_schedule_page_controller.py`, `test_app_services.py`: the desktop callback boundary, startup/shutdown, and restart behavior, all headless. `ui/test_desktop_app.py` drives the real Tk widgets and is skipped when no display is available.
- `test_main_cli.py` and `test_milestone2_end_to_end.py`: the CLI startup contract and the full import → schedule → execute → reopen → export lifecycle.

Every test that opens a database uses a temporary file. `tests/conftest.py` also redirects the default data location to a temporary folder, so no test can touch your real database.

CI (`.github/workflows/ci.yml`) runs `python -m pytest`, `python -m compileall .`, and `python -m ruff check .` on every push and pull request.

---

## CSV Input Format

The scheduler expects CSV files with columns similar to:

```text
date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies
```

Example:

```csv
date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies
1,Sleep,sleep,fixed,True,0,480,480,1,
1,Study Math,study,math,False,540,720,120,10,
1,Math Review,study,review,False,780,960,60,8,Study Math
```

Time values are stored as minutes from midnight:

```text
0    = 00:00
480  = 08:00
720  = 12:00
1020 = 17:00
1440 = 24:00
```

For fixed tasks, `start_time` and `end_time` are the actual scheduled time.

For flexible tasks, `start_time` and `end_time` represent the preferred time window.

---

Allowed categories: study, work, exercise, errand, food, entertainment, other

## How the Optimizer Works

The current optimizer is a greedy reward-based scheduler.

First, all fixed tasks are placed into the schedule. These tasks act as blocked time ranges. Then the optimizer looks at all flexible tasks that still need to be scheduled.

For each flexible task, the optimizer scans the day in 30-minute increments. For every possible start time, it checks whether the task fits without overlapping existing scheduled tasks. If the placement is valid, the optimizer scores it using the reward function.

After every remaining task has been checked, the optimizer chooses the task and placement with the highest score. That task is permanently added to the schedule. The process repeats until no flexible tasks remain or no valid placement can be found.

This approach is fast and understandable, but it is not guaranteed to find the global optimum. Since it locks in one task at a time, an early good choice may block a later better overall combination.

---

## Dependency Behavior

Dependencies are used to force one task to happen after another.

Example:

```text
Math Review depends on Study Math
```

This means `Study Math` must finish before `Math Review` can start.

The dependency system follows these rules:

- If a dependency exists in the task list, it is enforced.
- If a dependency name is missing, it is ignored.
- If dependencies form a cycle, the schedule is invalid.
- If a task is scheduled before its dependency finishes, the dependency constraint fails.

This keeps the scheduler flexible while still supporting real prerequisite relationships.

---

## Running the Project

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
```

On Windows PowerShell:

```bash
.venv\Scripts\Activate.ps1
```

On macOS/Linux:

```bash
source .venv/bin/activate
```

---

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

The current dependencies include:

- `pandas`
- `python-dotenv`
- `PyYAML`
- `pytest`
- `pydantic`
- `customtkinter`

---

### 3. Run the command-line scheduler

From the project root:

```bash
python -m app.main
```

Prints a summary of the saved data in your application database; nothing is imported or changed. To try the sample without touching your data:

```bash
python -m app.main --demo
```

This imports `samples/inputs/valid_single_day_basic.csv` (day 1 = 2026-01-05) into a temporary in-memory database, schedules it, prints it, and writes `samples/outputs/valid_single_day_basic.csv` and `.exact.json`.

Import a CSV into your database and schedule its first date:

```bash
python -m app.main --import-csv my_week.csv --anchor-date 2026-03-02 --timezone Europe/Berlin
```

Schedule a stored date later, reusing unchanged placement ids:

```bash
python -m app.main --select-date 2026-03-03 --start-date 2026-03-02 --end-date 2026-03-08
```

Other options:

- `--db-path FILE`: use another database file.
- `--import-mode replace`: replace the dates the file covers.
- `--mode adhd_friendly`: the other day-engine mode.
- `--legacy-csv-out`, `--exact-json-out`, `--export-planning-csv`: exports.

`--csv` still works as an alias of `--import-csv`. See `python -m app.main --help`.

---

### 4. Run the desktop UI

From the project root:

```bash
python -m app.app
```

This opens the schedule optimizer, loads everything saved in the application database, and shows the Day page for today. Every change is saved immediately. **Make Schedule** saves the generated schedule. The **Execute** tab lists the saved schedule's flexible tasks for Start/Pause/Resume/Complete/Skip. The **Productivity** page shows the analytics (see [Task Execution Tracking & Productivity Insights](#task-execution-tracking--productivity-insights-local-only) below).

If the database cannot be opened (for example, the folder is not writable), the app shows the error and the database path instead of the scheduler. Nothing can be edited that could not be saved.

Times you enter are wall-clock minutes in the planning timezone: `UTC` unless you set `SCHEDULE_MAXING_TIMEZONE` (e.g. `SCHEDULE_MAXING_TIMEZONE=America/Toronto`). Set it to your own zone so start-delay statistics compare against real local times.

---

### 5. Run the productivity report from the command line (optional)

```bash
python -m app.productivity.report_cli
python -m app.productivity.report_cli --period week --format json --output report.json
```

Prints (or exports) the same productivity analysis the desktop Productivity page shows, from whatever local execution history exists. See `python -m app.productivity.report_cli --help` for all options.

---

## Output Format

There are three exports; only the stored-planning CSV and the exact JSON are exact:

- **Stored planning CSV:** desktop **Export CSV...** or CLI `--export-planning-csv`. One row per task, fixed block, and placement, with ids, exact UTC and local intervals, dependencies, versions, and timestamps. The contract is documented in `app/planning/csv_export.py`. It is for reading and backups; importing it is not supported.
- **Exact JSON:** CLI `--exact-json-out`. The generated `DayScheduleOutput`, serialized without loss.
- **Legacy half-hour CSV:** CLI `--legacy-csv-out`, described below. **Lossy:** a task shorter than 30 minutes or off the 30-minute grid is not represented exactly, and it carries no ids.

The legacy command-line exporter creates a CSV with two columns:

```text
time,task
```

Example:

```csv
time,task
00:00,Sleep
00:30,Sleep
01:00,Sleep
...
09:00,Study Math
09:30,Study Math
10:00,Study Math
```

If no task is active during a 30-minute block, the task value is:

```text
-
```

---

## Algorithm Summary

The project uses the following scheduling pipeline:

```text
Load CSV or UI input
        ↓
Build Task, FixedBlock, and DaySchedule objects
        ↓
Place fixed tasks first
        ↓
Use PERT dependency logic to control task order
        ↓
Try valid placements for flexible tasks
        ↓
Score placements using reward.py
        ↓
Greedily choose the highest-scoring valid placement
        ↓
Return scheduled and unscheduled tasks
        ↓
Display result in UI or export to CSV
```

---

## Task Execution Tracking & Productivity Insights (Local Only)

Beyond planning a schedule, the app can optionally track what actually happened when you work a task, and turn that history into productivity insights. This is entirely local: no server, no cloud storage, no telemetry, no accounts.

### Where your data is stored

Execution history and persisted planning data (tasks, projects, fixed blocks, generated placements) live in a single local SQLite file, `executions.db`, in your per-user application-data directory — independent of the current working directory and of where this checkout lives:

| Platform | Default location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\ScheduleMaxing\executions.db` |
| macOS | `~/Library/Application Support/ScheduleMaxing/executions.db` |
| Linux/other | `$XDG_DATA_HOME/ScheduleMaxing/executions.db` (default `~/.local/share/...`) |

Override the directory with the `SCHEDULE_MAXING_DATA_DIR` environment variable (unchanged), or pass an explicit path (`get_connection(db_path=...)`, `--db-path` on the CLIs). The database and its schema are created and migrated automatically the first time the app or a CLI opens it.

**Existing repository-local database (before Milestone 2 the default was `<project root>/data/executions.db`).** On the first default open (no explicit path, no `SCHEDULE_MAXING_DATA_DIR`), if that file exists and the per-user database does not, it is **copied** to the per-user location with SQLite's backup API (read-only on the source; written to a temporary file and only then linked into place) and then migrated there; a warning is logged. The original file is never modified, moved, or deleted — it stays behind as a backup you can remove yourself. If both files exist, nothing is copied or merged: the per-user database is used and the old one is left untouched. To keep using the old file in place instead, set `SCHEDULE_MAXING_DATA_DIR=<project root>/data`. (An `ml_duration_model.joblib` saved under the old `data/` is not copied; move it yourself or re-run `--save-model`.)

`.gitignore` excludes `data/` and every SQLite file and side file (`*.db`, `*.sqlite`, `*.sqlite3`, `*-journal`, `*-wal`, `*-shm`), so none is ever committed.

If you run `python -m app.productivity.predictor_comparison_cli --save-model` and the ML activation gate passes, the trained model is saved in the same directory as `ml_duration_model.joblib` plus a `ml_duration_model.meta.json` sidecar.

### Recording execution (desktop UI)

After running **Make Schedule** on the Day/Week/Month page, open the **Execute** tab (next to Added Tasks/Unscheduled) to:

1. Pick a saved, scheduled flexible task from the dropdown (fixed blocks like sleep/meals are not tracked as executions). Its status and sessions are restored from the database; merely selecting it creates nothing.
2. Use whichever actions are enabled for its current state:
   - **Start** (scheduled → in progress)
   - **Pause** (in progress → paused)
   - **Resume** (paused → in progress)
   - **Complete** (in progress or paused → completed)
   - **Skip** (scheduled, in progress, or paused → skipped)
3. Watch the live **active time** counter — it only counts time while the task is in progress; time spent paused is excluded.
4. On Complete or Skip, optionally record a focus rating (1–5), energy rating (1–5), interruption count, and a short note. Every field is optional.

Executions are identified by task and placement id, never by name. The first Start or Skip on a placement creates its execution; re-selecting that placement later, including after reopening the app, shows the same execution with its sessions and feedback, never a duplicate. Re-running **Make Schedule** keeps a placement's id when it is unchanged. If it moves, the new placement gets its own execution, and the old execution keeps its history and planned snapshot.

### The Productivity page

A **Productivity** item appears in the sidebar once the local database is available. It shows, for the selected filters:

- Completed and skipped task counts, completion rate, productive active time, median start delay, and duration-estimate error (mean absolute error between planned and actual duration).
- **Planned vs. actual duration by category** and **completion rate by time bucket**, as simple bar charts drawn directly with Tkinter (no external plotting library).
- The **best-supported time bucket per category** — the time of day with the most completed, duration-bearing history for that category.
- A **recent trend** comparing the last 7 days against your current filter selection.
- **Insights** — short, data-derived sentences such as "Study tasks completed in the morning have a completion rate of 82% across 17 observations," never hardcoded text and never from an AI model.

Filters: a time window (all time / last 7 / 30 / 90 days), category, tag, day of week, and time bucket (Night/Morning/Afternoon/Evening — see `app/productivity/buckets.py` for the exact boundaries).

**Every statistic on this page is paired with its evidence** — an observation count and an evidence label (`insufficient` / `low` / `moderate` / `high`). A segment with too little history is shown honestly (e.g. "Not enough history to recommend a time for errand") rather than presented as a confident conclusion.

### Duration suggestions when adding a task

In the task-entry form, once you've filled in a category and start time, click **Suggest duration from history** to see a historical prediction alongside your own entered duration, with the evidence behind it (e.g. "Based on 12 completed study tasks in the morning" or "Not enough history yet — using your original estimate"). The suggestion is never applied automatically — click **Use suggestion** to fill it into the (still freely editable) duration field yourself.

This suggestion always comes from the median predictor described above (`app/productivity/prediction.py`) — it is the safe default and the only predictor the UI currently reads from. See the next section for an optional, separately evaluated machine-learning predictor that can only replace it once it has proven itself on your own real history.

### Evidence-gated ML duration predictor (experimental, disabled by default)

Alongside the median predictor, this repo includes an optional scikit-learn regression model, evaluated honestly against both the median predictor and your own original estimate before it is ever allowed to make a real prediction. This is not a claim that ML is better — it is a mechanism for finding out, on your own history, whether it actually is.

**What it's allowed to see.** Only information known before a task begins: planned duration, category, tag, priority, planned start time (and the time-of-day bucket derived from it), day of week, and historical median-duration aggregates computed only from tasks completed *before* the task being predicted (a strict, chronologically-ordered "no future data" rule — see `app/productivity/ml_features.py`). It never sees actual duration, focus/energy/interruption feedback, or completion outcome as an input.

**How it's evaluated.** `app/productivity/ml_evaluation.py` sorts your completed, plausible-duration history chronologically and splits it into a training set and a held-out test set by time (the most recent `--test-fraction`, default 20%) — never a random split. All three approaches are then scored on the exact same held-out test rows:
- your original estimate,
- the median predictor (re-run per test row using only the history that existed strictly before that row, so it doesn't get to "see" the test set either),
- the ML model (an sklearn `Pipeline` — median/most-frequent imputation, one-hot encoding with unknown categories handled safely, and `Ridge` regression — trained only on the training rows).

Reported for each: mean absolute error, median absolute error, percentage of predictions within 15 and within 30 minutes, sample counts, and the exact chronological cutoff.

**Activation rule.** ML can only become active when *both* are true: your history clears configurable, conservative minimum-sample thresholds (defaults: 40 total / 25 train / 10 test observations), and its held-out mean absolute error is strictly lower than the median predictor's held-out mean absolute error on that same test set. If either condition fails, ML stays disabled and every prediction keeps coming from the median predictor — nothing about the existing "Suggest duration from history" behavior changes. The concrete numbers behind every enable/reject decision are recorded, never asserted without evidence.

**Reproducing it yourself:**

```bash
python -m app.productivity.predictor_comparison_cli
```

Add `--format json` for machine-readable output, `--db-path` to point at a different execution database, `--min-total-samples`/`--min-train-samples`/`--min-test-samples`/`--test-fraction` to adjust the gate, and `--save-model` to persist the trained model (as `ml_duration_model.joblib` + `ml_duration_model.meta.json` under the same local data directory as `executions.db`) so it can be used at runtime — only written when the activation gate actually passes. Run `python -m app.productivity.predictor_comparison_cli --help` for the full list.

On a fresh install (no completed execution history yet), this correctly reports "insufficient history" and leaves ML disabled — that is the expected, honest result, not an error.

### Exporting and resetting your data

On the Productivity page's **Data** section:

- **Export history (CSV)** / **Export history (JSON)** save your complete raw execution history (including your own notes) to a file you choose.
- **Reset local history...** permanently deletes all locally stored execution data. This is destructive, requires an explicit confirmation dialog, and is kept in its own section, separate from normal navigation. It does not delete planning data. Conversely, a schedule page's **Reset...** never deletes execution history (see [Saved data](#saved-data-sqlite-import-export-reset-and-backups-milestone-2)).

### Privacy

- All execution and productivity data stays on your device, in the single SQLite file described above.
- Nothing here is sent to a server, cloud service, or third party.
- Application error messages and logs never include your notes or other personal feedback content — only the export you explicitly request does.

### Screenshots

This README does not embed screenshots of the desktop UI. To add your own: run `python -m app.app`, open the Execute tab and the Productivity page, and use your OS's screenshot tool (Windows: `Win+Shift+S`; macOS: `Cmd+Shift+4`), then reference the saved image(s) here with standard Markdown image syntax, e.g. `![Productivity page](docs/screenshots/productivity.png)`.

---

## Schedule Maxing v2: Canonical Planning Architecture

Alongside the original (Milestone 0) name/day-index-based scheduling described above, this codebase now also has a parallel **canonical** planning layer (`app/planning/`) built around stable UUID identity, real calendar dates, and aware UTC instants. Both layers coexist: `app/models.py`'s `Task`/`FixedBlock`/`ScheduledTask` and Greedy Optimizer v1 (`app/optimizer.py`'s `optimize_day_schedule`) are unchanged and remain available as the protected legacy baseline (`app/data_processor.py`, benchmarks, and tests). Since Milestone 2, the desktop app and the CLI run on the canonical layer and its SQLite persistence.

### Canonical models and identity

`app/planning/models.py` defines `Task`, `FixedBlock`, `ScheduledTask` (a placement), `DaySchedule`, `DayScheduleOutput`, `Project`, and `RecurrenceSpec` — every entity has a stable `uuid.UUID` id that survives serialization, edits, and repeated optimization. `ScheduledTask` intentionally carries no name/category/tags of its own (read them through a `TaskRegistry` and `project_scheduled_task_display`); duplicate task names are fully supported and never collide, since everything is matched by id, not name. `RecurrenceSpec` is model-only: nothing in this codebase expands a recurring template into concrete occurrences yet.

### Time contract

`app/planning/time.py` uses `datetime.date` for calendar dates, aware `datetime` for instants, and an explicit IANA timezone identifier for local scheduling — never a naive datetime silently treated as UTC. A local day window's end is explicit (`LocalDayWindow`'s `end_day_offset`): "24:00" and a following-midnight endpoint both resolve to the identical instant. This milestone supports windows that stay within one local day (including ending exactly at the next midnight); a window that would need to extend further, or that crosses a DST/offset transition, is rejected explicitly (`UnsupportedSchedulingWindowError`) rather than silently mishandled. Ambiguous (DST fall-back) and nonexistent (DST spring-forward gap) local times are also rejected explicitly.

### Legacy CSV import and identity

`app/planning/compat.py` converts legacy CSV rows (the same shape `app/data_processor.py` reads) into canonical models via `import_legacy_csv_rows(rows, anchor_date=..., tz_name=...)`. The anchor date is always explicit — day 1 == `anchor_date`, day N == `anchor_date + (N-1)` days — and is never inferred from today's date. IDs are minted fresh on every import; **re-running an import on the same CSV does not preserve identity across runs** (only a canonical JSON document, once created, preserves identity across edits/reloads — see below). Dependency-name resolution prefers an exact full-name match (so a hyphenated task name like `Pre-Calc Review` is never mis-split), accepts explicit semicolon-separated references, falls back to the old hyphen-delimited convention only when it resolves unambiguously, and reports (rather than guesses at) an ambiguous duplicate-name reference or a genuinely missing one. The persistence importer used by the desktop app and CLI (`app/planning/csv_import.py`) applies the same rules across the whole file, and treats those reports as errors that reject the file (see [Saved data](#saved-data-sqlite-import-export-reset-and-backups-milestone-2)).

### YAML reward configuration

`app/reward.py` now discovers `config/task_preference.yaml` (singular) automatically, anchored to this project's own directory rather than the current working directory (no ancestor/home-directory walking). Precedence: an explicit `config_path` always wins; otherwise the canonical singular filename; otherwise a recognized legacy filename in the same `config/` directory (`task_preferences.yaml` plural, `task_prefrence.yaml`/`.yml`, `task_preference.yml`), for anyone who already created a config file under an older name; otherwise built-in defaults. The checked-in template lists the five primary categories (`health`, `enjoyment`, `study`, `work`, `chores`) at a neutral `1.0` multiplier each; any other category name remains valid and falls back to `1.0`.

### Per-day preferences

`app/planning/preferences.py`'s `DayPreferences`/`resolve_day_preferences` layer, in increasing precedence: built-in defaults → the YAML template → an optional in-memory "user" override → a date-specific override. For the two per-category maps (`category_multipliers`, `category_preferred_windows`), a layer distinguishes an *absent* key (inherit unchanged) from an *explicit* value (override, including a deliberate `0.0`) from an *explicit `None`* (clear a lower layer's override). Every resolution returns independent data — editing one date's override can never mutate another date's, the YAML layer, or an already-resolved `DayPreferences`. `day_preferences_to_reward_settings` adapts a resolved `DayPreferences` into the legacy `RewardSettings` shape so the frozen scoring formula (`priority × importance_weight × category_multiplier × task_multiplier`) is reused unchanged; a category multiplier of `1.0` always leaves scoring unaffected.

### The canonical day engine: precise_greedy and adhd_friendly

`app/optimizer.py`'s `generate_day_schedule(day_schedule, preferences, ...)` is the canonical counterpart to Greedy Optimizer v1, evolved in the same module rather than as a separate stack. Two candidate modes, selected via `DayPreferences.optimizer_mode` (default: `precise_greedy`):

- **`precise_greedy`** — one-minute candidate resolution, no global snapping; a task starts the instant it becomes feasible (e.g. immediately at 10:13).
- **`adhd_friendly`** — a task longer than 30 minutes starts only on a local wall-clock quarter-hour boundary; a task at or under 30 minutes may start on any valid minute, exactly like `precise_greedy`. Durations are never rounded or split in either mode.

Both modes share the same hard constraints and base scoring, plus a **bounded, ADHD-only short-gap-filling bonus** (`app/reward.py`'s `_short_gap_bonus_score`, gated by `calculate_task_score(..., adhd_mode=True)`): a short task that starts flush against a neighbor/day-boundary where the pre-placement gap was already small enough to be fragmentation-penalized earns a bonus proportional to how close to fully flush it lands, capped per placement (`short_gap_bonus_cap`) and disabled entirely with `short_gap_bonus_weight=0`. It never fires in `precise_greedy` mode.

**Mandatory scheduling**: fixed blocks are placed first, then every `required` task plus the full transitive closure of its dependencies (an optional prerequisite of a required task is scheduled as essential for that run without mutating its own stored `required` flag), then optional tasks. A successful `DayScheduleOutput` always contains every required task exactly once; otherwise `generate_day_schedule` raises `MandatoryTaskSchedulingError` with one structured `MandatoryTaskFailure` per affected task (a reason code, an explanation, and `proven_infeasible` — `True` only when a coarse capacity/individual-fit bound *proves* no placement could succeed, `False` when this particular greedy run simply failed to find one). This is a greedy engine, not an exact solver, and never overstates a greedy failure as mathematical infeasibility.

**Identity across regenerations**: pass a prior `DayScheduleOutput` as `previous_result` and an unchanged placement (same task, same interval) keeps its placement id; any other placement (new, or the same task moved to a different interval) gets a fresh id — an existing execution record pointing at the old placement is never silently redirected.

Provisional performance: see `benchmarks/FINAL_COMPARISON.md` — under matched scoring configuration, the canonical engine reproduces Greedy Optimizer v1's score exactly on simple fixtures and slightly *exceeds* it on more contended ones (finer resolution finds marginally better placements). See the next section for how that resolution is now found without scoring every minute, and `benchmarks/EVENT_CANDIDATE_COMPARISON.md` for the current (post-event-search) timing.

### Exact event-based candidate search (Task 6)

`_best_candidate_for_canonical_task`/`_best_candidate_for_task` (Greedy Optimizer v1's own placement search) no longer score every feasible minute to find a task's best placement. One-minute precision does **not** require one-minute enumeration: each free interval is decomposed into a small set of analytically-derived candidate starts ("events") — every point where some reward component's closed form could change (a preferred-window boundary, its center and decay zero-crossings, a neighbor's related-tag or fragmentation gap threshold, a bounded ADHD short-gap-bonus zone) — plus a provably-monotonic binary search (`_earliest_best_in_continuous_range`) between consecutive events to find the *exact* earliest minute achieving the real, rounded `calculate_task_score`'s maximum, even across a long, nearly-flat rounded-score plateau. The ADHD short-gap bonus's own internal rounding is handled by exhaustively evaluating only its small, bounded "active zone" (at most `min_gap_between_tasks_minutes` wide) rather than being trusted to keep a region monotonic. See `app/optimizer.py`'s "Shared event-search machinery" section docstring for the full breakdown of which reward component drives which candidate point, and for why every omitted minute provably cannot improve on — or win an earlier-start tie against — an evaluated one.

`adhd_friendly` tasks over the 30-minute threshold, and every Greedy Optimizer v1 (legacy) candidate, are grid-constrained rather than 1-minute; since a grid already bounds the candidate count (≤96 quarter-hour points, ≤48 half-hour points, even for a full day), those candidates are evaluated directly rather than run through the monotonic-search machinery — grid-constrained "event search" is exhaustive-on-its-own-lattice by construction, not a further reduction. Legacy's 30-minute lattice and its exact snapping/scoring behavior are otherwise unchanged; it is not silently turned into a canonical consumer.

An independent, deliberately-separate **exhaustive reference** for each search (`_best_candidate_for_canonical_task_exhaustive`, `_best_candidate_for_task_exhaustive`) is kept in `app/optimizer.py` for tests and benchmarks only — never called by `generate_day_schedule`/`optimize_day_schedule` (see `tests/test_optimizer_differential.py`'s spy test). `tests/test_optimizer_candidates.py` and `tests/test_optimizer_differential.py` compare the two exhaustively across seeded scenarios (durations, both modes, signed/zero weights, near-cancelling slopes, thresholds, dependency/mandatory cases) and assert exact score and earliest-start parity, plus a substantial (order-of-magnitude, on a large sparse interval) reduction in real `calculate_task_score` calls. `python -m benchmarks.event_candidate_comparison` (see `benchmarks/EVENT_CANDIDATE_COMPARISON.md`) benchmarks the two on real workloads and fails loudly on any semantic mismatch.

Four narrowly-scoped regressions surfaced while building this and were fixed first, before the exhaustive reference was written (so it reflects intended, not buggy, behavior):
- A day whose usable window did not start at local midnight compared a task's preferred window (local minutes-from-midnight) directly against day-window-relative offsets — silently shifting the effective preferred window by the day's own start offset. Fixed in `_best_candidate_for_canonical_task` via `_normalize_preferred_window`.
- `compute_required_closure`'s returned `set` gave `generate_day_schedule`'s required tier an unordered (set/hash) iteration order instead of the original task order, so two equal-scoring required tasks could tie-break inconsistently. Fixed by deriving tier order from `movable_ids` (already input-ordered) instead of `list(essential_ids)`.
- `day_preferences_overrides_from_reward_settings` dropped all three `short_gap_bonus_*` fields and had no `tag_relations` field anywhere in `app/planning/preferences.py`, so YAML-configured short-gap bonus/tag-relation settings never reached the canonical engine. Fixed by adding `tag_relations` to `RewardPreferences`/`RewardPreferencesOverride` and propagating all four fields through the adapters.
- `_to_offset` used `round()` on an aware instant's offset, silently accepting sub-minute precision. It now explicitly rejects (raises `ValueError`) any fixed-block/deadline/external-dependency instant not aligned to a whole minute, rather than rounding a boundary and risking a false overlap or an early/late completion.

### Week/month allocation vs. selected-day generation

`app/planning/allocation.py`'s `allocate_tasks` decides **which date** each task goes on across a real date range (`week_dates`/`month_dates` use actual calendar arithmetic, including leap Februarys and year boundaries) — it never computes a minute-level start/end time and never calls the day engine (verified in tests and benchmarks via an instrumented spy, not inferred from timing). It reserves fixed-block occupancy and schedules required tasks (plus their dependency closure) before optional ones, respects `required_date`/deadlines/cross-date dependency ordering, and ranks feasible dates deterministically (preferred dates, then the day's own category preference, then remaining capacity, then earliest date as the final tie-break). A required task that cannot be placed is reported in `unallocated` with `required=True` — allocation itself never raises, so a partial result (e.g. "11 of 12 required tasks fit this week") stays useful. Allocation succeeding does not prove a full intra-day schedule exists: `app/planning/service.py`'s `generate_selected_day` is the only thing that actually calls the day engine, exactly once, for exactly one explicitly selected date — never the rest of the week/month, and a coarse-feasible allocation can still legitimately fail at that detailed stage (and does so loudly, via the same `MandatoryTaskSchedulingError`). `DayResultStatus` (`allocated` / `generated` / `stale`) tracks this: a fresh allocation run marks every previously generated day stale rather than leaving it looking current.

### CLI (Task 6, SQLite-backed since Milestone 2)

`python -m app.main` is a thin consumer of the canonical service. Its anchor/timezone contract and its selected-day contract are unchanged:

- **Anchor and timezone:** a legacy CSV is imported under an explicit `--anchor-date`/`--timezone`. `--demo` uses the documented fixed sample anchor `2026-01-05`, never today's date.
- **Selected day:** the file's (or the given) date range is allocated, and exactly one selected date is generated. `--select-date` defaults to the file's first date, so a multi-day CSV is never generated in full.
- **Mode:** `--mode` selects `precise_greedy`/`adhd_friendly`.
- **Persistence:** the import and the generated day are saved to the database, and plain startup reads it without importing anything.
- **Exports:** the original half-hour CSV (legacy, lossy), the exact JSON, and the stored-planning CSV.

An ID-less legacy CSV gets new ids on every import (see below).

### Execution tracking migration (Task 2)

`app/execution/`'s SQLite schema is now at version 2: `cancelled` is a new terminal status (alongside `completed`/`skipped` — no transitions are allowed out of any terminal status); `planned_date`/`planned_start`/`planned_end` are now nullable (a canonical-only execution has no legacy day-index snapshot); and new canonical columns (`task_id`, `scheduled_task_id`, `user_id`, `canonical_planned_date`/`timezone`/`planned_start`/`planned_end`, `actual_first_start_at`, `actual_final_end_at`, `version`) coexist with the original legacy columns, migrated automatically and non-destructively the first time the app or CLI opens the database (row counts, sessions, feedback, and even non-UUID legacy ids are all preserved; **unknown historical dates are left unset, never guessed from `created_at`**). `ExecutionService.create_canonical_execution`/`get_or_create_canonical_execution` are the new identity-aware creation API, keyed on `task_id`/`scheduled_task_id` (not task name) — re-selecting the same placement always reuses its execution, and a moved placement never overwrites the original execution's planned snapshot. Legacy `create_execution`/`get_or_create_execution` are unchanged. Productivity analytics (`app/productivity/data_prep.py`) use a canonical execution's real planned date for weekday analysis when available, falling back to the original `created_at`-based approximation for legacy rows exactly as before; `cancelled` counts as terminal but never as completed or skipped.

### Planning persistence (Milestone 2)

Schema version 3 (`app/execution/db.py`, same file and migration chain as execution history) stores the canonical planning entities relationally: `projects`, `tasks` with queryable columns (required date, preferred window, deadline plus a normalized UTC twin, recurrence fields, ownership, `created_at`/`updated_at`/`version`) and child tables for tags, preferred dates, dependencies, and recurrence weekdays; `fixed_blocks`; and `scheduled_tasks` placements (only the arbitrary `optimization_metadata` is a small JSON column). Instants keep their original UTC offset for an exact round trip. `app/planning/repository.py` is the only planning module with SQL; `app/planning/application.py`'s `PlanningService` owns the rules (task CRUD, fixed blocks, range loading, scoped placement saving), and `PlanningController` now delegates every authoritative read/write to it — returned models are fresh snapshots, while allocation results and per-day generated/stale state stay in-memory render state. Generating a day saves that date's placements, reusing stored placement ids for unchanged placements so linked execution history survives regeneration and restarts.

- **Migrations** are ordered, contiguous, and each runs in one transaction with its `user_version` bump; a failure rolls that migration back completely and leaves the previous version usable. `PRAGMA foreign_key_check` must pass before each migration commits and `PRAGMA quick_check` after all of them; a database newer than the code is refused. Foreign keys stay enabled on every normal connection.
- **Transactions**: connections run in autocommit mode and `transaction()` owns every transaction. The outermost call begins/commits; nested calls (a repository method inside a service operation) are savepoints that can never commit early. Each `ExecutionService` mutation (e.g. start = status change + new session + first-start time) is one transaction. A per-connection re-entrant lock, shared by every repository on that connection, is held for a whole transaction, so background-thread work cannot interleave with it.
- **Execution ↔ planning links**: `executions.task_id`/`scheduled_task_id` are historical identity, not cascading foreign keys. Rows from before v3 keep their ids even though their tasks/placements were never persisted: nothing is fabricated and no date is guessed. Newly created executions must reference a persisted task (and a persisted placement of that same task), enforced by triggers. Once written, those ids are immutable.
- **Deletion/replacement**: deleting a task that another task still depends on, or a project that still has tasks, is refused. Deleting a task removes its own child rows and placements. `replace_placements(start, end, ...)` only touches placements dated inside that range, and refuses to move one from outside it. Execution history is never deleted, changed, or used to block an edit. A removed history-linked placement is reported back, and its executions keep their ids and planned snapshot.
- **Range eligibility** follows allocation's own hard date rules. A task with `required_date` is eligible only when that date is inside the range. Otherwise, a task with a deadline is eligible when the deadline's UTC date is on or after the range start. Every other task is eligible. The order is `(created_at, id)`.

### Model-only scope: recurrence and projects

`RecurrenceSpec` and `Project` are persisted with their tasks, but remain data models only — there is no recurrence-expansion engine and no project-management service or UI.

### Saved data: SQLite, import, export, reset, and backups (Milestone 2)

**One database.** Planning data and execution history live in one SQLite file, `executions.db`. By default it is in your per-user application-data folder (see [Where your data is stored](#where-your-data-is-stored)). `SCHEDULE_MAXING_DATA_DIR` changes the folder, and `--db-path` (CLI) or `open_app_services(db_path=...)` selects a file directly.

**Upgrades and the former `executions.db`.** The schema is upgraded automatically, in order, each step in its own transaction; a failed step leaves the previous version intact. The first default open copies the old repository-local `data/executions.db`, if one exists and the per-user file does not. The original is left untouched and never merged; details are in the section linked above.

**Desktop data flow.** Every change in the desktop app goes through: widget callback → `SchedulePageController` → `PlanningController` → `PlanningService` → repository → SQLite. The change is committed first, and then the page is re-read from SQLite. A failed change shows its error and redraws the committed state, so the UI never shows unsaved data as saved.

- **Make Schedule:** generates every date of the page (one selected-day generation per date) and then saves the range in one transaction. If generation or saving fails, the previously saved schedule stays.
- **Placement reuse:** saved placements are reused as the previous result, so an unchanged placement keeps its id, and any execution linked to it, across regenerations and restarts.
- **After a restart:** allocation and "current/out of date" labels are not stored. A saved schedule from an earlier session is therefore shown as *out of date*, never as current, and it is kept until you run Make Schedule again.

**CSV import** (`app/planning/csv_import.py`; desktop **Upload CSV...**, or CLI `--import-csv`):

- **Validation first.** The whole file is validated before anything is written, and any problem rejects the whole file with line numbers.
- **Anchor.** Day 1 is the page's start date (desktop) or `--anchor-date` (CLI).
- **Dependencies.** They are resolved across the whole file: the same day's names first, then the rest of the file if the name is unique. A missing or ambiguous (duplicate-name) reference, a self-reference, a cycle, or overlapping fixed blocks is an error, never guessed or silently dropped. The standalone `app/planning/compat.py` adapter keeps its legacy drop-and-report behavior for its own callers.
- **Identity.** Legacy rows have no ids, so every import creates new entities; appending the same file twice stores it twice. There is no identity-preserving CSV import.
- **Append** adds every row.
- **Replace** first deletes the placements, fixed blocks, and tasks planned on every date from the file's first to its last day, then adds the file. Undated tasks and anything outside that span are kept. Execution history is kept too, including history linked to replaced placements. If a task outside the span depends on one inside it, the whole import is refused.
- **All or nothing.** Any failure, including during a replace's deletions, rolls back everything.

**Reset** (desktop **Reset...**) has two explicit scopes for the page's dates: *only the saved schedule*, or *schedule, fixed blocks, and tasks planned on these dates*. Both require confirmation, and neither deletes execution history; that has its own reset on the Productivity page. Deleting a task that another task depends on is refused with the dependent's name.

**Export** reads SQLite and never writes to it. See [Output Format](#output-format) for what each export contains and which ones are lossy. Execution-history exports (Productivity page) are unchanged.

**Backups and shutdown.** Closing the window waits for background work, such as a running Make Schedule, then closes the database cleanly. To back up, close the app (and any CLI run), then copy `executions.db`. The WAL/journal side files (`-wal`, `-shm`, `-journal`) exist only while the database is in use.

**Visual check.** The desktop flow was smoke-tested on Windows with a display: create, edit, and delete; Make Schedule; Start in the Execute tab; close and reopen with the tasks, schedule, and "In progress" execution restored; Reset; CSV import and export. `tests/ui/test_desktop_app.py` automates the same flow wherever a display exists. Manual checklist for other platforms:
1. `python -m app.app` opens on the Day page for today, with no sample data.
2. Add a fixed block and two tasks with the same name, edit one, delete one, and pick a dependency from the table.
3. Run Make Schedule; the status says the saved schedule is current.
4. Start a task in the Execute tab, close the app, and reopen it. The tasks and schedule are there, the schedule is labelled out of date, and the execution is still "In progress".
5. Upload a CSV with Append and then Replace, try an invalid CSV (nothing changes), use Export CSV..., and use Reset... with each scope.

---

## Current Limitations

- The optimizer is greedy and does not guarantee the global best schedule.
- Once a task is placed, the optimizer does not move it again.
- Some settings related to simulated annealing are present but not fully used by the current optimizer.
- Error messages for unscheduled tasks are currently general (`"not enough valid space or unresolved dependency cycle"`) and could become more specific in the future. Actual dependency cycles are instead reported precisely and immediately, as a `ValueError`, before scheduling even starts.
- **(Fixed)** Reward-config file discovery previously did not match the checked-in filename — the checked-in file was `config/task_preferences.yaml` (plural) but default discovery only looked for singular/misspelled variants, so it silently never found it. The checked-in template is now `config/task_preference.yaml` (singular), matching default discovery exactly; see `app/reward.py`'s module docstring for the full precedence (explicit `config_path` > canonical `config/task_preference.yaml` > a recognized legacy filename in `config/` > built-in defaults). `tests/test_reward.py` covers this precedence directly.
- **The legacy CSV loader keeps its hyphen bug.** `app/data_processor.py` (the legacy Greedy Optimizer v1 input path) still splits a flexible task's `dependencies` field only on `"-"`, so a hyphenated dependency name such as `Pre-Calc Review` is mis-split. `tests/test_data_processor.py` characterizes this as current behavior. The desktop app and CLI no longer use that loader: their importer applies `app/planning/compat.py`'s rules (a whole-name match first, so hyphenated names are preserved).
- `weight_category_bonus` is loaded from YAML (`weights.category_bonus`) into `RewardSettings` but is not read anywhere in `calculate_task_score` — only the separate `category_weights` per-category multiplier dict actually affects scoring. `tests/test_reward.py` characterizes this as current behavior.
- Fixed-block validation (`app.constraints.validate_fixed_blocks`) is now enforced by the shared optimizer (`app/optimizer.py`'s `optimize_day_schedule`/`combine_fixed_and_optimized_scheduled_tasks`) in addition to the desktop UI's own pre-existing checks: overlapping, non-positive, or out-of-day-window fixed blocks are rejected with a `ValueError` before scheduling begins, for both the CLI and UI entry points.
- Legacy executions (created before Milestone 2, or through `ExecutionService.create_execution`) are still identified by their planned snapshot, and their weekday statistics use when the record was created. Desktop executions are now canonical: identified by task/placement id, with real planned dates.
- A saved schedule from an earlier session is always shown as *out of date* after a restart, even if nothing changed. Allocation and staleness are not persisted; they are conservatively restored instead.
- The planning timezone defaults to `UTC` (Python's standard library cannot reliably detect your IANA zone on Windows); set `SCHEDULE_MAXING_TIMEZONE`.
- The Month page shows 30 days from its start date, not a calendar month. Canonical fixed blocks have no category, so the desktop draws all of them in the "fixed" color. The desktop form keeps the legacy 30-minute grid, although CSV imports and the engine are minute-precise.
- Make Schedule replaces only the page's own dates. A task planned on a page's dates but allocated to another day keeps any older placement it has on dates outside the page. A dependency outside the scheduled range is reported as unresolved rather than treated as done.
- Saving is last-write-wins for a single local user: there is no optimistic-concurrency check, and no multi-device synchronization.
- There is no identity-preserving CSV import (legacy rows carry no ids), and an ML model file saved under the old `data/` folder is not copied to the new data folder.
- The Reward Config page changes only the legacy Greedy Optimizer v1's runtime settings; the desktop scheduler reads `config/task_preference.yaml`.
- The ML duration predictor is evidence-gated and, on a typical personal-scale history, is expected to stay disabled (the stock test fixture's 19 completed tasks are well below its default 40/25/10 sample thresholds) — this is by design, not a defect. It is not wired into the desktop UI; use `python -m app.productivity.predictor_comparison_cli` to evaluate and, if it qualifies, persist it.
- The event-based candidate search's rounding-aware tie-breaking (see "Exact event-based candidate search" above) relies on the real, rounded `calculate_task_score` staying monotonic across an analytically-derived region; this holds exactly for real-number arithmetic and is validated empirically (fuzz-tested and differentially tested against an independent exhaustive reference across tens of thousands of scenarios, with zero mismatches found) rather than machine-checked for every possible floating-point evaluation order at astronomically extreme weight magnitudes.

---

## Verification

Using the project's `.venv`:

```bash
python -m pytest
python -m compileall .
ruff check .
```

Also run the CLI end to end after any change to the CSV loading, optimizer, or export path (the demo never touches your saved data):

```bash
python -m app.main --demo
```

`tests/ui/test_desktop_app.py` drives the real desktop widgets and runs only where a display is available (it is skipped in headless CI); everything else is headless.

CI (`.github/workflows/ci.yml`) runs the same three checks (via `python -m pytest`, `python -m compileall .`, and `python -m ruff check .`) on every push and pull request.

### Benchmarks

`benchmarks/` holds reproducible, standard-library-only performance scripts and their recorded results/method docs — none of them run as part of `pytest`/CI:

```bash
python -m benchmarks.optimizer_baseline            # Greedy Optimizer v1 (see BASELINE.md)
python -m benchmarks.day_engine_baseline           # canonical engine, both modes (see DAY_ENGINE_BASELINE.md)
python -m benchmarks.allocation_baseline           # week/month allocation (see ALLOCATION_BASELINE.md)
python -m benchmarks.final_comparison              # legacy vs. canonical under matched config (see FINAL_COMPARISON.md)
python -m benchmarks.event_candidate_comparison    # event search vs. its own exhaustive reference (see EVENT_CANDIDATE_COMPARISON.md)
```

---

## Future Improvements

Possible next steps:

- Add more detailed unscheduled-task reasons.
- Add a true simulated annealing optimizer as an alternative to the greedy optimizer.
- Add schedule comparison metrics.
- Add export options for weekly and monthly schedules.
- Add better warnings for ignored missing dependencies.
- Add drag-and-drop task editing in the UI.
- Feed productivity-derived duration predictions back into the optimizer as an opt-in input (currently the suggestion is shown but never applied automatically).
- Add a real calendar-date mapping for the abstract day-index schedule model in the *legacy* pipeline (largely superseded by the canonical layer's real `datetime.date`, but the legacy `app/models.py` path itself still uses abstract day indexes).
- If real usage history grows enough to clear the ML activation gate, surface the comparison result (and, once it wins honestly, the ML suggestion itself) in the desktop UI's duration-suggestion widget alongside the median predictor.
- Desktop controls for the day-engine mode and per-date category preferences, and a dedicated Week/Month allocation view (the pages currently allocate and generate in one step).
- Persist allocation/staleness state so a restart can tell current schedules from out-of-date ones, instead of conservatively labelling every restored schedule out of date.
- Implement recurrence-template expansion and a real project-management service on top of the existing `RecurrenceSpec`/`Project` models.

---

## Project Status

The project has these parts:

- **Scheduling:** the original Milestone 0 greedy engine (the frozen baseline), and a canonical, UUID-identity-based day engine with `precise_greedy`/`adhd_friendly` modes and week/month task-to-date allocation.
- **Persistence (Milestone 2):** a local SQLite store for all planning data and execution history, behind a transactional service boundary.
- **Desktop app:** reads and writes that store directly, with real dates, id-based selection, CSV import/export, scoped resets, and restart-safe execution tracking.
- **CLI:** the same persistence-backed pipeline.
- **Also:** reward-based optimization, PERT-style dependency handling (name- and id-based), personal productivity analytics, and an evidence-gated experimental ML duration predictor.

Everything is covered by an automated test suite (`python -m pytest`). The next major improvements are desktop controls for the engine mode and per-date preferences, a separate allocation view, recurrence expansion, and a real project-management service.
