# Schedule Maxing

## Author note

This was a personal project that I started in the summer of 2026 and finished a minimal version within a week; that was only a prototype of what I wanted. After a month or so, I started working on this project again to finish it. My main goal in doing this project was learning the workflow of how to create an application both for desktop and Android. I worked on multiple web applications before and had heard that there are materials that you need to know to finish each project, and honestly, from personal experience, the desktop application was both more fun and harder. You didn't have to worry much about the specs of people running the application, as the programs are fairly simple, unlike web apps where, if you are not willing to pay for an expensive service, you have to be constantly worrying about optimization and removing features. Overall, the process felt more enjoyable for me. My second goal was to hone my skill of using AI agents to build projects. Since my previous projects were fairly small, I didn't have to worry about reaching the limit too much; however, in this project, I was hitting the limit multiple times a day on the $20 subscription. I used ChatGPT/Codex for planning and used Claude for coding and implementation. I made many mistakes in the beginning that were causing the limits to hit early. One important thing that I learned was to not let Claude keep running test suites, and if it wants to run them, use a smaller "development test suite" as they take much less time. Also, I got better with prompting, dividing my prompts into sections to keep my context token minimal, letting Claude know what files to work on or should look into. Overall, I finished this project a little bit messy, but that is okay, and there are some things I'm planning to try in my next project.

The whole idea of the app was some planning application, similar to a calendar, that has some features I always wanted to see in a planning application. First, I wanted the app to focus on the generating engine, but as time went on, I realized most people might not be interested in having their schedules given to them where tasks are not in the order that they were planning to do. If they want to have the generated schedule be exactly the way they want it, they have to spend a lot of time in the settings and yml files, which is not something I or any user wants to do. Therefore, I pivoted towards an app where it records tasks and gives points on performance, and its main usage, I would say, right now is a way for users to see how productive they are. There is still a schedule-generating engine with five modes for different ways of planning. This was not in my original schedule, but I also added an account system where users can log in and see their information that is stored on that account. The desktop application talks to the web API, and the web API talks to the database both for logging users in and getting their data. Based on my current Render plan, this database cannot support many people, so it was mostly for practice and my own curiosity rather than an actual application to sell. Overall, I feel happy with the final results of the project.

Ramtin Rezaei
## Introduction

Schedule Maxing is a planning and productivity application. Its main part is a native Windows desktop app
(Python and CustomTkinter) that keeps everything in a local SQLite database and works offline. You enter fixed
blocks, flexible tasks and To Dos, generate a day's schedule with a minute-precise engine, record what you
actually did, and see how productive you were. An optional FastAPI + PostgreSQL backend adds accounts and
synchronization between devices.

The engine combines **hard constraints** with a **reward function**: constraints decide whether a placement is
allowed at all, and the reward decides how good an allowed placement is. A greedy search then places the flexible
tasks and reports the ones that did not fit, with the reason.

---

## Features

**Planning**

- **Three task types**, entered with one form on Day, Week and Month:
  - *Fixed* blocks that cannot move (sleep, classes, work, meals);
  - *Flexible* tasks with a duration, category, tags, points and a preferred third of the day (Early, Mid or Late);
  - *To Dos*: checklist items that are never scheduled.
- **Day, Week and Month** calendars in the computer's time zone, with a per-date **Day Window** (the usable part of the day).
- **Make Schedule** fills a day with the canonical engine in one of five **scheduling modes**: Normal, ADHD friendly,
  Early Finish, Night Owl and Catch-Up. Work already saved that still fits is kept.
- **Week and month allocation** gives tasks a date. Required tasks claim capacity first, and tasks with
  more points go first within each tier.
