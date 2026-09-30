# Windows responsiveness: viewport and incremental rendering

This investigation uses the current checkout, including the staged task-status,
account, persistence and sync work. Those changes and the earlier paint/GC fixes
were preserved. No optimizer, storage, sync, worker, or shutdown policy was changed.

## Evidence and changes

The initial 200-task run measured 588.4 / 593.4 ms p95 / worst resize lateness,
508.9 / 560.6 ms page-change lateness, and 428.5 / 1705.9 ms generation lateness.
Automated movement was 8.2 / 12.8 ms, with 160 root events and no descendant events.
These are new measurements of this checkout, not the historical JSON results.

The available-task tree accounted for 6,200 of 7,933 resize Configure events:
2,200 buttons, 2,000 button canvases and 2,000 labels. The status board was empty
during this phase. Suppressing its rebuild therefore did not improve resize.

Process-local experiments on that same source, with cProfile enabled, measured:

| 200-task experiment | Resize p95 / worst ms | Page p95 / worst ms |
|---|---:|---:|
| Full page | 478.7 / 478.7 | 691.4 / 691.4 |
| Lightweight page during resize | 22.1 / 27.3 | 273.3 / 340.1 |
| Available-task widgets disabled | 270.6 / 282.6 | 189.5 / 255.7 |
| Status-board rendering disabled | 477.9 / 477.9 | 770.0 / 770.0 |

These four-second exploratory runs deliver different operation counts; their
page mixes and final geometry differ. They establish where to investigate, not
matched production speedups. Their final generation phase includes shutdown:
**do not use its delay distribution as generation latency**. The final harness
separates shutdown and uses fixed operation counts for comparisons below.

The initial profile attributed 1.882 seconds to status-board rendering, including
1.416 seconds constructing 96 cards and 0.458 seconds repeatedly setting button
states. The generation result callback took 2.141 seconds under profiling.
The same trace recorded 0.095 seconds across three Day snapshot builds,
0.036 seconds in 628 SQLite execute calls, 0.018 seconds submitting grid operations,
and 0.001 seconds in 30 scroll-region callbacks. Rounded CTk drawing accounted for
3.640 seconds inclusive, with 3.196 seconds in button drawing. Tk's native layout
and painting also run inside its mainloop/Tcl calls; Python grid submission time
does not measure all native layout cost. Inclusive times overlap and must not be
summed. Profiling adds overhead and worker/GIL contention can affect wall times.

The selected fixes are limited to two production files:

- `DaySchedulePage`: above 24 available tasks, instantiate only the viewport plus
  spare rows. Native grid row sizes preserve the full scroll extent. Unchanged
  visible buttons retain identity; commands refresh version preconditions. Up/Down
  can reach entries beyond the viewport. The existing CTk scrolling and scaling
  behavior remains in use, with coalesced viewport refresh and no polling loop.
- `TaskStatusBoard`: reuse unchanged cards by placement ID, refresh the card used
  by callbacks, update wrap widths on responsive transitions, and only write button
  states when they change. Large boards create cards in approximately 8 ms work
  slices, yielding for 10 ms between slices. A single widget construction may
  exceed the budget. Boards of at most 12 cards remain synchronous. New data and
  destruction cancel unfinished batches. New cards are constructed off-grid and
  mapped together when ready, so each batch does not resize/redraw the tall board.
  Offscreen cards are retained but removed from active grid layout, with their
  measured row heights reserved. Scrolling restores them in the existing outer
  viewport. Focused cards stay mapped; Up/Down reveals adjacent cards. Every widget
  operation stays on Tk.

An intermediate implementation mapped each card immediately. Although it reduced
the worst generation delay, mean sampled CPU rose from 24.7% to 82.1%. The second
profile showed repeated CTk frame/dimension drawing as the board grew. Off-grid
construction followed by one layout commit was selected to remove that overhead;
the final measurements below include that correction.

Snapshot reads were not the dominant measured cost in the initial trace, so this
change does not add storage threads or weaken existing workspace/load guards.
There is no broad CTk monkey-patch in production and resizing remains enabled.

## Reproduction and provenance

