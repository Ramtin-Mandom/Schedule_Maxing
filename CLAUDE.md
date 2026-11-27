CLAUDE.md
Project Context
Schedule Maxing is an offline-first CustomTkinter desktop planner with a shared day engine, task outcomes, productivity reports, budgeting, and optional account sync.
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
Commits and attribution
The repository owner is the only author and contributor. Never add yourself (Claude, or any AI assistant) as an author, committer or contributor: no `Co-Authored-By` trailers, no "Generated with Claude" lines, and no AI footers in commit messages, PR bodies or tags. `.claude/settings.json` keeps `attribution` empty for the same reason.
Final Report
After the whole batch is complete, give one concise summary containing:
- major changes
- important files changed
- final tests/checks run and whether they passed
- important remaining limitations
Avoid verbose per-task summaries unless requested.
Project Knowledge Base (documentation navigation)

Before implementing new work, open the [documentation index](docs/README.md) and inspect the relevant sections of all six guides.

- Components, boundaries and major data flows. [Architecture](docs/ARCHITECTURE.md)
- Feature-to-code navigation, call sites and focused tests. Start with the change table. [Feature map](docs/FEATURE_MAP.md)
- Important design choices, reasoning and compatibility constraints. [Decisions](docs/DECISIONS.md)
- Verified capabilities, gaps and known issues. [Current state](docs/CURRENT_STATE.md)
- Open priorities, dependencies and completed milestones. [Roadmap](docs/ROADMAP.md)
- Inputs, processing, persisted results and outputs for product workflows. [Workflow](docs/Workflow.md)

Before closing the task, compare all six guides with the final diff and verification results. Update affected sections and linked guides in the same change. Record capabilities and checks in Current State, priorities and milestones in Roadmap, and design choices in Decisions. Update Workflow for changes to inputs, execution, rules or outputs. Update Architecture and Feature Map for changes to boundaries, entry points, paths or tests. Keep historical records and avoid date-only edits.

The `/wrap` procedure supports this review. [.claude/skills/wrap/SKILL.md](.claude/skills/wrap/SKILL.md)

CI check notes

Record major structural changes under Pending in the same change using the notes' template. `/ci-check` uses those entries when reviewing workflow results. [CI check notes](docs/development/ci_check_notes.md)
