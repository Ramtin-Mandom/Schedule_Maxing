# AGENTS.md

## Project purpose

Schedule Maxing is a local Python scheduling and optimization application. It
combines immovable fixed blocks with flexible tasks and attempts to maximize a
reward score while respecting hard scheduling constraints.

The project has two entry points:

- CLI: `python -m app.main`
- CustomTkinter desktop UI: `python -m app.app`

Both entry points must continue to use the same scheduling domain models and
optimizer behavior.

## Repository map

- `app/models.py`: Pydantic input and output models.
- `app/data_processor.py`: CSV parsing and conversion into domain models.
- `app/constraints.py`: reusable hard-constraint and free-slot helpers.
- `app/pert.py`: dependency graph, cycle detection, and dependency readiness.
- `app/reward.py`: candidate-placement scoring and YAML configuration loading.
- `app/optimizer.py`: greedy scheduling orchestration.
- `app/main.py`: CLI execution, display, and CSV export.
- `app/app.py`: CustomTkinter desktop interface.
- `config/task_preferences.yaml`: active reward configuration.
- `config/settings.py`: configuration constants; several are reserved or
  currently unused, so confirm call sites before relying on them.
- `samples/inputs/`: example schedule inputs.
- `samples/outputs/`: generated example outputs.
- `tests/`: automated tests. Add focused tests for behavior you change.

## Setup and standard commands

Use the repository's existing virtual environment when available. Otherwise:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
```

Run the CLI end to end:

```bash
python -m app.main
```

Run the desktop UI:

```bash
python -m app.app
```

Run tests:

```bash
python -m pytest
```

Check Python syntax and imports after broad changes:

```bash
python -m compileall app config
```

Do not claim a command passed unless it was actually run successfully.

## Core scheduling invariants

Preserve these behaviors unless the task explicitly changes them:

- Fixed blocks are immovable.
- Scheduled items must not overlap.
- Every placement must remain inside the configured day window.
- A flexible task must receive its required duration.
- A task with real dependencies must be placed after its scheduled
  prerequisites.
- Cyclic dependency graphs must be rejected or reported clearly.
- Tasks that cannot be placed must appear in the unscheduled result with a
  useful reason.
- `DayScheduleOutput` must keep scheduled tasks, unscheduled tasks, and the
  total score consistent with one another.
- CLI export and UI rendering must agree with optimizer output.
- Existing CSV fields and output formats are compatibility boundaries.

Do not silently change the current treatment of missing dependency names.
Discuss the desired behavior and add tests before changing it.

## Architecture rules

- Keep domain and scheduling logic outside the UI.
- Keep `app/main.py` as a thin CLI adapter rather than placing optimization
  logic in it.
- Prefer shared helpers over duplicating constraint or parsing logic.
- When changing placement validation, inspect both `app/constraints.py` and
  the inline checks in `app/optimizer.py` so their behavior does not drift.
- When changing dependency syntax, inspect `app/data_processor.py`,
  `app/pert.py`, `app/optimizer.py`, and the UI parser together.
- Do not use a plain hyphen as the only dependency delimiter because task names
  may themselves contain hyphens.
- Treat task names and user-entered labels as data, not parsing syntax.
- Maintain compatibility with both CLI and desktop UI consumers.
- Do not introduce network services, external APIs, databases, or telemetry
  unless the task explicitly requires them.

The implemented optimizer is currently greedy. Do not describe the application
as using simulated annealing merely because simulated-annealing constants exist
in `config/settings.py`.

## Configuration rules

- Trace a setting to an actual runtime call site before stating that it affects
  scheduling.
- Do not expose a configuration field in the UI as functional unless the
  optimizer or reward system actually consumes it.
- Preserve safe defaults when `config/task_preferences.yaml` is absent or
  incomplete.
- Validate YAML values before using them in scheduling calculations.
- Avoid adding additional fallback spellings or filenames; prefer one
  documented canonical configuration path.
- Ask before deleting currently unused public configuration fields because
  users may already have configuration files containing them.

## Time handling

- Keep internal time calculations separate from display formatting.
- Treat midnight and day-boundary values explicitly.
- Add boundary tests for `00:00`, noon, `23:59`, and an end time of `24:00`
  whenever time conversion code changes.
- In particular, do not regress the known `minutes_to_time(1440)` display case:
  an end-of-day value must not be shown as noon.

## Testing expectations

Every behavioral fix or feature should include focused automated tests.
Prioritize coverage for:

- fixed-block placement and overlap rejection;
- day-window and duration constraints;
- dependency ordering, missing dependencies, and cycle detection;
- reward scoring and YAML overrides;
- greedy optimizer progress and termination;
- scheduled versus unscheduled reporting;
- CSV parsing and export round trips;
- midnight and other time-display boundaries.

Tests must be deterministic. Do not depend on wall-clock time, display access,
network access, or mutable files outside a temporary test directory.

For UI changes, test extracted non-visual logic where possible. If the
environment is headless, run compilation and relevant unit tests and clearly
state that visual behavior was not manually verified.

## Change workflow

Before editing:

1. Read `README.md` and the files involved in the requested behavior.
2. Trace the complete input-to-output path instead of assuming a module is
   active because it exists.
3. Identify compatibility constraints and the smallest useful test.
4. For changes spanning multiple components, summarize the plan before coding.

While editing:

- Keep changes focused on the requested outcome.
- Preserve unrelated user changes and avoid broad mechanical rewrites.
- Follow existing model and naming conventions.
- Prefer simple, explicit Python over unnecessary abstractions.
- Do not mix unrelated cleanup with a bug fix or feature.
- Ask before adding a production dependency, changing a CSV schema, replacing
  the optimization strategy, or removing public configuration.

Before finishing:

1. Review the final diff for unintended changes.
2. Run the smallest relevant tests during development.
3. Run `python -m pytest` before completion when the environment permits.
4. Run `python -m app.main` when the end-to-end CLI path is affected.
5. Report the commands run, results, changed files, and any remaining risks.

## Security and repository hygiene

- Never commit credentials, secrets, `.env` contents, personal schedules, or
  private user data.
- Do not add generated schedules, caches, virtual environments, or temporary
  files to version control unless they are intentional fixtures.
- Do not modify, commit, push, rebase, or open a pull request unless requested.
- Do not delete dead code or dependencies solely because they appear unused;
  confirm scope and intent first.

## Known areas requiring care

- `tests/` currently has little or no coverage, so new work should improve the
  situation rather than rely only on manual execution.
- Time formatting at the `1440`-minute boundary has had a midnight/noon bug.
- Dependency parsing has used hyphens as delimiters and can conflict with
  hyphenated task names.
- Some simulated-annealing, neighbor-generation, enforcement, and reward
  settings are not connected to runtime behavior.
- `app/constraints.py` and `app/optimizer.py` contain overlapping placement
  logic that can diverge.
- Some documentation and UI labels may describe inactive or renamed behavior;
  verify claims against executable code.

Treat these as warnings to investigate, not permission to expand every task
into an unrelated cleanup project.
