# Documentation

Installation, launch commands and distribution are covered in the [project README](../README.md).
The desktop includes **How to Use** under Settings.

## Knowledge base

| Guide | What it covers |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Components, boundaries, data ownership and important flows |
| [Feature map](FEATURE_MAP.md) | Where to find a feature, which files it affects, and which tests cover it |
| [Decisions](DECISIONS.md) | Architectural choices and their reasoning |
| [Current state](CURRENT_STATE.md) | Verified capabilities, limitations and known issues |
| [Roadmap](ROADMAP.md) | Next priorities, dependencies and milestones |
| [Workflow](Workflow.md) | How inputs become validated records, schedules, reports and other outputs |

| Folder | What is in it |
| --- | --- |
| [architecture/](architecture/) | Desktop, web and persistence boundaries, record contracts and the sync protocol |
| [features/](features/) | what each part of the desktop app and the scheduler does |
| [backend/](backend/) | the API server: setup, endpoints, accounts, email and security |
| [deployment/](deployment/) | Windows installer and releases, CI/CD, Render, direct PostgreSQL mode |
| [development/](development/) | Testing and CI maintenance notes |
| [history/](history/) | completion reports, measurements and plans of past development batches |

## Runtime contracts

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
- [Budget entries, balances, forecasts and future items](Workflow.md#budgeting-and-forecasting)

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
- [CI change and verification notes](development/ci_check_notes.md)

## History

These preserve the scope, findings and checks of particular development batches. They are not current feature
inventories or instructions to implement pending work; later work may supersede them, so use the guides above for
current behavior.

- Completion reports: [Milestone 4](history/milestone-4-completion.md), [Milestone 5](history/milestone-5-completion.md),
  [Milestone 6](history/milestone-6-completion.md)
- [Windows distribution audit](history/windows-distribution-audit.md) (the design behind the installer and updater)
- [Productivity redesign plan](history/productivity-redesign-plan.md) (the tracker contracts A-E that code comments cite)
- [Repository diagnosis](history/diagnosis.md) at `c030585`
- Desktop responsiveness results and investigation. [Diagnosis](history/desktop-responsiveness-diagnosis.md), [results](history/desktop-responsiveness-results.md),
  [current-tree validation](history/desktop-responsiveness-current-validation.md), [round 2](history/desktop-responsiveness-round2.md),
  [round 3](history/desktop-performance-round3.md)

Benchmark reports record measurements for their stated scenarios and revisions. Executable benchmarks and
their data remain under `benchmarks/`. [Benchmark reports](history/benchmarks/), [scripts and results](../benchmarks/)
