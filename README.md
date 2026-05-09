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

---

## Current Project Structure

```text
.
├── .venv/
├── app/
│   ├── __pycache__/
│   ├── app.py
│   ├── constraints.py
│   ├── data_processor.py
│   ├── main.py
│   ├── models.py
│   ├── optimizer.py
│   ├── pert.py
│   └── reward.py
├── config/
│   ├── __pycache__/
│   ├── settings.py
│   └── task_preferences.yaml
├── samples/
│   ├── inputs/
│   │   ├── day_sample.csv
│   │   ├── sample_month_schedule.csv
│   │   └── week_sample.csv
│   └── outputs/
│       └── day_sample.csv
├── tests/
├── .editorconfig
├── .env
├── .gitattributes
├── .gitignore
├── README.md
└── requirements.txt
```

> Note: The `tests/` folder exists, but test files have not been added yet. Adding unit tests for constraints, PERT dependency behavior, reward scoring, CSV loading, and optimizer outputs would be a strong next step.

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

The UI uses `customtkinter`, so that dependency must be installed before running the app.

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

This folder is reserved for future tests.

Recommended tests to add:

- Constraint tests for overlap and valid time windows.
- CSV loading tests.
- PERT dependency tests.
- Reward scoring tests.
- Optimizer tests for fixed blocks, preferred times, dependencies, and unscheduled tasks.

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

This opens the CustomTkinter schedule optimizer interface.

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

## Current Limitations

- The optimizer is greedy and does not guarantee the global best schedule.
- Once a task is placed, the optimizer does not move it again.
- The project has a `tests/` folder, but test files have not been added yet.
- Some settings related to simulated annealing are present but not fully used by the current optimizer.
- Error messages for unscheduled tasks are currently general and could become more specific in the future.

---

## Future Improvements

Possible next steps:

- Add unit tests for every major module.
- Add more detailed unscheduled-task reasons.
- Add a true simulated annealing optimizer as an alternative to the greedy optimizer.
- Add schedule comparison metrics.
- Add export options for weekly and monthly schedules.
- Add better warnings for ignored missing dependencies.
- Add drag-and-drop task editing in the UI.
- Add persistent saving/loading of UI-created schedules.

---

## Project Status

The project currently has a working scheduling pipeline, CSV input/output, reward-based optimization, PERT-style dependency handling, and a desktop UI. The next major improvement should be adding tests to make sure future changes do not break the optimizer, dependency logic, or CSV processing.
