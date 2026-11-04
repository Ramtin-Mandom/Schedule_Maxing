# Desktop responsiveness investigation (Prompt 1)

This is the historical Prompt 1 audit, completed before implementing fixes. Later
external edits replaced parts of the UI. The final, current-tree comparison is in
[current validation](desktop-responsiveness-current-validation.md); the original raw
baseline is retained here rather than relabeled as a measurement of different code.

The physical Windows shaking diagnosis is provisional. Native Tk windows and scripted
geometry changes are measurable on this host, but no human title-bar/border drag has
been performed. Automated geometry is not Windows' interactive move/size modal loop.

## Source and environment

- Branch: `SMV2-M5.5`, HEAD `2aa927b5a97e37d576ec1fe0e5033773f71e8b32`.
- Audited the current dirty working tree, including its existing Day, Calendar, clock,
  account and execution changes; no attached historical source was used.
- Windows 10 build 19045 (bundled runtime report), 1920x1080 display. Measured with bundled Python 3.12.14 and
  CustomTkinter 5.2.2 (the version present in the repository's original environment).
- Default Python is 3.8.2; `.venv` refers to a missing Python 3.10 installation.
  Isolated dependencies were installed in ignored `.diagnostic-deps`. No application
  dependency requirements were changed. This runtime difference limits comparison
  with the user's usual app launch.

## Tk-thread trace

1. `ScheduleOptimizerApp.__init__`: UI-settings JSON read, Tk creation, initial
   geometry/minimum size, SQLite migrations and controller construction, eager creation
   of all workspace pages. Day/Calendar constructors load local snapshots and editor
   options. Startup precedes normal `mainloop`; startup wall time is reported separately.
2. Idle: `DesktopCollection.collect` performs a full `gc.collect()` every two seconds.
   Automatic collection is disabled to prevent Tk resource finalizers running on workers.
   `_poll_status` calls `refresh_status` every four seconds. That calls
   `AccountController.connection` → `SyncService.status` → SyncStore reads, including
   pending/conflict enumeration when an account exists. CTk also runs appearance/DPI timers.
3. Move/resize: root geometry is set at startup, not by the application's resize
   callbacks. Host `<Configure>` → `Coalescer` → `AppShell._check_layout` only changes
   the responsive mode across hysteresis thresholds. DayTimeline does not redraw on
   resize. CalendarView checks width after 80 ms and requests one coalesced repaint.
   CTk child dimension callbacks still redraw controls per changed dimension. In
   CustomTkinter 5.2.2, `CTkScrollbar._draw` and `CTkOptionMenu._draw` call their canvas's
   `update_idletasks()`, draining nested geometry/redraw callbacks before returning.
   Inclusive callback times can overlap; they must not be added as exclusive CPU costs.
4. Pages: shell `show_page` invokes `on_show`. Day/Calendar reloads go through
   `TaskFormActions._io` → `run_io(background=False)` locally, executing storage and
   snapshot computation synchronously; direct PostgreSQL uses workers. Calendar snapshots
   load the base period plus the calendar grid, freshness, missing tasks, and preferences.
   Day rendering rebuilds available-task buttons and editor options. Projects/Allocation
   and Productivity use background operations, followed by Tk rendering.
5. Sync: `sync_now` runs account sync in a worker; completion/status refresh is on Tk.
   SyncService's periodic wait runs in its own thread. Result polling is 15 ms **per
   pending job**, not a permanent idle poll. Registry counts jobs, but does not impose a
   capacity limit. Widget existence, workspace epoch and page load tokens reject stale
   deliveries. Slow synchronous SQLite calls can wait on the shared connection RLock.
6. Generation: Day `_start_run` captures the date and marks busy, then runs the
   controller/workflow in a worker. `generate_from` computes before a transaction that
   rechecks fingerprints and commits. Python CPU work can still compete for the GIL;
   a thread alone is not proof of responsiveness. No optimizer semantics were changed.
7. Shutdown: `_on_close` initially allows a 10-second service close. Sync thread join,
   registry wait and DB lock acquisition can block; retries use `after(100)` only after
   the initial wait fails. Resource finalization is deliberately on Tk's thread.

Searches found no production `sleep()` loop. Modal dialog `wait_window` uses Tk's nested
event loop; it is distinct from worker/lock waits. Appearance/settings writes and CSV
operations also warrant separate I/O profiling. Guide scrolling explicitly flushes idle
work once; it is not a recurring resize hook.

## Reproduction

With a supported Python environment containing the repository's dependencies:

```powershell
python -m benchmarks.desktop_responsiveness --tasks 10 --output benchmarks/desktop-baseline-small.json
python -m benchmarks.desktop_responsiveness --tasks 200 --output benchmarks/desktop-baseline-large.json
python -m benchmarks.desktop_responsiveness --tasks 200 --manual --output benchmarks/desktop-manual.json
```

On this host use the bundled Python executable returned by `load_workspace_dependencies`
and set `$env:PYTHONPATH="$PWD/.diagnostic-deps;$PWD"`. The probe needs permission to read
those isolated packages. Run samples sequentially, without tests running concurrently.

Exact host invocation (from the repository root):

```powershell
$probePython = 'C:\Users\Ramtin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = "$PWD/.diagnostic-deps;$PWD"
& $probePython -m benchmarks.desktop_responsiveness --tasks 10 --output benchmarks/desktop-baseline-small.json
& $probePython -m benchmarks.desktop_responsiveness --tasks 200 --output benchmarks/desktop-baseline-large.json
```

Use `desktop-after-small.json` / `desktop-after-large.json` for subsequent runs so the
baseline remains intact. This directory has the isolated dependencies; a fresh machine
needs a supported Python with `requirements.txt` and CustomTkinter 5.2.2 for comparison.

Fixtures: fresh temporary SQLite DB and UI settings; UTC, 2026-09-21; 10 or 200 synthetic
15-minute tasks, priorities cycling 1–10, preferred date 2026-09-21; no dependencies or
fixed blocks. Real data and backend configuration are never used. Eight-second phases:
idle, geometry movement at 100 ms, geometry resize at 100 ms, cycling through seven pages as time permits,
Day generation, unconfigured sync, synthetic empty-account sync with a two-second
transport delay, idle after. The generation phase waits for workers to finish.

The manual command leaves the window open. For at least 20 seconds each, drag the
title bar, resize each edge/corner, cross responsive widths, maximize/restore, then
repeat during page loading and generation. Repeat at the user's DPI/monitor setup.
Record visible shaking and mark physical verification separately; the probe does not
automatically assert that a physical drag happened.

Instrumentation is opt-in through this command only: callback durations, 50 ms heartbeat
lateness, process CPU (100% = one core), configure counts, Coalescer callbacks, worker
count and pending timer/poll snapshots. No SQL, callback arguments, task contents or
credentials are recorded. Samples and per-callback raw durations are retained in JSON.
Timers miss sub-50-ms worker lifetimes; callback instrumentation adds two clock reads
and list appends, and nested callbacks have inclusive duration. No hard real-time claim.

## Evidence and next changes

Initial runs demonstrate long resize stalls coinciding with nested CTk idle flushing,
despite no DayTimeline repaint and coalesced application layout. This is stronger
evidence than attributing shaking to the 15 ms worker poll. Full heap collections also
cause recurring idle pauses. Large page rendering remains a separate workload.

Concrete Prompt 2 targets, subject to the final baseline below:

- Introduce a narrowly scoped app-owned CTk drawing adapter for scroll frames and option
  menus; prevent their internal paint canvases from recursively draining Tk idle work.
  Keep explicit root `update_idletasks` available and all drawing on Tk's thread.
- `DesktopCollection.collect`: replace unconditional full-heap scans with allocation-
  driven generational collection on the owner thread; preserve restoration/finalization.
- Re-measure `DaySchedulePage._render/_show_available`, `TaskFormActions._io`,
  `ScheduleOptimizerApp.refresh_status` before selecting further changes. Do not rewrite
  the scheduler or introduce processes without evidence they address the observed stalls.
- Keep `run_in_background`, `Coalescer`, and optimizer behavior unchanged unless further
  evidence shows them responsible; their mere presence is not a measured bottleneck.

Live remote sync, network PostgreSQL latency and the physical Windows move/size loop
remain unmeasured. Synthetic latency tests the client waiting path, not remote apply of
large data sets. Raw final baseline values and verification are recorded below.

## Final baseline

Milliseconds, nearest-rank p95; CPU is the mean of process samples, with 100% representing
one core. Configure counts include descendant events; layout counts are Coalescer runs.
Poll snapshots include the four-second status poll (one at idle), not just worker polls.
Raw data: `benchmarks/desktop-baseline-small.json` and `desktop-baseline-large.json`.

| Tasks | Phase | Delay p95 / worst ms | Mean CPU % | Configure root / all | Coalescer runs | Peak workers / polls |
|---|---|---|---|---|---|---|
| 10 | idle | 16.7 / 53.3 | 4.1 | 0 / 0 | 0 | 0 / 1 |
| 10 | move | 16.7 / 64.7 | 6.5 | 156 / 156 | 0 | 0 / 1 |
| 10 | resize | 440.2 / 488.5 | 91.0 | 78 / 2299 | 38 | 0 / 1 |
| 10 | pages | 106.5 / 2056.6 | 31.0 | 0 / 2993 | 8 | 0 / 4 |
| 10 | generate | 18.5 / 217.6 | 6.0 | 0 / 89 | 2 | 1 / 3 |
| 10 | sync, empty account + latency | 16.7 / 60.9 | 1.8 | 0 / 4 | 0 | 1 / 2 |
| 200 | idle | 34.7 / 630.0 | 9.0 | 2 / 819 | 1 | 0 / 1 |
| 200 | move | 18.6 / 60.6 | 7.6 | 154 / 154 | 0 | 0 / 1 |
| 200 | resize | 1002.2 / 1506.9 | 90.3 | 46 / 4628 | 20 | 0 / 1 |
| 200 | pages | 491.1 / 1639.4 | 44.1 | 0 / 1597 | 10 | 0 / 1 |
| 200 | generate | 534.7 / 2306.2 | 20.5 | 0 / 995 | 2 | 1 / 3 |
| 200 | sync, empty account + latency | 13.1 / 73.1 | 2.8 | 0 / 5 | 0 | 1 / 2 |

Constructor wall times: 4076 / 13445 ms. Initial large-fixture idle still includes
startup settling; it is not a clean steady-state idle sample. Idle-after worst delays
were 49.9 / 81.8 ms. Brief verification commands overlapped the end of the large
idle-after phase, so do not use that phase as a precise CPU comparison.

Measured full-GC worst callbacks were 82.8 ms (small page phase) and 98.8 ms (large
sync phase); even small idle collected four times with a worst 58.5 ms. Small sync
worker polling ran 104 callbacks, worst 2.6 ms including delivery. Nested idle-flush
timings reached hundreds of milliseconds during resize and seconds during page
changes. Thus the measured priority is nested redraw/geometry processing, followed
by unconditional GC. The total delay during generation includes foreground return
rendering; it does not establish that the optimizer/GIL is the dominant cause.

Prompt 1 verification: 27 tests passed (diagnostic privacy/restoration, shell foundation,
AppServices); repository Ruff passed with the isolated dependency directory excluded;
compilation of app/backend/benchmarks/config/tests passed. Production behavior was not
changed during this milestone.
