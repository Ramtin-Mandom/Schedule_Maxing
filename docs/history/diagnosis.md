# Schedule Maxing diagnosis

Repository audit on 2026-11-23 at `c030585`, before the follow-up cleanup. The current
implementation and verification are in [Current State](../CURRENT_STATE.md).

## Where the application stands

Schedule Maxing is a working offline-first Windows planner. The desktop supports Day, Week and Month views,
flexible tasks, fixed blocks, To Dos, projects, task outcomes and points, productivity reports, and a budgeting
workspace. SQLite is the normal desktop store. Optional account sync goes through a FastAPI/PostgreSQL backend.
The repository also has a CLI, a loopback API profile, and a direct PostgreSQL mode for private development. The
Windows desktop and its API are the shipped product surfaces.
[Architecture and entry points](../ARCHITECTURE.md#8-application-entry-points)

The main branch has one deterministic day engine with five modes. Range allocation assigns dates and places
required tasks first, then uses points to rank tasks within each tier. The minute-level engine enforces hard
constraints. The engine remake removed priority, task dependencies, Greedy v1, and the ML duration predictor.
Recurrence and deadlines remain in the domain and APIs, with limited desktop editing.
[Workflow](../Workflow.md), [desktop task form](../features/desktop-task-form.md)

The best-supported path is local planning, marking outcomes, and reviewing points. Account sync, PostgreSQL, and
the desktop-only install passed GitHub CI on the two most recent PRs. The next installer and the database
migration need coordinated release verification.
[Engine-remake CI](https://github.com/Ramtin-Mandom/Schedule_Maxing/actions/runs/40501046182),
[cleanup CI](https://github.com/Ramtin-Mandom/Schedule_Maxing/actions/runs/41959633756)

## Git and release state

| Check | Result at the start of the audit |
| --- | --- |
| Current checkout | `main` at `c030585`, equal to the local `origin/main` and GitHub `main` (0 ahead, 0 behind) |
| Index and working tree | No staged, unstaged, or untracked changes reported by Git |
| Local branches and worktrees | All 24 local branch tips were ancestors of `main`, including `main` itself. There were no commits exclusive to local branches, no stashes, and one worktree |
| Remote branches and PRs | GitHub returned 25 branches. All tips matched the local remote-tracking refs and were reachable from `main`. No open PRs |
| Latest published release | `v1.3.0` at `dfd53d6`. The installer predates 16 commits now on `main`. Both revisions report `1.3.0` in `app/version.py` |

Git references checked with status, staged and unstaged diffs, the branch graph, worktree and stash lists, tag
ancestry, and the GitHub branches, pulls and releases APIs.
[GitHub main](https://github.com/Ramtin-Mandom/Schedule_Maxing/commit/c030585),
[published v1.3.0](https://github.com/Ramtin-Mandom/Schedule_Maxing/releases/tag/v1.3.0)

| Revision | What landed |
| --- | --- |
| `664de0e` and `9e37656` | Budgeting, followed by desktop responsiveness, caching, and engine-process work. Included in `v1.3.0` |
| `be226f4` (PR #24) | Points-first allocation, engine and ML removal, wire compatibility, schema changes, and regenerated baselines |
| `c030585` (PR #25) | Documentation and requirements reorganization, removal of three unreachable widgets, CI fixes, and backend deployment on tags |

## Major problems

### 1. The next release needs a coordinated database migration

**High.** Published 1.3.0 has SQLite v18 and Alembic `0020` . Main has SQLite v19 and Alembic `0021` . Opening a
desktop database with main migrates it, after which the 1.3.0 binary refuses that file. Automatic local backups
cover the migration. Server revision `0021` drops priority columns and dependency rows that the old server
queries. An isolated upgrade reproduced the failure of a formerly valid `SELECT priority FROM tasks` after
`0021` . The downgrade recreates empty dependency tables and default priorities. A pre-upgrade backup is
required to recover the original values. The HTTP wire supplies placeholder fields to older desktop clients.
Direct PostgreSQL mode requires the exact schema head. [Local migration](../../app/execution/db.py#L1363),
[server migration](../../backend/migrations/versions/0021_drop_priority_and_dependencies.py),
[old server models](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/v1.3.0/backend/models.py#L230),
[sync compatibility](../architecture/sync-protocol.md#removed-task-fields-legacy-wire-placeholders)

The Windows release and GitHub backend deployment start independently from the same tag. The installer can
publish while backend deployment is disabled or failing. Render branch Auto-Deploy is a separate path when
enabled. The last recorded GitHub backend deploy failed under the old merge trigger. The tag-triggered workflow
has no recorded run as of the audit. Public `/health` and `/ready` timed out on the first probe and returned
HTTP 200 on retry. That confirms the responding service considers its database current for its own code. The
response omits the deployed commit and revision. [Release workflow](../../.github/workflows/release.yml),
[backend workflow](../../.github/workflows/deploy-backend.yml),
[last deployment run](https://github.com/Ramtin-Mandom/Schedule_Maxing/actions/runs/40625746637),
[public readiness](https://schedule-maxing.onrender.com/ready)

Before publishing the next installer, identify the hosted revision, verify a restore on disposable data,
coordinate the `0021` cutover so the old server is out of traffic, and verify both an upgraded 1.3.0 desktop and
a new installation. Bump the version for the next release. The updater offers only strictly newer versions.
[Release and migration runbook](../deployment/ci-cd.md#migrations),
[update service](../../app/update/service.py#L119)

### 2. The supported Python runtime is past end of life

**High.** CI, the release build, and the documented setup use Python 3.10. Upstream support ended on 2026-10-01.
CI currently carries a documented `dnspython` audit exception whose fix requires Python 3.11 or later. The
affected email DNS deliverability check defaults to off. Builds also resolve most desktop and development
requirements from lower bounds, while the security job reads a separate lock file. This makes dependency drift
possible between CI, the audit, and the installer. The previous cleanup already had to address a failure after
CI resolved a new pytest version. [PEP 619](https://peps.python.org/pep-0619/),
[audit exception](../backend/security-operations.md#dependencies),
[CI requirements](../../.github/workflows/ci.yml), [desktop requirements](../../requirements/desktop.txt)

### 3. The desktop outcome flow cannot supply timed productivity evidence

**High product impact.** The active Day, calendar and project screens record task outcomes and points. The old
start/pause/resume presentation is disconnected. The execution service and HTTP API can still record sessions
and feedback. Completing a task through the board without sessions records the completion time and leaves actual
duration unknown. Duration-accuracy and other timed metrics therefore rely on existing or API-generated
sessions. The desktop task editor preserves existing recurrence and deadline values. Creating a recurrence,
setting a deadline, or moving an existing placement requires a service or API path.
[Day outcome controller](../../app/ui/day_outcomes.py), [execution completion](../../app/execution/service.py#L347),
[tracker timing samples](../../app/productivity/tracker.py#L537),
[desktop task editor](../features/desktop-task-form.md#limitations),
[placement moves](../features/execution-rescheduling.md#3-the-explicit-reschedule)

### 4. Range allocation can miss work the day engine could place elsewhere

**Medium.** The allocator checks aggregate free minutes and ranks candidate dates. It does not check that a long
task fits in one contiguous gap. Generation tries the assigned date and does not retry that task on a different
date. In an in-memory run, a nine-hour task preferred Monday. A fixed 08:00–16:00 block split Monday's 16 free
hours into two eight-hour gaps. Monday won allocation, the engine returned `no_valid_slot` , and Tuesday
remained empty. With Tuesday as the preference, the same task placed successfully. The hard constraint held. The
lost placement is the gap between date allocation and the minute-level engine.
[Date allocation](../../app/planning/allocation.py#L143), [generation loop](../../app/planning/workflow.py#L650)

There are related product limits. The desktop exposes Make Schedule for one date at a time, reasons for unplaced
work disappear after reload, and generation rejects overnight windows and windows crossing daylight-saving
offset changes. [Day page](../features/desktop-day.md#limitations), [local time rules](../../app/planning/time.py)

### 5. Hosted retention and release trust need explicit support

**Medium.** Users can reset task data, but there is no account-deletion route. Server maintenance expires
security and delivery records while sync tombstones, revisions, and change history accumulate. Compaction needs
a recovery path for clients with old cursors. Another operational gap is installer verification. The updater
validates a SHA-256 checksum from the GitHub release, while `verify_signature()` is a no-op. Packaging can sign
if a certificate is configured. Publisher verification remains missing at install hand-off.
[Retention](../../backend/maintenance.py), [sync change feed](../../backend/api.py#L680),
[signature hook](../../app/update/downloader.py#L90),
[signing runbook](../deployment/windows-distribution.md#code-signing)

### 6. Parallel paths raise the cost of each change

**Medium.** A persisted task field spans the local domain and SQLite, hosted ORM and migrations, snapshots, sync
mapping, direct mode, and CSV. Some code also duplicates rules, including placement checks in the generation
workflow and inside the engine. The largest modules are `app/planning/application.py` (2,322 lines),
`app/execution/db.py` (1,836), and `backend/models.py` (1,709). Unit tests can pass while a different adapter or
a Windows-specific path remains unverified.
[Change impact map](../FEATURE_MAP.md#d-if-you-need-to-change-x-look-here),
[coupling](../ARCHITECTURE.md#92-coupling-and-fragility-potential-concerns-not-defects)

The CLI demo also rewrites checked-in sample exports by default. It uses an in-memory database, but it can dirty
a clean checkout. This audit restored the generated sample files after exercising the demo.
[CLI export paths](../../app/main.py#L397)

## Cleanup candidates

The initial scan parsed tracked Python imports under `app/` , `backend/` , and `config/` . Files without inbound
imports were executable entry points or tools. Following controller methods to the mounted pages, CLI and API
callers, packaging entry points, and tests found the following remnants.

| Candidate | Evidence and disposition |
| --- | --- |
| `app/ui/history_model.py` and old History/Data methods in `app/ui/productivity_controller.py` | The desktop mounts General, Specific and Project. `history()`, `storage_copy()`, `export_execution_history()` and `reset_all_history()` have test callers and no active page caller. The old view model and UI copy can go with the retired controller methods. Keep the scoped reset workflow and domain export support where consumed. [History model](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/history_model.py), [controller](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/productivity_controller.py), [mounted sections](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/productivity_page.py#L211) |
| `app/ui/execution_workflow.py` and the former Execute-tab adapters | `ExecutionController.describe()` is the production import path into `describe_item()`, but the active board calls `set_outcome`/`set_outcomes`. `describe()` and the old `perform()` wrapper are exercised by tests. The presentation layer can be retired after preserving lifecycle coverage through the service or API. [Old presentation](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/execution_workflow.py), [controller paths](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/execution_controller.py#L71), [board actions](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/task_status.py) |
| `app/productivity/prediction.py` and its duration-suggestion wrappers | The median predictor is implemented and tested, but the only production chain found ends at a controller method. No task editor, API or CLI calls it. Treat it as a candidate for removal or a deliberate future product feature. The existing tracker remains live. [Prediction](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/productivity/prediction.py), [service wrapper](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/productivity/reporting.py#L272), [UI wrapper](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/productivity_controller.py#L163) |
| `SchedulePageController.make_schedule()` and its `ScheduleRun` wrapper | The former Week/Month button is gone. The mounted calendar uses `CalendarController`, and Day overrides this method. Tests use the old range wrapper. Keep shared range generation, which the planning API still exercises. The CLI can preview a range and generate a selected date. [Old wrapper](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/schedule_page_controller.py#L690), [shell wiring](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/app.py#L782), [planning API](../../backend/planning_api.py#L702) |
| `mark_stale_if_outdated()` and `week_states()` in `app/planning/service.py` | Their callers are tests. Production derives freshness from persisted provenance. Keep the selected-day engine adapter, which is called by the live generation workflow. [Legacy state helpers](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/planning/service.py#L69), [live freshness](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/ui/planning_controller.py#L894) |
| `samples/inputs/dependency_chain_linear.csv` | Follow-up verified that the day-engine and event-candidate benchmarks both load this input. It remains a benchmark fixture. [Day-engine benchmark](../../benchmarks/day_engine_baseline.py), [candidate comparison](../../benchmarks/event_candidate_comparison.py) |
| `weight_category_bonus` | The setting round-trips through persisted preferences and sync, with a test proving it has no scoring effect. Keep compatibility on read while deciding how to retire the field or remove it from active configuration. [Reward test](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/tests/test_reward.py#L250), [preference adapter](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/planning/preferences.py#L566) |

The cleanup PR already removed `feedback_dialog.py` , `cohort_view.py` , and `duration_suggestion.py` . The
cohort calculation still feeds Productivity's tracker and the authenticated API report. The execution lifecycle,
compatibility shims for older clients, both desktop entry points, operator tools, benchmarks, and migration
files have current consumers or explicit operational uses.
[Cleanup PR](https://github.com/Ramtin-Mandom/Schedule_Maxing/pull/25),
[tracker's cohort call](https://github.com/Ramtin-Mandom/Schedule_Maxing/blob/c030585/app/productivity/tracker.py#L1330), [cohort endpoint](../../backend/planning_api.py#L974)

## Verification

| Check | Result |
| --- | --- |
| `python -m pytest -m dev -q` | 1,994 passed, 3 skipped, 398 deselected. Two upstream warnings from Starlette/AnyIO test-client imports |
| `ruff check --no-cache .` | Passed |
| `python -m benchmarks.scheduling_mode_baseline --check benchmarks/results/prompt1_scheduling_mode_baseline.json` | Identical to the checked-in baseline |
| CLI sample and isolated probes | Demo imported 3 tasks and 4 fixed blocks and generated a schedule. Reproduced the fragmented-day allocation case and the old SQL query failure on migrated schema `0021` |
| GitHub PR CI | Five jobs passed for each of PR #24 and #25, including PostgreSQL and desktop-only |
| GitHub release | `v1.3.0` built and published. That run skipped its PR CI gate. The post-1.3.0 merges have separate successful PR runs. [1.3.0 release run](https://github.com/Ramtin-Mandom/Schedule_Maxing/actions/runs/39890156928) |
| Public Render health | First attempt timed out. Retry returned HTTP 200 from both endpoints and reported current migrations for the live service |
| Documentation | Local file links and anchors resolved. Credential scan and whitespace checks passed |

The local suite ran on Windows with temporary Python 3.10.19 and the checkout's installed test packages. The
`.venv` launcher was unusable because its Python 3.10.11 installation was missing. The full local suite, a new
Windows installer build, authenticated hosted operations, and the live database revision were outside the
verified scope. Local database probes used isolated temporary data. This audit updates documentation and leaves
runtime fixes and cleanup for follow-up. [Current state](../CURRENT_STATE.md),
[roadmap](../ROADMAP.md)
