---
name: schedule-maxing
description: Where things live in Schedule Maxing and which layer a change belongs to. Use before implementing a feature or fix that touches the scheduling engines, constraints/PERT/reward, planning, execution tracking, productivity/ML, sync, the FastAPI backend, the CustomTkinter UI, or Windows packaging -- to find the right module without exploring the repository.
---

# Schedule Maxing: project map

Python 3.10 (CI and `.venv`). A CustomTkinter desktop app (Windows installer, also Android), a FastAPI backend on
Render with PostgreSQL, and a local SQLite store. CLAUDE.md's working rules still apply: smallest scope first, no
repository-wide exploration, no unrelated refactors.

## Layers and where they live

| Layer | Modules | Rule |
|---|---|---|
| Models / data | `app/models.py`, `app/planning/models.py`, `app/execution/models.py` | |
| Hard constraints | `app/constraints.py`, `app/planning/fixed_block_rules.py` | A hard constraint decides *whether* a placement is allowed. Never turn one into a score penalty |
| Dependencies / PERT | `app/pert.py`, `app/planning/external_dependencies.py` | |
| Reward / scoring | `app/reward.py`, `app/mode_objectives.py` | Decides *how good* a valid placement is |
| Engines | `app/optimizer.py` (Greedy v1 legacy baseline + the minute-precise day engine), modes in `docs/scheduling-modes.md` | Normal, ADHD friendly, Early Finish, Night Owl, Catch-Up. Do not change baseline output unless the task says so |
| Planning service | `app/planning/` (`service`, `application`, `allocation`, `recurrence`, `series`, `csv_import`/`csv_export`) | The persistence-backed CSV importer rejects unknown dependencies; the legacy pipeline ignores them |
| Execution tracking | `app/execution/` (`service`, `lifecycle`, `db`, `backup`, `instance_lock`) | `db.py` owns local SQLite migrations; `backup.py` copies the file before any migration |
| Productivity / ML | `app/productivity/` | |
| UI | `app/ui/` -- `*_page`/`*_view` draw, `*_controller`/`*_model` hold logic | UI calls domain/application services; it never reimplements scheduling or persistence |
| Sync / API client | `app/sync/` (`engine`, `transport`, `credentials`), `app/persistence/` | |
| Backend (server only) | `backend/` (`api`, `accounts`, `auth*`, `sync`, `migrate`, `mail`, `rate_limit`) | Never imported by the packaged client |
| Updater | `app/update/` (`release_feed`, `downloader`, `versioning`) | |
| Runtime / paths | `app/runtime.py`, `config/settings.py` | The only place that knows source vs packaged. Ask it; never test `sys.frozen` elsewhere |
| Entry points | `app/desktop.py` (production), `app/app.py` (dev, `--storage postgres`), `app/main.py` (CLI), `app/web` | |
| Packaging | `packaging/windows/` (`build_windows.ps1`, `ScheduleMaxing.spec`, `.iss`, `check_bundle.py`, `smoke_test.py`) | |

Docs worth reading only when the task is in their area: `docs/scheduling-modes.md`, `docs/sync-protocol.md`,
`docs/desktop-web-boundaries.md`, `docs/windows-distribution.md`, `docs/ci-cd.md`, `docs/testing.md`.

## Boundaries that tests enforce

- **Desktop without the server.** The desktop must import and run with only `requirements-desktop.txt`: no FastAPI,
  SQLAlchemy, psycopg, Alembic, PyJWT, argon2, httpx. A top-level import of one of those in `app/` (outside the
  direct/web modules) breaks the `desktop-only` CI job and the packaged build. Import optional packages lazily.
- **Direct PostgreSQL mode** has its own minimal set (`requirements-direct.txt`): no HTTP or JWT packages.
- **Nothing private in the bundle:** no `.env`, server code, DB driver, key or log (`check_bundle.py`).
- **Bundled resources are read-only:** read them through `runtime.resource_path("config", "task_preference.yaml")`;
  write only under `runtime.data_dir()` (`%LOCALAPPDATA%\ScheduleMaxing`).
- **Version** is written once, in `app/version.py`; the release tag must equal `v` + that version.

## Change checklist

1. Which layer owns this? Put the logic there; let the UI/controller call it.
2. Changing a storage format, sync record or public model? Inspect only its direct call sites and keep old data
   readable (add a migration in `app/execution/db.py` or `backend/migrate.py` rather than rewriting data).
3. New dependency? Add it to the right requirements file (`-desktop`, `-backend`, `-direct`, `-build`) and
   `requirements.lock`; a desktop dependency must also be bundled by the PyInstaller spec.
4. Tests: focused while working, `pytest -m dev` + `ruff check .` once at the end. For failures, see the
   `schedule-maxing-debugging` skill.
