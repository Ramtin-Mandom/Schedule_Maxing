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
- analytics/ML
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
After all requested implementations in the current batch are complete, run final verification once:
pytest
python -m compileall .
ruff check .
If final verification fails, isolate the affected tests, fix the issue with focused reruns, then run the full suite once more.
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