- **Projects** with milestones and task defaults, plus **default and per-date preferences**. Planning supports
  recurring series and deadlines. The desktop form preserves existing values. [Task form limits](docs/features/desktop-task-form.md#limitations)

**Tracking and insights**

- Mark scheduled work completed, skipped or reopened on the Day board. Complete fixed blocks and To Dos for
  their points. The API supports timed sessions and execution feedback.
- Each day's Completed / Possible points and a **Productivity** page with completion rates and per-task and
  time-of-day reports. Duration accuracy uses recorded timed sessions.
- A **Budgeting** page for income, expenses and planned future items.

**Data and accounts**

- Everything is saved in SQLite as soon as it changes, and restored when you reopen the app.
- CSV import (append, or replace the dates a file covers, previewed first), plus CSV and exact JSON export.
- Scoped resets and automatic backups before a database migration.
- **Optional accounts** with email verification, two-step sign-in, password recovery and synchronization.
  Conflicts are shown and decided explicitly. Recovery and verification email use the configured SMTP, Resend,
  Brevo or Mailjet provider. Scheduling works offline.
- An in-app **How to Use** guide, and an update check for the installed Windows app.

---

## Installation

Python **3.10** with Tk support (the version CI, the build and the installer use).

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
source .venv/bin/activate           # macOS / Linux
```

Dependencies live in [`requirements/`](requirements/), one file per use:

| Install | Command | For |
| --- | --- | --- |
| Development (everything) | `python -m pip install -r requirements.txt` | the desktop, the backend, tests and Ruff; `requirements.txt` includes `requirements/dev.txt` |
| Desktop only | `python -m pip install -r requirements/desktop.txt` | running the desktop app and the CLI, with no server package |
| Backend only | `python -m pip install -r requirements/backend.txt` | the API server (FastAPI, Uvicorn, SQLAlchemy, Alembic, psycopg) |
| Direct PostgreSQL mode | `python -m pip install -r requirements/direct.txt` | the desktop storing its records straight in PostgreSQL (private development only) |
| Windows build | `python -m pip install -r requirements/build.txt` | the desktop runtime plus PyInstaller |

`requirements/database.txt` holds the PostgreSQL packages that the backend and direct mode share.
`requirements/requirements.lock` pins every package for the security scan and for reproducible installs
(`-c requirements/requirements.lock`). The root `requirements-backend.txt` only includes
`requirements/backend.txt`; it is kept for an existing Render service whose build command names it.

End users do not need Python: they install `ScheduleMaxing-Setup-<version>.exe` from the
[GitHub releases](https://github.com/Ramtin-Mandom/Schedule_Maxing/releases), and the app updates itself from them.

---

## Running

| What | Command |
| --- | --- |
| Desktop app | `python -m app.app` |
| Desktop app the way the installed version starts (log file, error dialogs, update check) | `python -m app.desktop` |
| Desktop with direct PostgreSQL storage | `python -m app.app --storage postgres --env-file .env` |
| Command-line scheduler (prints a summary of your saved data) | `python -m app.main` |
| Try the sample without touching your data | `python -m app.main --demo` |
| Productivity report | `python -m app.productivity.report_cli` |
| Backend API (needs `DATABASE_URL` and `JWT_SECRET`, see `.env.example`) | `uvicorn backend.main:app` |
| Backend database migrations | `python -m backend.migrate --env-file .env check` (or `upgrade`) |

`python -m app.main --help` lists the CLI options: importing a CSV, scheduling a stored date, choosing a mode and
exporting. Times are wall-clock minutes in the computer's time zone unless `SCHEDULE_MAXING_TIMEZONE` is set.
The demo uses an in-memory database and writes its exports to `samples/outputs/` by default. Use
`--legacy-csv-out` and `--exact-json-out` to send the demo exports elsewhere.

Guides: [desktop features](docs/features/), [backend setup](docs/backend/backend-setup.md),
[direct PostgreSQL mode](docs/deployment/direct-postgres.md).

---

## Tests

```bash
python -m pytest -m dev       # the fast development suite
python -m pytest              # the complete suite: real windows, sync, web and direct-mode tests
python -m compileall .
ruff check .
```

Tests are sorted into tiers automatically (`tests/test_tiers.py`): `unit`, `integration`, `system`, `ui` and
`slow`; `-m dev` leaves out the slow, real-window and system tiers. Real-window tests need a display.
PostgreSQL tests are marked `postgres` and run only when `TEST_DATABASE_URL` names a disposable database
([`docker-compose.postgres-test.yml`](docker-compose.postgres-test.yml) starts one). Details:
[docs/development/testing.md](docs/development/testing.md).

---

## Repository structure

```text
.
├── app/                    desktop app, CLI and the shared domain
│   ├── planning/           models, time, allocation, recurrence, persistence services
│   ├── execution/          SQLite store, migrations, backups, execution tracking
│   ├── productivity/       analytics and reports
│   ├── budget/             budgeting
│   ├── sync/               offline-first synchronization client
│   ├── persistence/        optional direct PostgreSQL storage
│   ├── update/             update check and installer hand-off
│   ├── web/                optional local web profile
│   ├── ui/                 CustomTkinter pages and their Tk-free controllers
│   ├── optimizer.py        the canonical day engine (all five modes)
│   ├── reward.py           placement scoring
│   ├── app.py, desktop.py  desktop entry points
│   └── main.py             command-line entry point
├── backend/                FastAPI + PostgreSQL server; migrations/ holds the Alembic versions
├── config/                 settings.py and the reward template task_preference.yaml
├── requirements/           dependency files and the lock file
├── packaging/windows/      PyInstaller spec, Inno Setup script, build and installer checks
├── assets/                 application icon
├── samples/                example CSV inputs and outputs used by the CLI demo and tests
├── benchmarks/             performance scripts and recorded results (not run by CI)
├── tests/                  the test suite
├── docs/                   documentation (index: docs/README.md)
└── .github/                CI, release and backend-deployment workflows
```

## Documentation

The [documentation index](docs/README.md) lists every guide. The most used:

- Architecture: [overview](docs/ARCHITECTURE.md), [run-time workflow](docs/Workflow.md),
  [feature map](docs/FEATURE_MAP.md), [decisions](docs/DECISIONS.md)
- Features: [Day page](docs/features/desktop-day.md), [scheduling modes](docs/features/scheduling-modes.md),
  [recurrence](docs/features/recurrence.md), [accounts and sync](docs/features/desktop-accounts.md)
- Project state: [repository diagnosis](docs/history/diagnosis.md), [current state](docs/CURRENT_STATE.md),
  [roadmap](docs/ROADMAP.md)

---

## Deployment

- **Desktop app.** Set the version in `app/version.py`, merge to `main`, then push the tag `vX.Y.Z`. The Release
  workflow builds and tests the Windows installer and publishes a GitHub Release, which installed copies update to.
  See [Windows distribution](docs/deployment/windows-distribution.md).
- **Backend.** The API runs as a Render web service. The same release tag starts a separate Deploy backend
  workflow when `BACKEND_DEPLOY_ENABLED` is `true`. That workflow deploys the tagged commit and checks `/health`
  and `/ready`. Render Auto-Deploy can also deploy branch updates independently. Coordinate schema revision
  `0021` before coupling a desktop release to a backend cutover. [Render deployment](docs/deployment/render-deployment.md)

All three workflows are described in [CI/CD](docs/deployment/ci-cd.md).

---

## License

Schedule Maxing is released under the [MIT License](LICENSE).
