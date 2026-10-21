# Desktop performance, round 3: start-up, idle work and responsiveness

Round 3 targets start-up time, CPU use while the app is not in use, and UI
responsiveness, without changing any scheduling, storage or sync behaviour. It
builds on [round 2](desktop-responsiveness-round2.md) and the
[current-tree validation](desktop-responsiveness-current-validation.md); their
guards (stale-result and record-version checks, Tk-thread-only widget access,
workspace guards, shutdown ordering) are unchanged.

## How it was measured

`benchmarks/desktop_responsiveness.py` gained, before any change:

- `import_ms` (import of `app.app`, warm) and `heavy_modules_after_import`;
- `first_paint_ms`: from the constructor's start until the window is viewable and the idle
  handlers queued before it (map, geometry, redraw) have run;
- `interactive_ms`: viewable, the Day page rendered its snapshot and its board, and no worker running;
- `--minimized-seconds N`: an iconified phase, with whole-phase process CPU seconds and the
  status polls counted;
- a `pages_revisit` phase: the same seven page switches again, every page already built;
- window placement on the monitor left of the primary one (`tests/window_placement.py`).

Each column below is one run of `--fixed-work --minimized-seconds 60` with the final
harness, from a git worktree at that phase's commit (Baseline = `257d0e8`, the harness only;
Phase 1 `bc2eb58`, Phase 2 `3c46092`, Phase 4 `0cf8f6f`, Phase 5 `bec7ad0`; Phase 3 changes
only the HTTP transport, which the probe's synthetic sync does not use). Windows 10, Python
3.10.11 (`.venv`), Tcl/Tk 8.6.12, CustomTkinter 5.2.2, temporary SQLite fixture with 10 or 200
synthetic tasks. Delays are lateness of a 50 ms Tk heartbeat; CPU % is the mean of
heartbeat samples (100% = one core). Single runs: differences of a few tens of
milliseconds are noise.

## Results

### 10 tasks

| Measure | Baseline | Phase 1 | Phase 2 | Phase 4 | Phase 5 |
|---|---:|---:|---:|---:|---:|
| `import app.app` ms | 1931 | 669 | 653 | 730 | 633 |
| Constructor ms | 3823 | 674 | 691 | 1096 | 655 |
| First paint ms | 5451 | 1228 | 1256 | 1632 | 1165 |
| Interactive ms | 5450 | 1226 | 1253 | 1630 | 1163 |
| resize p95 / worst ms | 327 / 328 | 329 / 346 | 301 / 311 | 446 / 488 | 86 / 95 |
| pages p95 / worst ms | 270 / 987 | 322 / 987 | 345 / 963 | 285 / 1069 | 110 / 348 |
| pages_revisit p95 / worst ms | 30 / 80 | 29 / 79 | 33 / 85 | 25 / 49 | 25 / 49 |
| generate p95 / worst ms | 15 / 185 | 17 / 179 | 17 / 93 | 27 / 81 | 25 / 79 |
| resize CPU % | 83.7 | 89.2 | 90.5 | 92.8 | 96.0 |
| pages CPU % | 43.2 | 61.5 | 61.0 | 55.2 | 55.0 |
| idle_after CPU % | 0.4 | 0.2 | 0.0 | 0.0 | 0.0 |
| resize Configure events (all) | 2257 | 2259 | 2259 | 2257 | 1057 |
| minimized 60 s: CPU s / status polls | 0.125 / 15 | 0.109 / 15 | 0.062 / 0 | 0.016 / 0 | 0.031 / 0 |

### 200 tasks

| Measure | Baseline | Phase 1 | Phase 2 | Phase 4 | Phase 5 |
|---|---:|---:|---:|---:|---:|
| `import app.app` ms | 2120 | 736 | 674 | 642 | 652 |
| Constructor ms | 3967 | 681 | 705 | 655 | 650 |
| First paint ms | 5828 | 1386 | 1418 | 1287 | 1284 |
| Interactive ms | 5826 | 1384 | 1416 | 1285 | 1282 |
| resize p95 / worst ms | 460 / 523 | 330 / 340 | 333 / 365 | 230 / 233 | 96 / 111 |
| pages p95 / worst ms | 359 / 986 | 312 / 967 | 335 / 971 | 225 / 899 | 109 / 346 |
| pages_revisit p95 / worst ms | 27 / 83 | 27 / 81 | 40 / 111 | 26 / 63 | 18 / 47 |
| generate p95 / worst ms | 55 / 474 | 53 / 473 | 27 / 543 | 32 / 254 | 40 / 234 |
| resize CPU % | 89.2 | 89.1 | 90.2 | 95.4 | 95.6 |
| pages CPU % | 47.9 | 64.1 | 63.1 | 57.7 | 56.9 |
| idle_after CPU % | 0.2 | 0.2 | 0.2 | 0.0 | 0.0 |
| resize Configure events (all) | 2747 | 2921 | 2921 | 2923 | 1216 |
| minimized 60 s: CPU s / status polls | 0.094 / 15 | 0.094 / 15 | 0.062 / 0 | 0.016 / 0 | 0.016 / 0 |

