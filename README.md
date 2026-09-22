# Schedule Maxing

A Python schedule optimization project that builds daily, weekly, and monthly schedules from user-defined tasks. The app supports fixed tasks, flexible tasks, preferred time windows, task dependencies, reward-based scoring, CSV input/output, and a CustomTkinter desktop interface.

The main idea of this project is to combine **hard scheduling constraints** with a **reward function**. Hard constraints decide whether a task placement is allowed, while the reward function decides how good a valid placement is. The optimizer then searches for strong task placements and returns a final schedule with scheduled and unscheduled tasks.

---

## Project Overview

This project is designed around a simple scheduling problem:

> Given a list of fixed tasks and flexible tasks, place the flexible tasks into the available time slots while respecting constraints and maximizing the schedule score.

The optimizer currently uses a **greedy reward-based scheduling algorithm**. It places fixed tasks first, then repeatedly chooses the best currently valid placement for one flexible task at a time. For each flexible task, the optimizer scans possible start times in 30-minute increments, scores each valid placement, and keeps the highest-scoring option.

The project also includes a dependency system inspired by PERT-style precedence constraints. If one task depends on another task, the optimizer tries to place the dependent task only after the prerequisite task has finished. Missing dependency names are ignored so that the app remains usable even if the input contains a dependency that is not present in the current task list.

---

## Main Features

- Add fixed tasks that cannot be moved, such as sleep, classes, work, or meals.
- Add flexible tasks with duration, priority, category, tag, preferred time window, and dependencies.
- Optimize a day, week, or month schedule.
- Use a reward function to prioritize important tasks and preferred time windows.
- Support task dependencies using PERT-style graph logic.
- Ignore missing dependencies instead of crashing the program.
- Export optimized schedules to CSV.
- Upload CSV input schedules.
- Display schedules visually in a desktop UI.
- Track unscheduled tasks and show why they could not be placed.
- Record actual task execution locally (start/pause/resume/complete/skip) with optional focus/energy/interruption feedback.
- View personal productivity insights (completion rates, duration accuracy, best-supported times of day) with an evidence/sample-count label on every figure.
- Get a historical duration suggestion (with its evidence and reasoning) when entering a new task, without ever overwriting your own entry automatically.

---

## Current Project Structure

```text
.
├── .venv/
├── app/
│   ├── app.py
│   ├── constraints.py
│   ├── data_processor.py
│   ├── execution/            # local SQLite execution-tracking domain (models, db, repository, service)
│   ├── main.py
│   ├── models.py
│   ├── optimizer.py
│   ├── pert.py
│   ├── productivity/         # analytics built on execution history (stats, predictions, insights, reporting)
│   ├── reward.py
│   └── ui/                   # desktop-UI controllers and widgets that wire execution/productivity into app.py
├── config/
│   ├── settings.py
│   └── task_preferences.yaml
├── data/                     # local runtime SQLite database (gitignored; created on first run)
├── samples/
│   ├── inputs/
│   │   ├── day_sample.csv
│   │   ├── month_sample.csv
│   │   └── week_sample.csv
│   └── outputs/
│       └── day_sample.csv
├── tests/
│   ├── execution/
│   ├── productivity/
│   ├── ui/
│   └── test_main_time_formatting.py
├── .editorconfig
├── .env
├── .gitattributes
├── .gitignore
├── README.md
└── requirements.txt
```

> `tests/` contains an automated suite (`python -m pytest`) covering the optimizer's supporting modules, execution tracking, productivity analytics, and the UI-facing controllers.

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

The reward system reads values from `task_preferences.yaml` when available. If the YAML file is missing or incomplete, safe default values are used.

---

### `app/optimizer.py`

Contains the main greedy scheduling algorithm.

The optimizer works in this order:

1. Load reward settings.
2. Read the schedule's day start and day end.
3. Convert fixed blocks into already scheduled tasks.
4. Collect all flexible tasks.
5. Repeatedly consider every remaining flexible task.
6. For each task, find the best valid time slot.
7. Pick the task-placement pair with the highest reward score.
8. Lock that task into the schedule.
9. Repeat until all tasks are scheduled or no progress can be made.
10. Return scheduled tasks, unscheduled tasks, and total score.

This is a greedy algorithm, so it is designed to find good schedules quickly. It does not guarantee a mathematically optimal schedule because it does not try every possible full schedule and it does not move tasks after locking them in.

---

### `app/main.py`

Provides a command-line entry point for loading a sample CSV, running the optimizer, printing the schedule, and exporting the final result to CSV.

It currently loads input from:

```text
samples/inputs/day_sample.csv
```

and exports output to:

```text
samples/outputs/day_sample.csv
```

The exported CSV uses 30-minute blocks. If a task lasts 3 hours, it appears across 6 rows. If no task is active during a block, the output uses `-`.

---

### `app/app.py`

Contains the desktop UI for the schedule optimizer.

The UI supports:

