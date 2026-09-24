# Milestone 4 — Production Web UI

You are preparing **Milestone 4** of this project.

Do **not** implement Milestone 4 yourself.

Your job is to:

1. Inspect the entire current repository.
2. Verify that Milestones 0–3 are actually complete and the application is functional.
3. Understand the real current backend, domain model, optimizer, persistence, authentication, synchronization, API, and existing UI.
4. Identify missing functionality or inconsistencies that would prevent Milestone 4 from being implemented correctly.
5. Then generate a sequence of **implementation-ready prompts for Claude Code** to implement Milestone 4.

Because Milestone 4 is large, **split the implementation into multiple Claude prompts**. Prefer several focused prompts with clear dependencies rather than one massive prompt.

The final result must be a **real, useful web application**, not a demo UI, mockup, prototype, or frontend that only manipulates temporary local state.

---

# 1. Preflight audit: verify Milestones 0–3 first

Before designing the Milestone 4 prompts, inspect the complete repository.

Read at minimum:

* `AGENTS.md`
* `CLAUDE.md`
* `README.md`
* backend/API code
* database models and migrations
* optimizer/engine code
* scheduling services
* synchronization code
* authentication/account code
* conflict-resolution services
* CSV import/export code
* preferences/configuration code
* project-related models/services
* tests
* frontend/UI code
* configuration and dependency files

Treat the **current repository as the source of truth**. Do not assume older descriptions of the project are still accurate.

Run the project's existing verification commands, including the appropriate equivalents of:

* test suite
* linting
* compile/type/build checks
* backend startup/import validation
* frontend build/tests if a frontend already exists

Do not silently ignore failures.

Determine whether the following functionality from previous milestones actually exists and works:

* stable UUID-based identities
* real dates/timestamps
* users/accounts
* `project_id`
* Task / ScheduledTask / TaskExecution separation
* completion and execution tracking
* recurring-task data preservation
* deadlines
* estimated vs actual duration
* created/updated timestamps
* sync/version metadata
* PostgreSQL persistence
* FastAPI API
* authentication
* local/offline storage
* synchronization
* conflict detection/resolution
* deletion/tombstone handling
* account ownership
* identity-preserving CSV import/export
* schedule freshness metadata
* per-user preferences
* per-date preference overrides
* allocation/planning services
* scheduling engine modes
* `precise_greedy`
* `adhd_friendly`
* fixed-block categories
* minute-precise scheduling
* project CRUD if previously implemented
* productivity/execution history where applicable

Also verify that the scheduling engine is reachable through the backend/service layer rather than requiring the UI to call Python optimizer internals directly.

## Important preflight rule

If Milestones 0–3 contain defects or missing integration that Milestone 4 depends on:

**Do not redesign around the defect.**

Instead, create a short **Milestone 4 Preflight Repair** Claude prompt before the UI prompts.

Only include repairs that are necessary for Milestone 4.

Do not perform unrelated refactors.

---

# 2. Architecture requirement: this must be a real web application

Milestone 4 should produce a production-style **web application**, not continue growing a desktop-only prototype.

Inspect what frontend technology already exists.

### If a modern web frontend already exists

Keep its established framework and conventions unless there is a strong technical reason not to.

### If there is no real web frontend yet

Use a modern, maintainable frontend stack appropriate for the existing FastAPI backend.

A reasonable default is:

* React
* TypeScript
* Vite
* React Router
* TanStack Query for server state
* React Hook Form for forms
* Zod or an equivalent typed validation layer
* `date-fns` or equivalent for calendar/date calculations
* an established accessible component system rather than hand-building every primitive

Choose libraries based on the actual repository and avoid unnecessary dependencies.

Use standard frontend architecture:

* reusable components
* page/layout separation
* typed API client
* server-state management
* local UI state separated from persisted domain state
* loading states
* empty states
* error states
* optimistic updates only where safe
* clear authentication state
* accessibility
* responsive layouts
* keyboard accessibility
* sensible focus handling
* reusable theme/design tokens
* proper form validation
* automated tests

The frontend must communicate through the backend/API/service contracts.

