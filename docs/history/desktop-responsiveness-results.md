# Desktop responsiveness changes (Prompt 2)

**Historical measurements:** another task subsequently changed the Day/Calendar UI
and execution model. The user confirmed those edits were finished and requested validation
of the current tree. See [current-tree validation](desktop-responsiveness-current-validation.md)
for the final source-hashed controlled comparison and tests. The tables below retain
the original experiment; they do not certify the newer UI.

Measured pauses improved, but this is **not a claim that physical Windows dragging is
fixed**. No human title-bar/border drag was performed. Large live-resize and page
transitions still have substantial delays and require further work if they match the
user's symptoms.

The diagnostic milestone was completed first; see
[diagnosis and baseline](desktop-responsiveness-diagnosis.md). Work stayed on
`SMV2-M5.5`, HEAD `2aa927b5a97e37d576ec1fe0e5033773f71e8b32`, preserving the existing
dirty working tree. Measurements were taken on September 28, 2026 UTC.

## Changes

- `app/ui/paint_widgets.py`: app-owned scroll-frame, option-menu and textbox adapters
  stop their private CTk paint canvases from calling nested `update_idletasks()`.
  Tk's normal event loop performs pending drawing. Explicit root/widget idle flushes
  still work. There is no global CTk patch, added polling, worker or scheduling queue.
  The adapter uses CTk 5.2.2 internals, so it needs regression testing on CTk upgrades.
- `app/ui/tk_lifecycle.py`, `DesktopCollection.collect`: allocation-driven generational
  collection replaces unconditional full scans every two seconds. Collection and Tcl
  finalization remain on the Tk thread; the original automatic-GC state is restored
  only when the last desktop closes. Full collection can still occur under sufficient
  allocation pressure; it has no hard wall-time bound.
- `app/ui/day_page.py`, `_show_available`: reuse task buttons instead of destroying and
  rebuilding unchanged tasks. Remove vanished tasks, update changed labels, and refresh
  command closures when record versions change (RowRef equality ignores version).
- `app/ui/task_editor.py`, `DependencyPicker.set_choices`: do not write unchanged
  BooleanVars, which previously invoked CTk redraw traces for every choice on reload.
- Adopt the paint adapters in `app/app.py` and `app/ui/{account_page,calendar_page,
  components,day_page,execution_panel,feedback_dialog,guide_page,pages,planning_pages,
  preferences_editor,productivity_page,task_editor}.py`. These call-site changes retain
  the existing styling, geometry, scrolling and page structure.
  `execution_panel.py` was subsequently removed by the other edits; it was not restored.

No optimizer, storage, sync protocol, thread registry or scheduling semantics changed.
In particular, no extra CPU-heavy workers were introduced: generation remains on its
existing worker and still competes for the GIL. Measurements of generation include the
foreground result rendering, which was materially reduced by the rendering changes.
Existing stale-result guards, workspace epochs, busy-state guards, database locking and
shutdown behavior remain in place. The pre-existing unbounded worker registry and first
blocking shutdown wait were audited but not changed without a measured causal link.

## Repeated measurements

Same command, runtime, task-fixture specification, eight-second phases and operation
cadence as the baseline. Final runs were sequential and did not overlap tests.
JSON contains per-function callback p95/worst times, raw callback durations, heartbeat
samples, CPU and worker/poll snapshots:

- `benchmarks/desktop-baseline-small.json`, `desktop-baseline-large.json`
- `benchmarks/desktop-after-small.json`, `desktop-after-large.json`

All values below are **event-loop lateness**, not total time between ticks; the nominal
heartbeat is 50 ms. Callback times are inclusive and cannot be summed across nested
dispatch. CPU is mean sampled process utilization, 100% = one core.

