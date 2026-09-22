# CLAUDE.md

## Project Context

This is a Python scheduling/productivity application with:

* Pydantic models
* scheduling constraints
* dependency / PERT logic
* reward scoring
* a greedy optimizer
* CSV loading
* a CustomTkinter desktop UI
* automated tests

The current optimizer is the baseline:

**Greedy Optimizer v1**

Future features may include better optimization, ML prediction, persistence, cloud sync, Android, and AI feedback.

Do not implement future roadmap features unless the active task explicitly requests them.

---

## Queued Tasks — IMPORTANT

When multiple prompts are queued, complete them **strictly in order**.

For each task:

1. Read the full prompt.
2. Inspect the relevant repository files.
3. Implement the requested changes.
4. Add/update tests when needed.
5. Run verification.
6. Fix failures caused by your changes.
7. Confirm the task is complete.
8. Only then start the next queued task.

Do not partially implement several queued tasks at once.

Do not skip an unfinished task to work on a later one.

If a task is genuinely blocked, explain the blocker instead of silently moving on.

---

## Inspect Before Editing

Before changing code:

* inspect relevant files
* inspect imports/call sites
* inspect nearby tests
* check whether functionality already exists

Do not assume the repository structure from the prompt alone.

Prefer extending existing architecture instead of creating duplicate systems.

---

## Architecture

Keep these responsibilities separate:

* **models** — data structures
* **constraints** — whether a schedule is valid
* **PERT/dependencies** — graph ordering and cycles
* **reward** — how desirable a valid placement is
* **optimizer** — searches for good valid placements
* **UI** — calls the scheduling logic; does not reimplement it

Hard constraints must not be replaced with reward penalties.

Do not change Greedy Optimizer v1 behavior unless explicitly requested.

---

## Change Discipline

Make focused changes.

Avoid:

* unrelated refactors
* unnecessary file renaming
* large formatting changes
* speculative abstractions
* unnecessary dependencies
* implementing future features early

If you notice unrelated technical debt, mention it afterward instead of automatically fixing it.

---

## Tests

For scheduling changes, consider:

* overlaps
* fixed blocks
* day boundaries
* durations
* dependencies
* cycles
* reward calculations
* unscheduled tasks
* CSV loading
* final schedule validity

Prefer meaningful tests over increasing test count.

Do not weaken or delete useful tests just to make the suite pass.

---

## Verification

When available, use:

```bash
pytest
python -m compileall .
ruff check .
```

Run focused tests during development if useful.

Before completing a milestone, run the broader checks requested by the prompt.

Fix failures introduced by your changes.

---

## Dependencies

Do not add packages unless necessary.

Check existing dependencies and the standard library first.

If a dependency is added, update the appropriate dependency file.

---

## Future Optimizers / ML

When adding another optimizer:

* preserve the greedy baseline
* use the same constraints
* compare results using measurable metrics

When adding ML:

* establish a simple baseline first
* evaluate the model properly
* compare it against the baseline
* keep ML separate from hard scheduling correctness

---

## Completion

At the end of each task, briefly report:

* what changed
* important files changed
* tests/checks run
* whether they passed
* any important remaining limitation

If more queued tasks remain, continue only after the current task is complete and verified.

## Most Important Rule

**Finish each queued command completely before starting the next one.**