Use a supported Python with the repository dependencies. On this host, commands
used the existing Python 3.12.14 runtime and isolated CTk 5.2.2 dependencies:

```powershell
$probePython = 'C:\Users\Ramtin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = "$PWD/.diagnostic-deps;$PWD"
foreach ($size in @(10, 200)) {
    & $probePython -m benchmarks.desktop_responsiveness --tasks $size --fixed-work --baseline-ui benchmarks/round2-baseline-ui.json --output "benchmarks/desktop-round2-matched-before-$size.json"
    & $probePython -m benchmarks.desktop_responsiveness --tasks $size --fixed-work --output "benchmarks/desktop-round2-matched-after-$size.json"
}
```

`round2-baseline-ui.json` contains only the two pre-change UI files from the initial
checkout. Their exact byte hashes were checked against the first measured source
manifest. `--baseline-ui` loads them inside the benchmark process and restores
module classes on exit; it never rewrites the checkout. It retains all earlier
responsiveness fixes. It is distinct from the older `--reference-behavior` option,
which is not used for this comparison. No prototype attachment is involved.

Both modes use the same probe, current remaining source, temporary SQLite fixture,
UTC date 2026-09-21, and 10 or 200 synthetic 15-minute tasks with priorities 1–10.
Final fixtures use deterministic task UUIDs as well as identical task contents.
Each delivers 80 position changes, 24 size changes and all seven page switches.
Other phases run for eight seconds, waiting for workers and card batches to finish.
Runs are sequential, without concurrent tests. Results include raw samples, source
and probe hashes, effective reference hashes, callback timings, tree event counts,
explicit geometry-call counts, positions/sizes and display scaling. No personal
data, callback arguments, SQL, or credentials are logged.

Environment: Windows 10, Tcl/Tk 8.6.12, 1920×1080 primary display; two monitors
reported. CTk window scaling was 1.0, and Tk scaling 1.3333 (96 physical pixels per
inch / 72 points per inch). No DPI setting was changed as a proposed fix.

## Physical movement: not verified

The Windows computer-use skill initialized and located the disposable app, but
the capture/activation call timed out waiting for app approval before any drag.
No physical title-bar or border drag, one-monitor observation, or cross-monitor
comparison was completed. A still screenshot would not establish smooth motion
anyway. Automated `geometry()` movement is not Windows' interactive move/size loop.

Run the app and minimal control separately:

```powershell
& $probePython -m benchmarks.desktop_responsiveness --tasks 200 --manual --output benchmarks/manual-app.json
& $probePython -m benchmarks.desktop_responsiveness --minimal --manual --output benchmarks/manual-minimal.json
```

1. Press F6, then physically drag the title bar back and forth for 20 seconds on
   one monitor. Note shaking and pauses. Repeat across monitors, noting each
   monitor's Windows scaling setting.
2. Press F7, then drag a border/corner for 20 seconds. Keep this separate from movement.
3. Press F8 for 10 seconds of idle, then close the window to save. Repeat with the
   minimal CTk window at comparable size/position. Record your visual observations
   alongside the JSON. Recording also saves automatically after 180 seconds.

F6/F7 label phases; they do not prove a physical drag happened, so the JSON keeps
`physical_drag_performed: false`. Confirm actual human actions in the observation
notes. Per-sample window position, size and CTk/Tk scaling, root event coordinates,
callbacks, CPU, event-loop lateness and explicit geometry calls are recorded.

## Remaining limits

The available-task widgets explain much of the large-list resize cost; the normal
page still contains many CTk controls that redraw during size changes. Card creation
is now incremental and offscreen cards no longer participate in ordinary resize
layout, but native layout/redraw and CPU/GIL competition with generation
can still delay Tk. These changes are not a claim of smooth physical dragging.
Real remote sync payloads, live PostgreSQL latency and cross-monitor DPI transitions
were not benchmarked. Existing UI/sync tests cover their application safeguards.

## Final matched results

Each cell shows before → after. Delays are lateness of a 50 ms Tk heartbeat,
not total operation duration. CPU is the unweighted mean of process CPU samples
at those heartbeats (100% = one core). Configure counts are root / descendant.

