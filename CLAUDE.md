CLAUDE.md
Project Context
This is a scheduling/productivity application containing domain models, constraints, dependency logic, reward/scoring, multiple scheduling engines, persistence/sync, analytics, and a CustomTkinter desktop UI.
Implement only the active request. Preserve working behavior and existing architecture unless the task requires a change.
Efficient Working Method
Queued tasks
Process queued prompts strictly in order. Finish the implementation for one before editing for the next, but do not run the full verification suite between queued tasks.
If a task is genuinely blocked, explain the blocker instead of guessing.
Read as little as necessary
Avoid repository-wide exploration.
Start from the smallest relevant scope:
- files explicitly named by the task
- direct imports/call sites found through search
- the specific functions/classes being changed
- nearby tests only when their behavior matters
For large files, read/search the relevant sections rather than the whole file. Do not repeatedly reread unchanged files or inspect unrelated tests/configuration.
Only broaden the search when the current information is insufficient.
Architecture
Keep responsibilities separated:
- models/data
- hard constraints
- dependencies/PERT
- reward/scoring
- optimizer/engine strategies
- analytics
- UI
- persistence/sync/API
The UI should call domain/application logic rather than reimplement it. Hard constraints must remain hard constraints.
Preserve existing optimizer baselines and public behavior unless the active task explicitly changes them.
Change discipline
Make focused changes. Avoid unrelated refactors, broad formatting, unnecessary renames, speculative abstractions, duplicate systems, and unnecessary dependencies.
When changing a public API/model/config/storage format, inspect only its directly affected call sites and maintain compatibility when practical.
Testing and Verification
This repository has a large test suite. Optimize for implementation speed and context efficiency.
While building:
- do not run the full pytest suite after each task or edit
- do not run broad test directories by default
- rely on code inspection/static reasoning for straightforward changes
- run a small targeted test only when needed to resolve uncertainty or diagnose a failure
- add/update necessary tests without repeatedly executing them
- do not rerun checks that already passed unless later changes affect them
Testing Workflow
- Development suite: `pytest -m dev` (~1,600 fast tests, ~2 minutes). Full suite: `pytest` (~1,900 tests, all tiers).
- Default to focused tests through the dev filter while implementing (`pytest -m dev tests/path/test_x.py -k name`), so the real-window, system and slow tests in those files are skipped; never run the full suite after each step.
- Drop `-m dev` only when the change is in sync, web or direct mode (`tests/sync`, `tests/web` and `tests/direct` are all system tier, so the dev filter selects nothing there) or needs real-window coverage.
- Before considering an implementation batch complete, run `pytest -m dev` once (plus `ruff check .`).
- Reserve the full suite (`pytest`, `python -m compileall .`, `ruff check .`) for the end of a milestone, major
  cross-cutting changes (storage, sync, migrations, desktop UI framework), an explicit request, or final
  verification before merge/release -- once.
- If a test fails, rerun only the affected tests while debugging; run the broader suite once after fixing.
- Tiers are assigned automatically (tests/test_tiers.py): ui (real windows), system (sync/web/direct), slow,
  integration, unit; mark a new slow test `@pytest.mark.slow`.
Never weaken/delete meaningful tests just to make the suite pass.
If the prompt explicitly requests a different test strategy, follow the prompt.
Dependencies
Use existing packages or the standard library when practical. Add dependencies only when clearly justified and update dependency files accordingly.
Final Report
After the whole batch is complete, give one concise summary containing:
- major changes
- important files changed
- final tests/checks run and whether they passed
- important remaining limitations
Avoid verbose per-task summaries unless requested.
Project Knowledge Base (documentation navigation)
Six root documents describe the repository. Load only the document -- and only the section -- the current task needs; never read all six by default.
- CODEBASE_MAP.md: where a feature lives, what a change affects, which tests to run. Start with its "If You Need to Change X, Look Here" table.
- ARCHITECTURE.md: system design, boundaries, module dependencies, file reference and feature-to-file map.
- WORKFLOW.md: run-time behaviour -- the Make Schedule call chain, engine internals, rules/constraints, decision order and error handling.
- CURRENT_STATE.md: what currently works, is partial or broken (a dated snapshot).
- ROADMAP.md: planned and unfinished work, when planning development.
- DECISIONS.md: established architectural decisions; read the relevant record before changing one.
Keep them accurate in the same change that makes them wrong:
- module responsibilities, important paths or dependencies changed -> update CODEBASE_MAP.md
- architectural boundaries or major data flows changed -> update ARCHITECTURE.md
- execution flow, engine behaviour or an enforced rule/constraint changed -> update WORKFLOW.md
- a feature's state or a known limitation changed -> update CURRENT_STATE.md
- a milestone or deliverable status changed -> update ROADMAP.md
- a consequential decision made or superseded -> add or amend a record in DECISIONS.md
Do not rewrite unchanged documentation just to refresh dates or commit hashes.
At the end of a session the user can run /wrap (.claude/skills/wrap/SKILL.md), which detects changes since the last wrap and applies these updates incrementally.
CI check notes
ci_check_notes.md (repository root) is what /ci-check (.claude/skills/ci-check/SKILL.md) reads first, so it can fix CI in one pass. After every major structural change, add an entry under its "Pending" heading in the same change, using the file's template. Its "What counts as a structural change" list defines major: renamed/moved/removed or re-signatured public code, changed defaults/settings/storage fields/UI text that tests look up, moved paths, new background threads/processes/timers, new real-window behaviour, dependency or workflow changes. Do not read it for unrelated tasks.