# Current-tree responsiveness validation

The fixes reduce measured pauses; physical Windows shaking remains unverified.
Automated geometry changes exercised a real Windows Tk window, but no human title-bar
or border drag was performed. Large resize/page-load delays remain; passing tests alone
does not establish that the user's symptom is solved.

## Why this comparison was repeated

The initial [diagnosis](desktop-responsiveness-diagnosis.md) and
[before/after experiment](desktop-responsiveness-results.md) used an earlier dirty
working-tree state. Another task subsequently replaced the execution/task-list UI with
the task-status board, added outcome/points handling and changed related tests/storage.
The user confirmed those edits were finished and asked to validate the current tree.
Those edits were preserved, including removed files. No old source was restored.

The current Day page renders `TaskStatusBoard` through `_refresh_status_board`; Calendar
calls `_refresh_day_panel` and `DayOutcomeController.detail`. Both still use the local
synchronous `_io` path and the existing direct-storage worker path. The status board
currently rebuilds cards when rendered. This is a new workload relative to the first
audit; it was inspected but not redesigned as part of these fixes.

All four current runs use the same source manifest, recorded as SHA-256 hashes of every
Python file under `app/` and `config/` in each JSON file. The probe also checks that these
files did not change during a run. The branch remains `SMV2-M5.5`, HEAD
`2aa927b5a97e37d576ec1fe0e5033773f71e8b32`; the manifest identifies the uncommitted state.

## Controlled reference and fixed runs

`--reference-behavior` temporarily disables only this task's responsiveness changes
inside the diagnostic process: restores CTk's native paint methods, the two-second full
GC scan, rebuilding all available-task buttons, and writing unchanged dependency values.
It does not edit files or revert the new UI/domain work. Method patches are restored on
exit. The reference adapter performs a few extra BooleanVar reads to reproduce the old
writes; this is a small additional instrumentation cost, so these are controlled ablation
results, not a byte-for-byte recreation of the former app.

Both modes use the same Probe hooks and 10/200-task fixtures, documented in the initial
diagnosis. The transport is synthetic (empty account, two-second worker wait, no network
or record upload). Each phase lasts eight seconds; delayed callbacks change how many
operations fit in that duration. Per-phase p95 distributions are therefore not identical
operation cohorts. Measurements are single-run observations, not statistical guarantees.

Exact command, after setting `$probePython` and `PYTHONPATH` as in the diagnosis:

```powershell
foreach ($probeSize in @(10, 200)) {
    & $probePython -m benchmarks.desktop_responsiveness --tasks $probeSize --reference-behavior --output "benchmarks/desktop-current-reference-$probeSize.json"
    & $probePython -m benchmarks.desktop_responsiveness --tasks $probeSize --output "benchmarks/desktop-current-after-$probeSize.json"
}
```

Runtime: Windows 10, Python 3.12.14, Tcl/Tk 8.6.12, CustomTkinter 5.2.2, 1920x1080 screen.
The original `.venv` points to a missing Python 3.10 executable; isolated dependencies
and the bundled runtime were used. No permanent runtime or dependency upgrade was made.
Runs were sequential, without tests running concurrently. No credentials, task contents,
SQL or callback arguments appear in the measurement logs.

## Implementation and verification

Production changes: `app/ui/paint_widgets.py`, `tk_lifecycle.py`, `day_page.py`,
`task_editor.py`, plus adapter imports/construction in `app/app.py` and the existing
account/calendar/components/feedback/guide/pages/planning/preferences/productivity widgets.
The full explanation and inventory are in the original results document.

No new threads, processes, timers for drawing, or worker polls were introduced. GC stays
on Tk's owner thread. Scheduling/Greedy v1 and persistence behavior were not changed by
this task. Existing account/workspace/stale-result, widget destruction, busy-state,
database-lock and shutdown behavior was exercised by the UI/sync suite.

