# Documentation

The [project README](../README.md) covers installation, running, tests and deployment in short. End users have
the same material, without the internals, in the app itself: **How to Use** under Settings
(`app/ui/guide_content.py`).

| Folder | What is in it |
| --- | --- |
| [architecture/](architecture/) | how the system is built, the run-time flow, design decisions and the data contracts |
| [features/](features/) | what each part of the desktop app and the scheduler does |
| [backend/](backend/) | the API server: setup, endpoints, accounts, email and security |
| [deployment/](deployment/) | Windows installer and releases, CI/CD, Render, direct PostgreSQL mode |
| [development/](development/) | testing, the current state of the project and the roadmap |
| [history/](history/) | completion reports, measurements and plans of past development batches |

## Architecture

- [Architecture overview](architecture/ARCHITECTURE.md): layers, boundaries, module dependencies, file reference
- [Run-time workflow](architecture/WORKFLOW.md): the Make Schedule call chain, engine internals, rules and constraints
- [Codebase map](architecture/CODEBASE_MAP.md): where a feature lives, what a change affects, which tests to run
- [Decisions](architecture/DECISIONS.md): architectural decision records
- [Desktop and web boundaries](architecture/desktop-web-boundaries.md)
- [Local record and synchronization contract](architecture/sync-contract.md), [synchronization protocol](architecture/sync-protocol.md)

## Features

- [Layout and navigation](features/desktop-layout.md)
- [Day scheduling, To Dos and points](features/desktop-day.md), [Week and Month calendars](features/desktop-calendar.md)
- [Task entry and editing: Flexible, Fixed and To Do](features/desktop-task-form.md)
- [Projects, milestones and task defaults](features/desktop-projects-allocation.md)
- [Settings and task-data reset](features/desktop-settings.md)
- [Accounts, saved sign-in and synchronization](features/desktop-accounts.md)
- [Five scheduling modes](features/scheduling-modes.md), [recurring series](features/recurrence.md)
- [Execution, rescheduling and manual placements](features/execution-rescheduling.md)
- [Productivity analytics and tracker metrics](features/analytics.md)

## Backend

- [Backend setup and native authentication](backend/backend-setup.md)
- [Backend API, schema and password recovery](backend/backend.md)
- [Web application API and browser sessions](backend/web-api.md)
- Security: [email verification and delivery](backend/email.md), [passwords, sessions and two-step sign-in](backend/account-security.md),
  [authorization and API security](backend/api-security.md), [operations, retention, backups and the staging checklist](backend/security-operations.md)

## Deployment

- [Windows installation, builds, releases and updates](deployment/windows-distribution.md)
- [CI/CD pipeline: CI, desktop releases and backend deployment](deployment/ci-cd.md)
- [Render backend deployment](deployment/render-deployment.md)
- [Private-development direct PostgreSQL mode](deployment/direct-postgres.md), [with a Render Postgres database](deployment/render-direct-desktop.md)

## Development

- [Testing commands and tiers](development/testing.md) (recorded timings are historical)
- [Current state](development/CURRENT_STATE.md): what works, what is partial, known bugs
- [Roadmap](development/ROADMAP.md): planned and unfinished work

## History

These preserve the scope, findings and checks of particular development batches. They are not current feature
inventories or instructions to implement pending work; later work may supersede them, so use the guides above for
current behavior.

- Completion reports: [Milestone 4](history/milestone-4-completion.md), [Milestone 5](history/milestone-5-completion.md),
  [Milestone 6](history/milestone-6-completion.md)
- [Windows distribution audit](history/windows-distribution-audit.md) (the design behind the installer and updater)
- [Productivity redesign plan](history/productivity-redesign-plan.md) (the tracker contracts A-E that code comments cite)
- Desktop responsiveness: [diagnosis](history/desktop-responsiveness-diagnosis.md), [results](history/desktop-responsiveness-results.md),
  [current-tree validation](history/desktop-responsiveness-current-validation.md), [round 2](history/desktop-responsiveness-round2.md),
  [round 3](history/desktop-performance-round3.md)

The Markdown reports in [`benchmarks/`](../benchmarks/) record measurements for their stated scenarios and
revisions, next to the scripts and results they describe; they are not performance guarantees for every workload.