Do not duplicate core scheduling business logic in JavaScript merely to make the UI work.

Backend/domain validation remains authoritative.

---

# 3. General design direction

The UI should feel like a polished scheduling/productivity application rather than an engineering dashboard.

## Visual style

Use:

* rounded cards and controls
* rounded task blocks
* soft borders
* subtle shadows where appropriate
* generous spacing
* readable typography
* muted/pastel colors
* category colors that are visually distinct without being highly saturated
* consistent spacing and sizing tokens
* light mode
* dark mode

Avoid:

* harsh/vibrant colors
* excessive gradients
* sharp rectangular controls
* excessive visual clutter
* raw configuration names intended for developers
* exposing YAML syntax to ordinary users

Tasks should retain consistent category colors throughout Day, Week, Month, Project, and allocation views.

Fixed blocks must use their actual category color rather than all sharing one generic fixed-block color.

Dark mode must be a genuine theme, not simply inverted colors.

---

# 4. Application shell and navigation

The application should have a collapsible left sidebar.

It starts **collapsed**.

Use a standard hamburger/three-line control to open it.

Navigation:

* Day Schedule
* Week Schedule
* Month Schedule
* Project Schedule
* Settings
* Account
* About

Default route/page:

**Day Schedule**

The sidebar opening/closing animation must be smooth.

Fix the current behavior where resizing or moving the desktop/window causes navigation/layout elements to shake, jump, or resize poorly.

The layout should behave normally across common laptop and desktop widths and degrade gracefully at smaller widths.

Use responsive CSS/layout mechanisms rather than absolute pixel positioning wherever possible.

---

# 5. Day Schedule

This is the main working screen.

When opened, it should automatically select **today's real date** and load today's persisted data.

Do not use artificial numeric days such as `1`, `2`, `3` as the actual date model.

The page is broadly divided horizontally:

* upper workspace
* lower workspace

Exact pixel-perfect 50/50 sizing is not mandatory if a more usable responsive ratio is appropriate.

---

## 5.1 Upper workspace

The upper area contains:

### A. Schedule timeline

Display today's schedule as a horizontal time-based timeline.

Hour markers should be visible.

Tasks should be positioned according to their actual scheduled start/end times.

Fixed blocks are shown immediately because their time is known.

After optimization, scheduled flexible tasks also appear here.

Use minute-precise placement.

The UI may render proportional widths rather than one DOM element per minute.

### B. Unscheduled / available tasks

Below the schedule timeline, display the tasks available for that date that have not yet been placed into the generated schedule.

If they fit, display them directly.

If there are many, use an appropriate horizontal/vertical scroll region.

Task cards should show enough useful information to identify them without becoming overly large.

---

## 5.2 Task interactions

Selecting/clicking a task should expose task actions.

At minimum:

* edit, if supported
* remove/delete

Do not permanently display destructive buttons on every card if a context menu/popover is cleaner.

Deletion must use the actual persisted deletion/service behavior.

If deletion can affect synchronization or ownership, show the corresponding error clearly.

For destructive operations that remove substantial data, use confirmation where appropriate.

---

# 6. Add Task panel

The lower workspace should contain a clearly separated Add Task area and action/control area.

The task form should ask for the fields required by the **current domain model and scheduling engine**.

Do not simply copy fields from the old UI.

Inspect the actual engine and model first.

Avoid asking the user for fields that are implementation details or automatically generated.

For example, never ask users to type:

* UUID
* `user_id`
* version numbers
* sync metadata
* created/updated timestamps

These should be generated or inferred by the application.

Use dropdowns/selects for discrete values such as:

* category
* project
* fixed/flexible type where appropriate
* other enumerated fields

---

# 7. Time input

Remove the old 30-minute UI restriction.

The user must be able to specify minute-precise values.

Use a high-quality time input supporting both:

* direct typing
* picker/scroll interaction

Display user-facing time in a standard 12-hour format:

`h:mm AM/PM`

Internally convert to the representation expected by the current API/domain model.

Do not make the user enter integer minutes-from-midnight.

Duration should also support minute precision.

Validation should explain errors in human terms.

---

# 8. Tags

