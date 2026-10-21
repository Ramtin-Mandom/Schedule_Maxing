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
- Not done: replacing CTk buttons in dense lists with lighter widgets (their cost is
  CustomTkinter's rounded drawing; swapping them changes look and keyboard behaviour), and
  idle pre-building of the Week page (a page build is one blocking block of 0.1-0.4 s).
- Not justified: a separate process for schedule generation. Lateness during generation is
  p95 25-40 ms; its worst stalls come from rendering the result, not from GIL contention.

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