| Tasks | Phase | Before p95 / worst ms | After p95 / worst ms | CPU before → after % |
|---|---|---|---|---|
| 10 | idle | 16.7 / 53.3 | 13.7 / 18.6 | 4.1 → 1.2 |
| 10 | move | 16.7 / 64.7 | 16.6 / 26.1 | 6.5 → 4.8 |
| 10 | resize | 440.2 / 488.5 | 296.0 / 348.9 | 91.0 → 92.3 |
| 10 | pages | 106.5 / 2056.6 | 356.9 / 936.9 | 31.0 → 43.8 |
| 10 | generate | 18.5 / 217.6 | 20.2 / 68.8 | 6.0 → 3.8 |
| 10 | synthetic sync latency | 16.7 / 60.9 | 13.6 / 16.7 | 1.8 → 0.2 |
| 200 | idle, includes startup settling | 34.7 / 630.0 | 16.7 / 213.1 | 9.0 → 5.0 |
| 200 | move | 18.6 / 60.6 | 15.0 / 24.8 | 7.6 → 5.4 |
| 200 | resize | 1002.2 / 1506.9 | 617.2 / 619.6 | 90.3 → 94.6 |
| 200 | pages | 491.1 / 1639.4 | 542.2 / 595.7 | 44.1 → 56.9 |
| 200 | generate | 534.7 / 2306.2 | 28.1 / 377.1 | 20.5 → 8.9 |
| 200 | synthetic sync latency | 13.1 / 73.1 | 24.8 / 27.2 | 2.8 → 0.4 |

Page p95 and resize CPU did **not** improve uniformly. Each phase has a fixed elapsed
duration, so stalls change the number of operations delivered; these are not matched
per-operation latency distributions. The pages phase cycles through Day/Week/Month/
Productivity/Settings/Projects/Allocation as time allows, rather than guaranteeing all
seven finish. This throughput difference and a single run per final fixture limit causal
claims. In particular, removing nested dispatch can move work from one long callback
into more distinct events without eliminating all painting cost.

| Tasks | Phase | Configure root/all before → after | Coalescer runs before → after | Peak workers/polls before → after |
|---|---|---|---|---|
| 10 | move | 156/156 → 160/160 | 0 → 0 | 0/1 → 0/1 |
| 10 | resize | 78/2299 → 62/3288 | 38 → 26 | 0/1 → 0/1 |
| 10 | pages | 0/2993 → 0/3720 | 8 → 11 | 0/4 → 0/4 |
| 10 | generate | 0/89 → 0/121 | 2 → 2 | 1/3 → 1/3 |
| 200 | move | 154/154 → 154/154 | 0 → 0 | 0/1 → 0/1 |
| 200 | resize | 46/4628 → 46/8476 | 20 → 18 | 0/1 → 0/1 |
| 200 | pages | 0/1597 → 0/8407 | 10 → 11 | 0/1 → 0/1 |
| 200 | generate | 0/995 → 0/491 | 2 → 2 | 1/3 → 1/3 |

DayTimeline paints remained zero during scripted movement and resizing. Coalescer counts
include layout checks and paints; raw JSON separates callback names. Poll counts include
the four-second status poll. Short worker jobs may finish between 50 ms samples.

Constructor wall time: small 4076 → 3927 ms; large 13445 → 10565 ms. Large generation's
worst delay fell 2306 → 377 ms; its remaining delay still warrants attention. Full-GC
idle callback worst time fell 58.5 → 0.05 ms (small) and 81.7 → 0.04 ms (large).
These are observed workload values, not a promise that future full collections are fast.
The last idle callback duration includes shutdown; use heartbeat lateness rather than
that callback maximum for steady-state idle comparison.

## Verification and remaining limits

Focused regression tests cover idle-dispatch isolation, explicit root flushing, scrolling,
selection, resizing, widget destruction, GC pressure/owner-thread behavior, multiple-window
GC restoration, no-op dependency reloads, and task-button identity/version/deletion behavior.
The current-tree verification completed with 403 UI/sync/direct tests passing and 14
focused tests passing (these groups overlap). Repository Ruff and compilation passed.
The broad run reported one dependency deprecation warning from FastAPI/Starlette's
httpx test-client integration, not a test failure.

Physical Windows dragging, multi-monitor/DPI changes, real remote sync payloads, and a
live PostgreSQL server were not measured. The synthetic sync transport waits two seconds
in a worker for an empty account and sends no records. Existing local synchronous page
reads and eager large widget construction remain potential bottlenecks; resize is still
CPU-heavy. A bounded visible-row rendering approach and separate storage/render timing
would be the next investigation, not an unsupported claim that adding threads solves it.

The probe is entirely opt-in and uses a temporary DB/settings directory. No personal
tasks, credentials, SQL or callback argument values are logged. Run the manual command
in the diagnosis report to complete the physical check on the user's actual runtime.