The minimized CPU includes the probe's own 20 Hz heartbeat.

### Regressions and caveats, stated plainly

- **First page visits got slower in Phases 1-2 at 10 tasks** (p95 270 -> 322-345 ms), because
  building a page moved from start-up to its first show. Phase 5 more than recovers it (110 ms),
  mainly by building the Settings preference rows in slices (the round-2 "page worst ~0.9 s").
- **Phase 4, 10 tasks** shows a slower constructor (1096 ms) and resize (446 ms) than the
  phases around it; the same commit's 200-task run does not (655 ms, 230 ms), and Phase 5 on
  top of it is at 655 ms. Treated as a run outlier, not re-measured.
- **Resize CPU % rose** (84-89% -> 96%). The probe delivers 24 size steps 100 ms apart; with
  the page size now applied 120 ms after the last step, Tk handles the window frame
  continuously and the page is laid out once at the end. Lateness, not CPU, is the
  measure of jank; the sampled CPU is not time-weighted.
- **Interactive dragging** is still unverified physically (as in round 2): `geometry()` steps
  are not Windows' interactive move/size loop.
- **After a resize the page shows its old size for 120 ms** (clipped or with an uncovered
  margin) before it is laid out at the new one.

## What changed, by phase

### Phase 1 -- start-up (`bc2eb58`)

- scikit-learn, scipy, pandas and joblib are imported inside the functions that train, save
  or load a model (`ml_evaluation`, `ml_persistence`, `ml_prediction`); none is loaded by
  `import app.app` (`tests/test_startup_cost.py`, run in a subprocess).
- Only the Day page is built at start-up. Every other page is a `DeferredPage`
  (`app/ui/pages.py`) built, with its module imported, the first time it is shown; it passes
  attributes and assignments through to the built page, applies the layout mode given before
  the build, and reports `built`, which `app.py` checks before refreshing a page nobody opened
  (appearance switch, task-data reset). The status bar's Account button, the group bar,
  Ctrl+number and conflict handling all build the Account page on demand.
- Scrollbars of scrollable frames and text boxes no longer flush Tk's idle queue while they
  are constructed (`paint_widgets.AppScrollbar`): 18 such flushes cost 1.7 s at start-up.
- The OS credential store (keyring discovery, ~0.3 s) is a `LazyVault`: found on first use,
  or by a background prefetch 1.5 s after start; the first background sync waits 3 s.
- Not changed: the PyInstaller spec. A onedir build pays at start-up only for what is
  imported (now lazily); excluding scientific submodules could not be verified without a
  Windows build and bundle check.

### Phase 2 -- idle work (`3c46092`)

- `app/ui/activity.py`: focused / unfocused / minimized from the root's focus and map events.
  Status poll 4 s / 15 s / paused (one read on restore); GC timer 2 s / 2 s / 30 s;
  `SyncService.set_activity`: 60 s / 300 s / 300 s, no pending-count looks while minimized,
  a sync on regaining focus when the last success is older than 60 s, a local edit pushed
  2.5 s after the last commit (ADR-041).
- CustomTkinter 5.2.2 trackers: appearance polled every 1000 ms (was 30), DPI every 500 ms
  (was 100); pinned by `tests/ui/test_activity.py`.
- `StatusBar.show` reconfigures only what changed.

### Phase 3 -- network (`ae07feb`)

- `HttpTransport` keeps one `http.client` keep-alive connection: one sync's capability,
  push and pull requests share one TLS handshake. Verification, redirect refusal, timeouts
  and the wake probe are unchanged; a dropped kept connection is retried once for idempotent
  requests only, never a POST (`tests/test_transport_keepalive.py`).
- Already in place and kept: capabilities cached per token, `/health` only when the server
  may be asleep, a page reload only when a pull applied changes.

### Phase 4 -- redundant reloads (`0cf8f6f`)

- `AppServices.data_version()`: the connection's count of commits that wrote rows plus
  sqlite3's `total_changes`, and the workspace epoch. A transaction that writes nothing (some
  loads read inside one) does not count.
- Day, Week/Month, Projects and Productivity skip `on_show`'s re-read and re-render when
  the key they read with (data version + date/period/selection + today + task-form
  categories) is unchanged (`app/ui/page_freshness.py`); `reload()` always re-reads.