Provide a tag input.

Behavior:

1. User types a tag.
2. Pressing Enter creates a tag chip.
3. Created tags appear in a small tag area.
4. Individual tags can be removed.
5. The area can scroll/wrap gracefully if there are many tags.

Use the repository's real tag representation.

Do not invent a new incompatible schema.

---

# 9. Fixed-task validation

When a user creates a fixed block:

* it must be inside the day's allowed scheduling window
* its start must be before its end
* it must not overlap another fixed block
* any other backend hard constraints must be respected

If the user presses **Add Task** and the block is invalid:

* do not persist it
* clearly explain the specific conflict
* preserve the form where helpful so the user can fix it

Backend validation is authoritative.

Frontend validation should provide faster feedback, but must not replace backend validation.

---

# 10. Day Preferences

The old concept of editing `config.yaml` or `task_prefrence.yaml` directly should become a user-facing **Day Preferences** dialog/drawer.

Do not present YAML syntax.

When opened from a day, it should show the effective scheduling preferences for that date.

The controls should be generated from the preferences actually supported by the selected scheduling engine.

Examples may include:

* scheduling window
* preferred category times
* category weights
* spacing preferences
* tag relationships
* relevant reward weights

Only expose settings that have meaningful semantics for the selected engine.

The UI must clearly distinguish:

* inherited default values
* date-specific overridden values

Allow the user to:

* modify a date-specific preference
* save it
* reset one override to its inherited/default value
* reset the whole date to inherited defaults

Persist this through the proper preference service.

Do not simply mutate module globals in the frontend or backend process.

---

# 11. Day action buttons

Provide:

* Import CSV
* Export CSV
* Make Schedule
* Reset Day

Use sensible ordering and visual hierarchy.

---

# 12. Make Schedule behavior

The button must call the real scheduling backend/service.

It must use the currently selected engine mode.

Supported modes currently expected:

* `precise_greedy`
* `adhd_friendly`

Verify their actual names/contracts during the audit.

### First scheduling

Generate the schedule normally using:

* fixed blocks
* eligible flexible tasks
* current preferences
* current date
* existing constraints

### Pressing Make Schedule again

Do not create duplicate scheduled-task records.

If nothing has changed and the existing schedule is still current:

* do not generate redundant data
* communicate that the schedule is already current

If tasks were added after the schedule was generated:

preserve already committed placements as locked/existing placements when the domain/service supports this behavior, and schedule new work into remaining available time.

Do **not** permanently mutate scheduled tasks into fake fixed tasks merely as a UI shortcut.

Use the existing scheduling/incremental scheduling service or extend it properly if necessary.

If there are no remaining eligible tasks or no available time, communicate this without modifying the schedule.

---

## 12.1 Scheduling engine dropdown

Place an accessible dropdown labeled **Engine** immediately beside **Make Schedule**.

Include all currently supported scheduling engine modes, using these user-facing labels:

* **Normal** -> `precise_greedy` (the current standard engine)
* **ADHD friendly** -> `adhd_friendly` (the existing ADHD engine)

Use **Normal** when no saved engine preference applies. Otherwise display the effective saved engine selection, respecting default preferences and date-specific overrides.

Selecting an engine must use the existing preference/API/service contracts. Make Schedule must run the selected engine; this must not be a cosmetic dropdown. Preserve the internal mode identifiers and existing optimizer behavior.

For Day Schedule, persist a changed selection as the selected date's engine override. Keep the Settings default-engine control consistent with the same labels and preference inheritance rules. Changing a date's engine must not silently change the user's default engine.

If Make Schedule is available for a date range, define the selector's range behavior explicitly in the implementation prompts, preserving existing date-specific overrides unless the user explicitly chooses to replace them. Do not display a single effective engine when the range has mixed engine preferences.

Changing the engine must update persisted schedule freshness through the existing service. Do not automatically regenerate on selection; generate when Make Schedule is pressed. Update Day Preferences to show controls relevant to the selected engine.

Keep the dropdown keyboard accessible, adjacent to the button on desktop, and usable when controls wrap on smaller screens. Disable engine changes while saving the selection or generating a schedule. Show recoverable persistence errors and prevent generation with a selection that failed to save.