| Tasks | Action | p95 ms | Worst ms | CPU % | Configure root / descendant |
|---:|---|---:|---:|---:|---|
| 10 | move | 8.3 → 8.6 | 12.7 → 13.9 | 4.3 → 4.9 | 160 / 0 → 160 / 0 |
| 10 | resize | 198.8 → 193.9 | 211.2 → 194.6 | 95.5 → 93.9 | 48 / 2340 → 48 / 2340 |
| 10 | pages | 158.9 → 159.0 | 881.4 → 889.5 | 43.5 → 45.2 | 0 / 4661 → 0 / 4661 |
| 10 | generate | 14.2 → 14.1 | 160.3 → 123.5 | 6.1 → 6.4 | 0 / 231 → 0 / 231 |
| 200 | move | 7.8 → 7.9 | 12.7 → 12.3 | 9.8 → 9.0 | 160 / 0 → 160 / 0 |
| 200 | resize | 557.8 → 205.6 | 560.4 → 206.8 | 95.5 → 94.3 | 48 / 9369 → 48 / 2488 |
| 200 | pages | 455.7 → 166.3 | 879.4 → 883.1 | 47.3 → 44.7 | 0 / 11311 → 0 / 4801 |
| 200 | generate | 369.9 → 14.0 | 1604.0 → 373.1 | 22.1 → 27.3 | 0 / 2044 → 0 / 238 |

A separate matched 200-task run generated the schedule first, then delivered
24 resizes with the populated board: p95 **922.9 → 294.9 ms**, worst
**1857.6 → 299.2 ms**, CPU **97.5 → 97.9%**, and Configure events
**48 / 11134 → 48 / 2521**. This establishes the benefit of removing offscreen
cards from active layout independently of the initially empty-board resize.

The large fixture improves resize p95 by 63%, page p95 by 64%, and generation
worst delay by 77%. The small fixture is mostly unchanged. Its movement p95/worst
and page worst are slightly higher; the 200-task page worst also rises 3.7 ms.
These single-run differences do not establish a regression or a movement benefit.
The unchanged small-fixture Configure counts support the intentionally limited
scope. Page worst remains approximately 0.88 seconds and is unresolved.

Generation sampled CPU rises from 22.1% to 27.3%. Incremental construction and
viewport management add callbacks, and yielding exposes more CPU samples during
work; a long blocking callback produces only one delayed heartbeat sample. This
metric is not time-weighted CPU utilization or total CPU seconds, so it must not
be interpreted as an energy comparison. Small CPU increases elsewhere are also
reported rather than omitted. Resize still saturates roughly one core.

The automated phases recorded exactly 80 explicit geometry setters for movement
and 24 for resize, with none during page changes or generation. Movement produced
160 root events and zero descendant events in both versions. There is no evidence
of an application geometry feedback loop in these runs; this does not rule out
Windows interactive move/size or DPI behavior during real dragging.

All six final result files report unchanged source during measurement and the same
probe hash. Effective baseline hashes match the initial current-checkout manifest;
effective after hashes match the final production files, including timer cleanup.

Generated raw JSON and binary profiles were removed after summarizing the results above. Future telemetry outputs are ignored by Git. The small `round2-baseline-ui.json` source fixture remains for the comparison test and reproduction commands. Raw-sample reanalysis requires rerunning the benchmark.

## Verification

- Broad UI, sync, and desktop-direct suite: **408 passed**, one existing
  Starlette/httpx deprecation warning.
- After the final timer-cancellation correction: **16 passed** across bounded
  rendering, desktop shell, desktop app, and diagnostics tests. The broad suite
  was run before that final correction; the targeted suite verifies its behavior.
- Ruff: **passed** across the repository, excluding the isolated `.diagnostic-deps`.
- `compileall app backend benchmarks config tests`: **passed**.
- `git diff --check`: **passed**.

New native-Tk tests exercise bounded widgets, scrolling to all tasks, keyboard
reachability, latest record versions in callbacks, variable-height cards, narrow
layouts, 1.25 widget scaling, card identity reuse, replacement of pending renders,
and cancellation of timers on destruction. Existing account and sync safeguards
remain covered by the broader suite. No unrelated staged changes were reset.