- Productivity keeps 16 recent reports by (data version, section, range, filters).
- `components.font()` shares one CTkFont per (size, weight) per Tk interpreter, with O(1)
  callback removal.
- Not done: a month-grid layout cache. With the re-read skipped, a revisited month is not
  re-rendered at all, and navigation computes its model in a worker.

### Phase 5 -- main-thread rendering (`bec7ad0`)

- The shown page is placed in the shell's page area at a fixed size, applied once the area
  kept its size for 120 ms (`Coalescer(..., debounce=True)`): a resize or the sidebar
  animation lays the page out once instead of on every step.
- `PreferencesEditor` builds its rows in 8 ms idle slices (the Settings page and the Day
  preferences dialog) instead of one ~1.2 s callback; `finish()` completes it before typed
  values are restored.
- Deferred at first, done in Round 3b below: lighter widgets for the long lists, idle
  pre-building of the Week page (in parts), and the day engine in a separate process.

### Phase 6 -- SQLite (no change; ADR-042)

On the app's real paths (120 task saves, 20 Day loads, one generation, 50 status reads):

| Setting | Save median | Day load median | Generation | Status read |
|---|---:|---:|---:|---:|
| Default (rollback journal, FULL) | 3.24 ms | 13.7 ms | 621 ms | 0.05 ms |
| WAL + synchronous=NORMAL | 0.95 ms | 13.2 ms | 605 ms | 0.01 ms |
| Default + temp_store/cache_size/busy_timeout | 3.29 ms | 15.6 ms | 626 ms | 0.05 ms |
| WAL + NORMAL + the three | 0.92 ms | 12.8 ms | 633 ms | 0.01 ms |

WAL makes commits about 3x faster, but every write already runs off the Tk thread, and WAL
would add `-wal`/`-shm` files, make the documented "copy a backup over executions.db"
restore unsafe while a stale `-wal` exists, and make a file copy of a running database miss
recent commits. EXPLAIN QUERY PLAN of the Day/Week snapshot and status reads showed scans
only of the workspace's own tasks (which those reads need in full) and one executions count,
so no index was added.

## Verification

Focused tests per phase (new: `tests/test_startup_cost.py`, `tests/ui/test_deferred_pages.py`,
`tests/ui/test_activity.py`, `tests/ui/test_activity_window.py`, `tests/test_sync_activity.py`,
`tests/test_transport_keepalive.py`, `tests/ui/test_page_freshness.py`,
`tests/ui/test_page_freshness_keys.py`, additions to `tests/execution/test_transactions.py`
and `tests/ui/test_shell_foundation.py`), the affected real-window modules, and the existing
sync idle-loop, keep-signed-in and HTTP transport tests; results in CURRENT_STATE.md section D.

## Round 3b: dragging, light widgets, Week pre-build, engine process

Follow-up after the user reported that dragging the window was better but still raised
`dwm.exe` (the Windows compositor) from about 1% to 14% of total CPU (12 logical cores).

### Why dragging costs the compositor

A probe (scratch script, not committed) moved a 1440x880 window with `SetWindowPos` every 16 ms
for 8 s on the left monitor and read `\Process(dwm)\% Processor Time` (percent of ONE core)
through PDH. Measured with low background load:

| Window | Native child windows | dwm.exe while moving |
|---|---:|---:|
| empty CustomTkinter window | 3 | 7-10% |
| the app, Day page, 200 tasks (round 3 end) | 556 | 22-24% |
| 150 CustomTkinter buttons | 453 | 22% |
| 400 plain Tk buttons | 403 | 20% |
| 400 CustomTkinter buttons | 1203 | 35% |
| the same 400 items drawn on one Canvas | 4 | 5% |

The cost follows the number of native child windows, not what the app draws (the app itself
used ~10% of a core). Tk gives every widget one; a CustomTkinter widget is two or three
(frame, drawing canvas, often an inner label). Later measurements (with heavy background
compositor load on the machine, so as paired still/moving medians) showed that **hidden** child
windows cost as much as visible ones: 400 hidden CTk buttons 43-51% while moving, visible 41-48%,
the same widgets in a withdrawn window of their own 18-23% (empty window 18-24%). So every page
built during a session -- and the pre-built Week page -- made dragging heavier.
`WS_EX_COMPOSITED` on the window was tried and rejected: Tk repaints endlessly with it.

### What changed

- **Light widgets** (`app/ui/light_widgets.py`, ADR-043): one native window each, themed through
  CustomTkinter's trackers. Used for the available-task chips, the status-board cards (16 -> 7
  windows each), `SectionTitle`, field captions/hints/errors and `Notice` text. Day page with 200
  tasks: 556 -> 435 child windows; dwm.exe while moving ~23% -> ~16% of one core (single runs).
  Look: list buttons have square corners; ghost buttons lost their outline.