Include this requirement in Prompt 3 (Day Schedule), keep Prompt 6 (Settings) consistent, and verify it in the final integration prompt.

Acceptance tests must cover both labels and internal mode mappings, Normal as the fallback, restoration of saved selections, date override/default isolation, scheduling with each engine, freshness after an engine change, and save-failure handling.

---
# 13. Free-time visualization

Every unscheduled gap inside the day's allowed scheduling window should appear visually as:

**Free time**

However:

Prefer to compute these free-time blocks for presentation rather than persist fake `Task` records called `"free time"`.

They should represent empty intervals, not pollute task history, synchronization, ML training, or execution history.

---

# 14. Reset Day behavior

This should behave like a real destructive reset.

Use confirmation before deleting persisted day data.

Reset should:

* clear the task-entry form
* remove tasks/scheduled placements for the selected date according to the product's deletion rules
* clear date-specific preference overrides
* return preferences to inherited defaults
* clear generated schedule state/freshness as appropriate

Do **not** set every scheduling/reward weight to numeric zero.

That would create an invalid or meaningless configuration.

---

# 15. Week Schedule

The Week page should display a real calendar week.

The current week should load by default.

Top area:

* seven day columns
* clear weekday/date headers
* time-of-day axis where useful
* scroll vertically if displaying the full 24-hour day
* responsive horizontal handling where necessary

Past dates should remain visible but appear visually muted/greyed.

Do not delete tasks simply because a date passed.

---

## Week task behavior

Before a day's detailed schedule has been generated:

* tasks may be displayed in creation/input order rather than implying an exact scheduled time

After the day's schedule has been generated:

* show tasks using their actual chronological scheduled positions/order

Selecting a day should make it the active day.

Opening a day should take the user to the Day Schedule page for that date.

Support an intuitive interaction such as:

* single click selects
* Open Day action
* optional double-click shortcut

Do not make important functionality depend exclusively on double-click because that is poor for accessibility/touch devices.

When Day Schedule was entered from Week Schedule, provide a clear Back to Week control and preserve the week context.

---

## Week lower workspace

Reuse the task-entry and controls architecture from Day Schedule where possible.

Avoid duplicated implementations.

The Week page should allow tasks to be created for the selected date.

Per the requested workflow, it does not need a separate Make Schedule button if scheduling is intentionally performed from the Day page.

Reset Week should be clearly destructive and require confirmation.

It should only affect the selected week according to backend/domain semantics.

---

# 16. Month Schedule

Implement a **real calendar month**, not a hardcoded 30-day period.

Correctly handle:

* 28-day months
* 29-day February
* 30-day months
* 31-day months
* weekday alignment

Display the current month by default.

Show the month name prominently.

Provide a month selector/dropdown.

For the current requested scope, expose months from the current year.

Past days remain visible and are visually muted.

Past months in the selector may also be visually muted where the component supports this clearly.

Never delete old data just because it is in the past.

Each date should be selectable/openable similarly to the Week page.

Before optimization, task summaries may use input order.

After a day's schedule has been generated, represent the day's tasks in actual chronological order.

Use real persisted dates.

---

# 17. Allocation Planning view

The system now has a distinction between:

* deciding **which date** a task belongs on
* generating the detailed intraday schedule

Expose this distinction clearly.

Provide an Allocation Planning experience that lets the user inspect:

* tasks allocated to each date
* tasks that remain unallocated
* reason a task could not be allocated
* relevant deadline/project/date information
* allocation status

Do not confuse allocation with the detailed Day schedule.

Use the actual backend allocation service and reason codes/messages.

---

# 18. Project Schedule / Project management

Project functionality is required for the useful final application.

Inspect the repository first.

If project CRUD/services from earlier milestones already exist, build a real Project page rather than leaving a permanent placeholder.

Users should be able to:

* create projects
* rename/edit projects
* archive projects if supported
* delete projects if supported
* assign tasks to a project
* view project tasks
* filter by project
* inspect scheduled/unscheduled work belonging to that project

Follow backend ownership rules.

