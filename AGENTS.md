AGENTS.md
Project
This is a scheduling/productivity application with models, constraints, dependency handling, reward/scoring, optimization engines, persistence/sync, analytics, and desktop UI.
Only implement what the active task requests. Preserve existing behavior unless a change is required.
Core Rules
1. Process queued tasks in order
Complete queued tasks sequentially. Do not skip ahead or mix unrelated tasks.
For each task:
1. Understand the requested behavior and acceptance criteria.
2. Inspect only the code needed to make the change.
3. Implement the change completely.
4. Continue to the next queued task.
If genuinely blocked, report the blocker rather than inventing missing information.
2. Minimize context usage
Do not broadly read the repository before every task.
Use the smallest useful inspection scope:
- start with files named by the prompt or obvious entry points
- search for symbols/imports/call sites instead of opening many files
- read only relevant sections of large files
- inspect tests only when needed to understand expected behavior or when modifying that area
- do not repeatedly reread files already understood unless they changed materially
- do not inspect unrelated folders, generated files, migrations, fixtures, or test suites without a reason
Expand the inspection scope only when the current evidence is insufficient.
3. Keep architecture boundaries
Maintain separation between:
- models/data — domain structures and persistence models
- constraints — hard validity rules
- dependencies/PERT — ordering and dependency logic
- reward/scoring — desirability of valid choices
- optimizers/engines — search/placement strategies
- analytics/ML — prediction and historical analysis
- UI — presentation and orchestration; do not duplicate domain logic
- sync/API/storage — persistence and remote synchronization
Hard constraints must not be replaced by reward penalties.
Preserve existing optimizer engines and compatibility unless the task explicitly changes them.
4. Make focused changes
Prefer the smallest implementation that satisfies the task.
Avoid:
- unrelated refactors
- repository-wide formatting
- unnecessary renames
- speculative abstractions
- duplicate implementations
- unnecessary dependencies
- premature roadmap work
Before changing a public API, model, configuration key, or stored format, inspect the directly affected call sites and preserve compatibility when practical.
5. Testing policy — optimize for development speed
The repository has a large test suite. Do not run the full suite while working through queued implementation tasks.
During implementation:
- do not run pytest after every edit or every queued task
- do not run broad test directories by default
- use static reasoning and code inspection first
- run a very small targeted test or command only when it is necessary to diagnose uncertain behavior or a regression
- do not repeatedly rerun the same passing checks
- add/update tests when the task requires coverage, but they do not need to be executed immediately
Before considering an implementation batch complete, run the development suite once: `pytest -m dev`
(~1,600 fast tests; real-window, multi-system and slow tests are excluded -- see tests/test_tiers.py).
Run the full suite once (`pytest`, `python -m compileall .`, `ruff check .`) only at the end of a milestone,
after major cross-cutting changes, on explicit request, or for final verification before merge/release.
If a suite fails:
1. identify failures related to the changes
2. run only those focused tests while fixing them
3. rerun that suite once after fixes are complete
Do not weaken, delete, or skip meaningful tests merely to obtain a passing result.
If the user explicitly requests a different verification strategy, follow it.
6. Dependencies
Prefer existing dependencies and the standard library. Add a package only when it provides clear value, and update dependency files when doing so.
Completion
At the end of the entire requested batch, briefly report:
- what changed
- important files changed
- final verification performed and result
- meaningful remaining limitations
Do not produce lengthy per-task reports unless requested.