- **Hidden pages detached** (`AppShell._hide`/`_detach`): a hidden page that stayed hidden for
  30 s, with no input for 2 s and no idle callback pending, moves into a withdrawn window of its
  own (`wm manage`); showing it makes it a child again (`wm forget`). After visiting five pages
  the window holds ~450 child windows instead of ~1,700. Two pitfalls found and handled:
  re-attaching costs a full re-map and redraw (0.2-0.7 s, about a first visit), so pages are not
  detached while the user moves between them; and `wm manage`/`wm withdraw` run every pending idle
  callback at once (a 1.8 s block in a synthetic test, 1.3-3.2 s in the probe when Settings was
  building its rows), so a detach waits until no idle callback is pending.
- **Week pre-built in idle time** (`DeferredPage.prebuild_step`, `CalendarPage(staged=True)`):
  one part per idle callback (header, day-window bar, calendar, details, task form, load) while
  the Day page has rendered, no worker runs, the window is not minimized and there was no input
  for 0.8 s; then parked (detached). Gives up after about a minute without such a moment.
- **Day engine in a separate process** (`app/planning/engine_process.py`, ADR-044): only the pure
  engine call; probe heartbeat lateness during a 200-task generation p95 26-41 -> 15 ms (two runs
  each); the worst stall (rendering the result) is unchanged.

### Probe results

Same harness as above; one run per column. The 1612f65 column is before hidden pages were
detached; its page switches are the fastest, but every visited page stayed in the window.

#### 10 tasks

| Measure | Round 3 end (bec7ad0) | + light widgets, Week pre-build, engine process (1612f65) | + hidden pages detached (bdc67f6) |
|---|---:|---:|---:|
| `import app.app` ms | 633 | 532 | 563 |
| Constructor ms | 655 | 646 | 651 |
| First paint ms | 1165 | 1044 | 1094 |
| Interactive ms | 1163 | 1043 | 1093 |
| resize p95 / worst ms | 86 / 95 | 97 / 103 | 100 / 120 |
| pages p95 / worst ms | 110 / 348 | 99 / 302 | 143 / 343 |
| pages_revisit p95 / worst ms | 25 / 49 | 15 / 29 | 35 / 86 |
| generate p95 / worst ms | 25 / 79 | 14 / 31 | 11 / 52 |
| resize CPU % | 96.0 | 92.6 | 88.1 |
| pages CPU % | 55.0 | 49.4 | 52.9 |
| idle_after CPU % | 0.0 | 0.0 | 0.8 |
| resize Configure (all) | 1057 | 949 | 663 |
| minimized 60 s: CPU s / status polls | 0.031 / 0 | 0.078 / 0 | 0.047 / 0 |

#### 200 tasks

| Measure | Round 3 end (bec7ad0) | + light widgets, Week pre-build, engine process (1612f65) | + hidden pages detached (bdc67f6) |
|---|---:|---:|---:|
| `import app.app` ms | 652 | 549 | 544 |
| Constructor ms | 650 | 654 | 665 |
| First paint ms | 1284 | 1123 | 1181 |
| Interactive ms | 1282 | 1122 | 1179 |
| resize p95 / worst ms | 96 / 111 | 78 / 84 | 109 / 118 |
| pages p95 / worst ms | 109 / 346 | 96 / 319 | 127 / 335 |
| pages_revisit p95 / worst ms | 18 / 47 | 21 / 45 | 27 / 69 |
| generate p95 / worst ms | 40 / 234 | 22 / 124 | 13 / 161 |
| resize CPU % | 95.6 | 89.0 | 87.8 |
| pages CPU % | 56.9 | 52.3 | 55.2 |
| idle_after CPU % | 0.0 | 0.0 | 0.6 |
| resize Configure (all) | 1216 | 1165 | 693 |
| minimized 60 s: CPU s / status polls | 0.016 / 0 | 0.062 / 0 | 0.047 / 0 |

The minimized-phase CPU (0.03-0.08 s per minute, still about 0.1% of a core) varies between
runs; today's runs had much more background load on the machine than yesterday's.

### Not done / limits

- The rest of the visible page is still CustomTkinter: the form's entries and menus, the action
  and navigation buttons (36 `AppButton`s on the Day page, three native windows each). Converting
  them changes the look of the whole app; drawing very long lists on one Canvas would save more
  but loses per-item focus and widget-level tests.
- Physical dragging was not measured with a mouse; the probe's `SetWindowPos` steps are not
  Windows' interactive move loop. The dwm.exe numbers vary strongly with other activity on the
  machine.