If necessary backend project functionality is genuinely missing despite being required by previous milestones, put the required backend repair into an earlier Claude prompt before building this page.

A temporary placeholder is acceptable only during intermediate implementation steps, not as the final Milestone 4 result.

---

# 19. Engine-mode selection

Expose the scheduling engine mode to the user.

Expected modes:

* `precise_greedy`
* `adhd_friendly`

Verify these against the actual repository.

The selection must persist through the existing preference/settings service.

The UI should briefly explain the practical difference without exposing low-level implementation details.

Any settings that only affect one engine should be:

* clearly labelled
* conditionally displayed, or
* disabled with explanation when another engine is selected

Do not display configuration controls that silently do nothing.

---

# 20. Reward/preferences alignment

The existing Reward Config concept must be redesigned around the actual active engine.

Do not expose obsolete settings from unused optimizers.

Determine exactly which settings affect:

* `precise_greedy`
* `adhd_friendly`
* any other currently supported mode

Then surface only relevant controls.

Default preferences belong in Settings.

Date-specific overrides belong in Day Preferences.

Changes must persist through the backend rather than only modifying in-memory Python values.

---

# 21. Account and connection workflow

Build a genuine account experience using the authentication/backend implemented in Milestone 3.

Support:

* registration
* login
* logout
* active-account display
* backend connection configuration if this remains a supported deployment feature
* backend connectivity status

Do not create fake authentication.

---

# 22. Connect existing local data

Users may already have local/offline data before signing into an account.

Do **not** automatically upload or claim that data.

Provide an explicit workflow:

1. User signs in.
2. Detect unassociated local data.
3. Explain that local data exists.
4. Ask whether the user wants to associate/import it into the signed-in account.
5. Show an appropriate preview/count where practical.
6. Only upload/associate after explicit confirmation.

Handle ownership conflicts clearly.

---

# 23. Sync status and controls

Expose synchronization state in a compact but useful place.

The user should be able to see:

* online/offline status
* whether local changes are pending
* number of pending changes if available
* last successful synchronization
* sync in progress
* sync failure
* actionable error message

Provide:

**Sync now**

Use the existing synchronization service.

Do not implement a second independent synchronization algorithm in frontend state.

---

# 24. Conflict resolution

Use the conflict service built previously.

When a conflict occurs, present both sides clearly:

* local value
* remote value
* modification information where available
* deletion/tombstone state

Support the resolutions actually provided by the backend, such as:

* keep local
* keep remote
* merge where the service supports it

Do not silently use last-write-wins in the UI when a conflict is known.

Deletion conflicts must be handled explicitly.

The frontend should be a view/controller for the established conflict service rather than containing conflict-resolution business logic.

---

# 25. Schedule freshness

The existing schedule freshness indicator must work across:

* application restart
* login/logout
* synchronization
* task changes
* preference changes
* allocation changes
* engine-mode changes

It should show a meaningful status such as:

* Current
* Out of date

Use persisted freshness/version metadata.

Do not derive freshness only from temporary frontend memory.

When a schedule becomes stale, make the reason available where feasible.

---

# 26. CSV import/export

Use the new identity-preserving CSV format implemented by previous milestones.

Import/export must preserve relevant IDs/version/domain information according to the repository's established format.

Clearly handle errors including:

* duplicate IDs
* invalid rows
* ownership mismatch
* conflicts
* malformed dates/times
* unknown references
* unsupported format/version

Provide meaningful user-facing messages instead of raw stack traces.

Do not revert to the legacy CSV format.

---

# 27. Settings

Settings should include:

### Language

Only:

* English

Structure the control so additional languages could be added later.

### Appearance

* Light
* Dark

Persist the user's choice.

### Default scheduling preferences

Expose the same preference model used by Day Preferences, but editing here changes the user's defaults.

Date-specific overrides should continue to inherit from these defaults unless explicitly overridden.

### Engine mode

Allow selection of the default scheduling engine.

---

# 28. Account page

Display data from the actual authenticated account.

At minimum:

* Name
* Email
* Account status/plan

Mask email in the requested display form where appropriate, leaving only a small identifying portion visible.

