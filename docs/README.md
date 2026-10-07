# Documentation

The [project README](../README.md) covers setup, CLI usage and the architecture.
These guides describe the current repository; deployment availability must be
checked separately by the operator.

## Desktop and installation

- [Windows installation, builds, releases and updates](windows-distribution.md)
- [Layout and navigation](desktop-layout.md)
- [Day scheduling](desktop-day.md), [Week and Month calendars](desktop-calendar.md)
- [Task entry and editing](desktop-task-form.md)
- [Projects, milestones and task defaults](desktop-projects-allocation.md)
- [Settings and task-data reset](desktop-settings.md)
- [Accounts, saved sign-in and synchronization](desktop-accounts.md)

## Scheduling and data contracts

- [Five scheduling modes](scheduling-modes.md)
- [Recurring series](recurrence.md)
- [Execution, rescheduling and manual placements](execution-rescheduling.md)
- [Productivity analytics and tracker metrics](analytics.md)
- [Local record and synchronization contract](sync-contract.md)
- [Synchronization protocol](sync-protocol.md)

## Development and operation

- [Testing commands and tiers](testing.md) (recorded timings are historical)
- [CI/CD pipeline: CI, desktop releases and backend deployment](ci-cd.md)
- [Desktop/web boundaries](desktop-web-boundaries.md)
- [Backend setup and native authentication](backend-m8.md)
- [Backend API, schema and password recovery](backend.md)
- [Optional web API and browser sessions](web-api.md)
- [Render backend deployment](render-deployment.md)
- [Private-development direct PostgreSQL mode](direct-postgres.md)
- [Render direct-database verification](render-direct-desktop.md)

## Historical plans and verification

`milestone-*` plans, prompt collections and completion reports, the
`next-milestone-prompts/` collection, `productivity-redesign-plan.md`, and
`desktop-responsiveness-*` reports preserve the scope, findings and checks of
particular development batches. They are not current feature inventories or
instructions to implement pending work. Later implementation may supersede
limitations or proposed work in them; use the guides above for current behavior.

The [benchmark reports](../benchmarks/BASELINE.md) and other Markdown files in
`benchmarks/` record measurements for their stated scenarios and revisions,
not performance guarantees for every current workload.