- Day, week, and month schedule pages.
- Task input forms.
- Fixed/flexible task selection.
- CSV upload.
- Task removal.
- Schedule visualization.
- Unscheduled task display.
- Runtime reward configuration page.

The UI uses `customtkinter`, so that dependency must be installed before running the app. It also wires in the Execute tab and Productivity page (see below) via `app/ui/`, but contains none of their persistence or analytics logic itself.

---

### `app/execution/`

The local task-execution domain, used by both the desktop UI and the CLI report tool: `models.py` (execution/session/status models), `db.py` (SQLite connection + idempotent schema migrations), `repository.py` (the only module with raw, parameterized SQL), `service.py` (the state machine — start/pause/resume/complete/skip, duplicate prevention, active-duration and start-delay calculations), and `exporters.py` (CSV/JSON export of raw execution history). See [Task Execution Tracking & Productivity Insights](#task-execution-tracking--productivity-insights-local-only) for usage and storage location.

---

### `app/productivity/`

Turns execution history into statistics and predictions: `data_prep.py` (flattens executions into analysis-ready records), `stats.py`/`segments.py` (aggregate statistics and groupings, each carrying its own sample count and evidence label), `prediction.py` (the median duration estimator and its documented fallback hierarchy — the production predictor), `insights.py` (structured, template-generated observations), `trends.py` (recent-vs-baseline comparison), `reporting.py` (the `ProductivityService` boundary and report/dashboard bundles), `exporters.py`, and `report_cli.py` (`python -m app.productivity.report_cli`).

The evidence-gated ML duration predictor (see [Evidence-gated ML duration predictor](#evidence-gated-ml-duration-predictor-experimental-disabled-by-default)) lives in its own small, focused modules: `ml_features.py` (leakage-safe feature construction), `ml_split.py` (chronological train/test splitting), `ml_model.py` (the sklearn `Pipeline` definition and fitting), `ml_metrics.py` (MAE/median-AE/tolerance-rate calculations), `ml_evaluation.py` (the three-way comparison and activation gate), `ml_persistence.py` (saving/loading the model artifact, never raising), `ml_prediction.py` (the runtime, fail-safe prediction path), and `predictor_comparison_cli.py` (`python -m app.productivity.predictor_comparison_cli`).

---

### `app/ui/`

Desktop-UI-facing controllers and widgets that connect `app/app.py` to `app/execution/` and `app/productivity/`, keeping persistence and analytics logic out of UI callbacks: `execution_controller.py`/`productivity_controller.py` (the only things UI widgets call into), `background.py` (runs database calls off the UI thread), `execution_panel.py`/`feedback_dialog.py`/`duration_suggestion.py`/`productivity_page.py`/`productivity_charts.py` (the actual widgets).

---

### `config/settings.py`

Stores global project settings, such as:

- Time slot size.
- Default day start and end.
- Hard constraint toggles.
- Reward weight constants.
- Simulated annealing constants kept for future expansion.
- Neighbor generation settings kept for future expansion.

Some settings may be older or reserved for future optimizer versions. The current optimizer mainly uses the 30-minute slot structure and reward configuration loaded through `reward.py`.

---

### `config/task_preferences.yaml`

Stores user-adjustable reward preferences.

This file can define:

- Reward weights.
- Maximum preferred-time distance.
- Category weights.
- Exact task weights.
- Exact task preferred windows.
- Category preferred windows.
- Related tags.

This allows the behavior of the optimizer to be tuned without rewriting Python code.

---

### `samples/inputs/`

Contains sample CSV input files for testing the scheduler manually.

Current sample files include:

- `day_sample.csv`
- `week_sample.csv`
- `sample_month_schedule.csv`

---

### `samples/outputs/`

Contains generated schedule output files.

Currently, `day_sample.csv` is used as the exported result from `app/main.py`.

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

CI (`.github/workflows/ci.yml`) runs `pytest`, `python -m compileall`, and `ruff check .` on every push and pull request.

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

This loads the sample day CSV, optimizes the schedule, prints the result, and exports a CSV file to `samples/outputs/`.

---

### 4. Run the desktop UI

From the project root:

```bash
python -m app.app
```

This opens the CustomTkinter schedule optimizer interface. Once you run **Make Schedule**, an **Execute** tab appears next to Added Tasks/Unscheduled where you can Start/Pause/Resume/Complete/Skip each flexible task, and a **Productivity** item appears in the sidebar (see [Task Execution Tracking & Productivity Insights](#task-execution-tracking--productivity-insights-local-only) below). If the local execution database cannot be opened, the scheduler still runs normally and a warning explains that tracking is disabled for that session.

---

### 5. Run the productivity report from the command line (optional)

```bash
python -m app.productivity.report_cli
python -m app.productivity.report_cli --period week --format json --output report.json
```

Prints (or exports) the same productivity analysis the desktop Productivity page shows, from whatever local execution history exists. See `python -m app.productivity.report_cli --help` for all options.

---

## Output Format

The command-line exporter creates a CSV with two columns:

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

Execution history lives in a single local SQLite file at:

```text
<project root>/data/executions.db
```

Override the location with the `SCHEDULE_MAXING_DATA_DIR` environment variable if you want it elsewhere. The `data/` directory (and any `*.db`/`*.db-journal`/`*.db-wal`/`*.db-shm` file) is listed in `.gitignore`, so it is never committed. The database and its schema are created automatically the first time the app or the productivity CLI runs; nothing needs to be set up by hand.

If you run `python -m app.productivity.predictor_comparison_cli --save-model` and the ML activation gate passes, the trained model is saved alongside it as `<project root>/data/ml_duration_model.joblib` plus a `ml_duration_model.meta.json` sidecar — also under `data/`, so also never committed.

### Recording execution (desktop UI)

After running **Make Schedule** on the Day/Week/Month page, open the **Execute** tab (next to Added Tasks/Unscheduled) to:

1. Pick a scheduled flexible task from the dropdown (fixed blocks like sleep/meals are not tracked as executions).
2. Use whichever actions are enabled for its current state:
   - **Start** (scheduled → in progress)
   - **Pause** (in progress → paused)
   - **Resume** (paused → in progress)
   - **Complete** (in progress or paused → completed)
   - **Skip** (scheduled, in progress, or paused → skipped)
3. Watch the live **active time** counter — it only counts time while the task is in progress; time spent paused is excluded.
4. On Complete or Skip, optionally record a focus rating (1–5), energy rating (1–5), interruption count, and a short note. Every field is optional.

Re-selecting the same task after re-running **Make Schedule**, reloading a CSV, or reopening the app reuses its existing execution record instead of creating a duplicate, as long as the task's name, category, tag, and scheduled time are unchanged.

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
- **Reset local history...** permanently deletes all locally stored execution data. This is destructive, requires an explicit confirmation dialog, and is kept in its own section, separate from normal navigation.

### Privacy

- All execution and productivity data stays on your device, in the single SQLite file described above.
- Nothing here is sent to a server, cloud service, or third party.
- Application error messages and logs never include your notes or other personal feedback content — only the export you explicitly request does.

### Screenshots

This README does not embed screenshots of the desktop UI. To add your own: run `python -m app.app`, open the Execute tab and the Productivity page, and use your OS's screenshot tool (Windows: `Win+Shift+S`; macOS: `Cmd+Shift+4`), then reference the saved image(s) here with standard Markdown image syntax, e.g. `![Productivity page](docs/screenshots/productivity.png)`.

---

## Current Limitations

- The optimizer is greedy and does not guarantee the global best schedule.
- Once a task is placed, the optimizer does not move it again.
- Some settings related to simulated annealing are present but not fully used by the current optimizer.
- Error messages for unscheduled tasks are currently general and could become more specific in the future.
- Execution tracking identifies a task by its planned snapshot (name, category, tag, and scheduled time), not a separate stable ID: two genuinely distinct tasks that happen to share an identical snapshot will be treated as the same execution.
- Day-of-week and "recent" productivity statistics are anchored to when an execution *record* was created (a real timestamp), not a real calendar date for the *plan* itself, since this app's schedule uses abstract day numbers (Day 1, Day 2, ...) rather than calendar dates.
- The ML duration predictor is evidence-gated and, on a typical personal-scale history, is expected to stay disabled (the stock test fixture's 19 completed tasks are well below its default 40/25/10 sample thresholds) — this is by design, not a defect. It is not wired into the desktop UI; use `python -m app.productivity.predictor_comparison_cli` to evaluate and, if it qualifies, persist it.

---

## Future Improvements

Possible next steps:

- Add more detailed unscheduled-task reasons.
- Add a true simulated annealing optimizer as an alternative to the greedy optimizer.
- Add schedule comparison metrics.
- Add export options for weekly and monthly schedules.
- Add better warnings for ignored missing dependencies.
- Add drag-and-drop task editing in the UI.
- Add persistent saving/loading of UI-created schedules.
- Feed productivity-derived duration predictions back into the optimizer as an opt-in input (currently the suggestion is shown but never applied automatically).
- Add a real calendar-date mapping for the abstract day-index schedule model, which would make day-of-week productivity statistics exact rather than an approximation.
- If real usage history grows enough to clear the ML activation gate, surface the comparison result (and, once it wins honestly, the ML suggestion itself) in the desktop UI's duration-suggestion widget alongside the median predictor.

---

## Project Status

The project has a working scheduling pipeline, CSV input/output, reward-based optimization, PERT-style dependency handling, a desktop UI, local SQLite task-execution tracking, personal productivity analytics (completion rates, duration accuracy, evidence-labeled insights, and historical duration suggestions), and an evidence-gated experimental ML duration predictor evaluated against that same median predictor on real history — all covered by an automated test suite (`python -m pytest`). The next major improvement should be extending duration predictions into an opt-in optimizer input, and adding a real calendar-date model for more precise day-of-week analytics.