Do not hardcode user information.

If no paid-account infrastructure exists yet, the default plan may display as:

**Normal**

but implement this in a way that can support future plan values.

Include a **Performance** section.

It may be an intentional empty state for this milestone:

> Performance analytics will appear here in a later milestone.

Do not fabricate statistics.

---

# 29. About page

Keep this short.

Explain that:

* the application is a personal scheduling/productivity project
* it was created by **Ramtin Rezaei**
* its purpose is to experiment with intelligent scheduling and productivity tools

Do not make it overly promotional.

---

# 30. Recurrence scope

Recurring-task **data must be preserved correctly**.

Distinct recurring occurrences must not accidentally overwrite each other.

However, do not invent recurring-task expansion logic if the current backend/domain does not support it yet.

If full recurrence expansion is outside Milestone 4, make that limitation explicit in the audit and protect recurrence data during all UI operations.

Do not create a fake Repeat Task control that does not actually work end-to-end.

---

# 31. Error handling and UX quality

Every major interaction should have proper:

* loading state
* disabled state while appropriate
* empty state
* success feedback when useful
* recoverable error state
* validation feedback

Avoid browser `alert()` for ordinary application flows if the selected component system has proper dialog/toast components.

Never expose Python stack traces or raw database errors to users.

Map backend errors into understandable messages while keeping logs useful for developers.

---

# 32. Accessibility

Use reasonable accessibility conventions:

* semantic elements
* labels associated with form inputs
* keyboard navigation
* focus management
* adequate contrast
* no color-only status communication
* accessible modal/dialog behavior
* visible focus states
* buttons rather than clickable generic divs for actions

The scheduling UI can be visually complex, but basic actions must remain keyboard accessible.

---

# 33. Responsive behavior

This is primarily a desktop productivity application, but it should not collapse when the viewport changes.

At smaller widths:

* sidebar becomes overlay/drawer if appropriate
* panels can stack
* scrollable schedule regions remain usable
* forms stay readable
* navigation remains stable

Do not recreate the current shaking/jumping behavior when the window changes size.

---

# 34. Testing requirements

Milestone 4 must include tests.

Claude prompts should explicitly require appropriate tests for each section.

At minimum cover:

### Component/unit tests

* task form validation
* minute-precise time handling
* tags
* fixed-block conflict errors
* category rendering
* theme switching
* inherited vs overridden preferences
* engine-mode controls
* stale/current schedule indicator

### Integration tests

* login/logout
* account loading
* backend error handling
* creating/editing/deleting tasks
* Make Schedule
* rerunning scheduling without duplication
* incremental scheduling after adding tasks
* sync
* conflict resolution
* project assignment/filtering
* CSV import/export

### Calendar tests

* 28-day month
* leap-year February
* 30-day month
* 31-day month
* week transitions
* year/date boundaries where relevant
* past-day visual state

### End-to-end smoke tests

At least one realistic flow should cover:

1. launch app
2. authenticate
3. create fixed and flexible tasks
4. configure preferences
5. generate schedule
6. inspect Week/Month
7. modify a task
8. observe schedule become stale
9. regenerate
10. sync successfully

Use the testing tools appropriate to the selected frontend framework.

Do not make external production services necessary for normal automated tests.

---

# 35. Engineering requirements

Follow existing repository conventions.

Prefer extending existing services over rewriting them.

Avoid:

* giant page components
* duplicated domain logic
* tightly coupled UI/backend code
* global mutable state
* hardcoded users
* hardcoded fake API responses
* temporary demo data appearing in production
* storing authentication secrets in unsafe frontend storage
* silently swallowing API failures
* introducing a second task/domain model in the frontend

Use typed DTOs/schema-derived types where practical.

Keep API contracts centralized.

Do not make major unrelated optimizer changes during this milestone.

---

# 36. Documentation

At the end of Milestone 4, documentation should explain:

* how to install frontend dependencies
* how to run backend and frontend locally
* required environment variables
* how authentication works at a high level
* how sync works at a high level
* how to run tests
* how to build production frontend assets
* Render/deployment expectations if applicable