- `pytest tests/ui tests/sync tests/direct/test_desktop_direct.py -q --tb=short`:
  **403 passed**, one FastAPI/Starlette test-client deprecation warning, 451.63 seconds.
- `pytest tests/ui/test_desktop_diagnostics.py tests/ui/test_responsiveness_regressions.py -q`:
  **14 passed**. This overlaps the broad run; it also covers the latest diagnostic additions.
- `ruff check . --exclude .diagnostic-deps`: passed.
- `compileall -q app backend benchmarks config tests`: passed.

Tests include native scrolling/selection/layout, no nested dispatch during paint, explicit
root idle flushing, GC generation selection/restoration, chip version safety/deletion,
unchanged dependency traces, diagnostic privacy, reference-mode restoration and source
manifest change detection. Existing UI/sync tests exercise the current task-status UI,
account switching, execution persistence, generation, destruction and shutdown.

Remaining limits: physical dragging, multiple monitors/DPI transitions, real remote sync
payloads and live network PostgreSQL were not benchmarked. The direct-storage tests use
the repository's test backend. Main-thread storage reads, eager widget creation and the
new board's card rebuilding remain possible follow-up targets. Full generational GC can
still be expensive under allocation pressure; it is no longer forced during every idle
two-second interval. The CTk adapters use private paint-canvas fields and should be tested
when that dependency is upgraded.

## Current measurements

The raw JSON files contain every callback's p95/worst duration, per-callback raw samples,
50 ms heartbeat lateness, CPU samples, Configure and Coalescer counts, active workers
and pending polls. CPU is mean sampled process utilization (100% = one core). Configure
counts include descendants; poll counts include the four-second status poll. Callback
durations are inclusive, so nested timings must not be summed.

All four `source_unchanged` flags were true and their manifests were identical.
SHA-256 of the sorted-key JSON manifest:
`d4e97abbc82d01e1141905fb66fc3573479a4864d8a2e355e8ce780414bfc31d`.

Raw files:


| Tasks | Phase | Reference p95 / worst ms | Fixed p95 / worst ms | CPU reference → fixed % |
|---|---|---|---|---|
| 10 | idle | 16.7 / 36.1 | 16.6 / 20.5 | 4.9 → 1.3 |
| 10 | move | 16.6 / 49.9 | 12.5 / 20.4 | 6.7 → 2.8 |
| 10 | resize | 448.7 / 562.3 | 324.5 / 326.4 | 91.2 → 90.0 |
| 10 | pages | 140.7 / 2217.9 | 371.5 / 978.6 | 27.4 → 47.4 |
| 10 | generate | 17.8 / 355.6 | 16.6 / 241.0 | 7.4 → 7.4 |
| 10 | sync, unconfigured | 16.6 / 65.0 | 13.8 / 20.2 | 2.9 → 0.0 |
| 10 | sync, synthetic latency | 16.6 / 59.8 | 15.7 / 17.7 | 2.2 → 1.0 |
| 10 | idle after | 16.6 / 64.0 | 13.7 / 22.0 | 2.0 → 0.0 |
| 200 | idle, startup settling | 34.4 / 633.7 | 15.5 / 230.2 | 9.3 → 6.7 |
| 200 | move | 16.7 / 87.3 | 13.4 / 16.7 | 8.8 → 7.6 |
| 200 | resize | 1007.5 / 1631.4 | 799.4 / 815.7 | 91.4 → 88.6 |
| 200 | pages | 489.0 / 1633.0 | 731.5 / 813.1 | 43.5 → 90.0 |
| 200 | generate | 2979.1 / 2979.1 | 389.3 / 1720.4 | 94.0 → 24.6 |
| 200 | sync, unconfigured | 16.6 / 66.0 | 15.9 / 16.7 | 4.6 → 0.2 |
| 200 | sync, synthetic latency | 16.5 / 91.3 | 14.4 / 18.7 | 2.6 → 0.2 |
| 200 | idle after | 15.3 / 90.0 | 13.6 / 23.5 | 2.5 → 0.6 |

