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
- run a very small targeted test or command only when it is necessary to diagnose uncertain behavior or a regression; run targeted tests through the dev filter (`pytest -m dev tests/path/test_x.py -k name`) unless the change is in sync, web or direct mode (those test folders are entirely system tier) or needs real-window coverage
- do not repeatedly rerun the same passing checks
- add/update tests when the task requires coverage, but they do not need to be executed immediately
Before considering an implementation batch complete, run the development suite once: `pytest -m dev`
(~1,600 fast tests; real-window, multi-system and slow tests are excluded -- see tests/test_tiers.py).
Run the full suite once (`pytest`, `python -m compileall .`, `ruff check .`) only at the end of a milestone,
after major cross-cutting changes, on explicit request, or for final verification before merge/release.
If a suite fails:
1. identify failures related to the changes
2. run only those focused tests while fixing them (with `-m dev` when they are in the development suite)
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
7. Project knowledge base (documentation navigation)
Five root documents describe the repository. Load only the document -- and only the section -- the current task needs; never read all five by default.
- CODEBASE_MAP.md: where a feature lives, what a change affects, which tests to run. Start with its "If You Need to Change X, Look Here" table.
- ARCHITECTURE.md: system design, boundaries and major data flows.
- CURRENT_STATE.md: what currently works, is partial or broken (a dated snapshot).
- ROADMAP.md: planned and unfinished work, when planning development.
- DECISIONS.md: established architectural decisions; read the relevant record before changing one.
Keep them accurate in the same change that makes them wrong:
- module responsibilities, important paths or dependencies changed -> update CODEBASE_MAP.md
- architectural boundaries or major data flows changed -> update ARCHITECTURE.md
- a feature's state or a known limitation changed -> update CURRENT_STATE.md
- a milestone or deliverable status changed -> update ROADMAP.md
- a consequential decision made or superseded -> add or amend a record in DECISIONS.md
Do not rewrite unchanged documentation just to refresh dates or commit hashes.
The end-of-session procedure for these updates is .claude/skills/wrap/SKILL.md (/wrap in Claude Code); other agents can follow the same steps and its helper script.