Do not allow README instructions to become inconsistent with the actual commands.

---

# 37. How to divide the Claude prompts

After completing your audit, create a sequence of Claude Code prompts.

Prefer approximately this breakdown, adjusting it to the actual repository:

### Prompt 0 — Milestone 4 preflight repairs

Only if needed.

Fix blockers from Milestones 0–3 that prevent UI implementation.

### Prompt 1 — Web foundation and design system

Frontend architecture, routing, application shell, sidebar, typed API client, query/state architecture, themes, reusable components, test foundation.

### Prompt 2 — Account, local-data connection, synchronization and conflicts

Authentication UI, account state, backend connection, local-data association, sync status, Sync Now, conflict resolution.

### Prompt 3 — Day Schedule

Today's loading, timeline, task backlog, task CRUD, minute-precise form, tags, fixed blocks, Day Preferences, Make Schedule, stale/current state, free-time visualization, import/export controls.

### Prompt 4 — Week and Month

Real calendar week/month views, navigation to Day, past-state styling, chronological scheduled display, task creation for selected dates, true month behavior.

### Prompt 5 — Allocation and Projects

Allocation planning, unallocated reasons, project CRUD, project filtering, task-project assignment, Project Schedule view.

### Prompt 6 — Settings, engine configuration, Account and About

Engine mode, default preferences, reward/preferences alignment, appearance/language, account details/performance empty state, About page.

### Prompt 7 — Integration hardening and production polish

CSV edge cases, freshness after restart/sync, responsive behavior, accessibility, E2E tests, build verification, documentation and final cleanup.

If the repository architecture suggests a better split, change the boundaries, but explain why.

---

# 38. Requirements for every Claude prompt

Each generated Claude prompt must be independently understandable.

Each one should tell Claude to:

1. Read `AGENTS.md`, `CLAUDE.md`, README and relevant repository files first.
2. Inspect the implementation produced by all prior milestone/prompts.
3. Do not assume a feature exists merely because an earlier prompt requested it.
4. Preserve working functionality.
5. Use the current repository as the source of truth.
6. Implement only that prompt's scope.
7. Add/update tests.
8. Run relevant tests and quality checks.
9. Fix regressions caused by its changes.
10. Update documentation where the change affects usage.
11. Avoid unrelated refactoring.
12. Report:

* files changed
* architecture decisions
* tests added
* commands run
* remaining limitations

Every prompt should include concrete acceptance criteria.

Do not use phrases like:

* "make it look nice"
* "add a modern UI"
* "connect everything"

without explaining exactly what success means.

---

# 39. Final verification prompt

The final Claude prompt must inspect the completed application as a whole rather than trusting previous prompts.

It should verify:

* backend starts
* frontend starts
* production frontend build succeeds
* authentication works
* task CRUD works
* fixed-block validation works
* minute-level times work
* Day scheduling works
* repeat Make Schedule does not duplicate records
* Week navigation works
* Month is a real calendar month
* project functionality works
* allocation view works
* preferences persist
* per-date overrides work
* engine selection works
* sync works
* conflict resolution works
* CSV import/export works
* schedule freshness persists
* light/dark theme works
* responsive resizing works
* tests pass
* lint/type/build checks pass
* README commands match reality

Fix only issues necessary to make the documented Milestone 4 workflow function correctly.

---

# 40. Output format

Your response should contain:

## A. Repository audit

Concise summary of:

* current architecture
* what Milestones 0–3 successfully implemented
* missing/broken prerequisites
* existing frontend state
* backend/API capabilities relevant to Milestone 4
* important risks or inconsistencies

## B. Recommended Milestone 4 architecture

Explain the frontend architecture and major library choices based on the actual repository.

Keep this practical and concise.

## C. Implementation sequence

List the Claude prompts in execution order and explain dependencies between them.

## D. Full Claude prompts

Provide every prompt in full, ready for me to copy directly into Claude Code.

Do not implement Milestone 4 yourself.

Do not stop at a high-level plan.

The primary deliverable is the complete set of detailed Claude implementation prompts required to turn the current project into a **functional, maintainable, production-style scheduling web application**.