Constructor wall time: **4537 → 4141 ms** (10 tasks), **14669 → 10833 ms** (200).
The first idle phase includes startup settling; idle-after is a better steady-state
measure. Shutdown is included in the last runner callback's duration, so it must not
be mistaken for a steady-state idle callback stall.

| Tasks | Phase | Configure root/all reference → fixed | Coalescer runs reference → fixed | Peak workers/polls reference → fixed |
|---|---|---|---|---|
| 10 | idle | 0/0 → 0/0 | 0 → 0 | 0/1 → 0/1 |
| 10 | move | 156/156 → 158/158 | 0 → 0 | 0/1 → 0/1 |
| 10 | resize | 78/2309 → 60/3417 | 36 → 25 | 0/1 → 0/1 |
| 10 | pages | 0/3184 → 0/3540 | 10 → 11 | 0/4 → 0/4 |
| 10 | generate | 0/269 → 0/448 | 2 → 2 | 1/2 → 1/2 |
| 10 | sync latency | 0/5 → 0/4 | 0 → 0 | 1/2 → 1/2 |
| 200 | idle | 2/832 → 0/678 | 1 → 0 | 0/1 → 0/1 |
| 200 | move | 154/154 → 160/160 | 0 → 0 | 0/1 → 0/1 |
| 200 | resize | 44/4744 → 38/5705 | 20 → 14 | 0/1 → 0/1 |
| 200 | pages | 0/1723 → 0/8396 | 9 → 8 | 0/1 → 0/1 |
| 200 | generate | 0/2548 → 0/2024 | 2 → 2 | 1/2 → 1/2 |
| 200 | sync latency | 0/5 → 0/4 | 0 → 0 | 1/2 → 1/2 |

DayTimeline paint counts remained zero during movement and resizing, and two during
generation, in both modes. The domain's coalescing is functioning; it does not eliminate
all native/CTk child geometry work. Descendant Configure counts increased in several
phases, and CPU remains high during resize/page transitions. This is not a uniformly
better latency distribution: **page p95 worsened** even though its worst stall decreased.

Selected callback measurements (p95 / worst ms):

| Tasks | Callback / phase | Reference | Fixed |
|---|---|---|---|
| 10 | GC timer / idle (4 callbacks) | 60.04 / 60.04 | 0.04 / 0.04 |
| 200 | GC timer / idle (4 callbacks) | 84.29 / 84.29 | 0.06 / 0.06 |
| 10 | CTk dimension handler / resize | 1.53 / 587.02 | 1.42 / 2.50 |
| 200 | CTk dimension handler / resize | 1.26 / 1644.69 | 1.20 / 2.61 |
| 10 | result poll including generation rendering | 388.62 / 388.62 | 199.95 / 199.95 |
| 200 | result poll including generation rendering | 2745.31 / 2745.31 | 1737.56 / 1737.56 |

The result poll's large duration is its `on_done` rendering, not merely checking a worker
event. Moving that rendering to a worker would violate Tk ownership. Its scheduling
interval was therefore not changed based on this number.

The implemented changes address confirmed nested idle dispatch, unconditional full-GC
scans, and avoidable widget/variable rebuilds. The remaining 816 ms resize and 1.72 s
generation pauses on the larger current UI mean the issue is **mitigated, not fully
resolved**. Next measured targets are bounded/incremental main-thread rendering in
`TaskStatusBoard.render`, `DaySchedulePage._render`, and visible-only rendering for the
large available-task area, alongside separate profiling of synchronous snapshot reads.
Any such work must preserve stale-result and record-version guards, and needs another
physical Windows dragging check and matched measurement run.

Generated raw telemetry JSON files are intentionally excluded from the repository; the measured summaries above are retained. Reproduction commands generate fresh local files.
