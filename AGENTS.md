# AGENTS.md

## Project Purpose

This repository is a personal scheduling and productivity application centered around a constraint-aware schedule optimization engine.

The current core includes:

* task and schedule models
* fixed and flexible tasks
* hard scheduling constraints
* dependency / PERT handling
* reward-based task placement
* a greedy optimizer
* CSV input/output
* a desktop UI
* productivity-related features and tests

The project will gradually expand toward:

* stronger optimization algorithms
* machine-learning duration prediction
* productivity analytics
* persistent local storage
* account and cloud synchronization
* Android support
* AI-generated productivity feedback

Do not prematurely implement future roadmap items unless the current task explicitly asks for them.

---

# Working Rules

## 1. Execute queued tasks strictly sequentially

When multiple user instructions or queued tasks exist, process them in the order they were given.

For each task:

1. Read the full task.
2. Inspect the relevant repository files.
3. Complete the task fully.
4. Run the requested or appropriate verification.
5. Fix failures caused by the task.
6. Confirm internally that the task's acceptance criteria are satisfied.
7. Only then continue to the next queued task.

Do not begin Task N+1 while Task N is incomplete.

Do not partially implement several queued tasks in parallel.

Do not skip a task because a later task appears easier or more interesting.

If Task N reveals a problem that blocks Task N+1, finish or resolve that blocker before continuing.

If Task N genuinely cannot be completed because required information is unavailable, clearly identify the blocker and stop rather than silently moving to later tasks.

---

## 2. Treat each queued instruction as an atomic milestone

For queued prompts, behave as though the user said:

> Finish this task completely before touching the next task.

Before continuing to another queued task, verify:

* required files were changed
* required behavior exists
* requested tests were added
* relevant tests pass
* compile/lint checks requested by the task pass
* no known unfinished TODO from the current task remains

Do not consider a task complete merely because code was written.

A task is complete only when its requested behavior is implemented and verified.

---

## 3. Inspect before editing

Never assume repository layout from the prompt alone.

Before making changes:

* inspect the repository tree
* open the files directly relevant to the task
* inspect relevant imports and call sites
* inspect existing tests
* inspect configuration affecting the code
* check for existing implementations before adding new ones

Prefer extending existing architecture over creating duplicate systems.

---

# Project Architecture Rules

## 4. Keep scheduling layers separated

Preserve the conceptual separation between:

### Models

Data structures such as:

* Task
* FixedBlock
* TimeWindow
* DaySchedule
* ScheduledTask
* DayScheduleOutput

### Hard constraints

Rules determining whether a schedule is valid.

Examples:

* overlap prevention
* day boundaries
* task duration
* fixed blocks
* dependencies

Hard constraints should not be hidden inside reward calculations.

### Reward / scoring

Determines how good a valid schedule is.

Examples:

* task priority
* preferred time
* category weighting
* related tags
* fragmentation penalties

Reward logic should not silently override hard constraints.

### Optimizer

Searches for valid task placements and attempts to maximize the reward.

The current production baseline is the greedy optimizer unless the repository explicitly changes this later.

### Analytics / ML

Prediction and productivity analysis should remain separate from the low-level constraint engine.

ML may provide inputs such as predicted task duration, but scheduling correctness must not depend on opaque model behavior.

### UI

The UI should call application/domain logic rather than reimplement optimization rules.

---

## 5. Preserve Greedy Optimizer v1 behavior unless instructed otherwise

The greedy optimizer is an important baseline.

When working on testing, CI, persistence, UI, ML, or future optimizers:

* do not casually change greedy scheduling semantics
* do not replace greedy behavior without an explicit task
* add regression tests before changing important optimizer behavior
* keep comparison with the baseline possible

Future algorithms such as simulated annealing or constraint programming should normally be introduced alongside the greedy baseline rather than silently replacing it.

---

# Code Change Rules

## 6. Prefer focused changes

Avoid unrelated refactors.

Do not:

* rename many files unnecessarily
* reformat the whole repository
* change APIs unrelated to the task
* redesign modules merely for stylistic preference
* add dependencies without a clear reason
* implement roadmap features that were not requested

When a small change solves the task, prefer the small change.

---

## 7. Preserve backward compatibility when practical

Before changing public functions, models, imports, configuration keys, or file formats:

* inspect their call sites
* inspect tests
* determine whether compatibility is expected

Do not break working UI, CLI, CSV, or test flows unnecessarily.

---

## 8. Never hide bugs behind tests

Do not weaken assertions simply to make a test pass.

Do not:

* delete meaningful tests
* skip tests unnecessarily
* replace strict assertions with meaningless ones
* catch broad exceptions solely to hide failures
* disable lint rules solely to conceal real issues

When behavior is incorrect, fix the implementation or clearly explain the incompatibility.

---

# Testing and Verification

## 9. Test important scheduling behavior

When modifying the scheduling engine, consider tests for:

* fixed blocks
* overlapping fixed blocks
* task/task overlap
* day boundaries
* task duration
* dependency ordering
* multiple dependencies
* dependency cycles
* missing dependencies
* PERT ordering
* reward calculations
* preferred-time scoring
* category/tag relationships
* fragmentation
* unscheduled tasks
* CSV loading
* invalid input
* optimizer output invariants

Do not add redundant tests solely to increase test count.

---

## 10. Run verification before declaring a task complete

When available and relevant, run:

```bash
pytest
python -m compileall .
ruff check .
```

If the task affects only a narrow area, focused tests may be run first, but run the broader checks before completing a milestone when practical.

Fix failures caused by your changes.

Do not silently ignore failing checks.

---

# Dependencies

## 11. Be conservative with dependencies

Before adding a package:

1. Check whether the repository already has a suitable dependency.
2. Determine whether the standard library can reasonably solve the problem.
3. Add the dependency only when it materially improves the implementation.
4. Update dependency files if a new
