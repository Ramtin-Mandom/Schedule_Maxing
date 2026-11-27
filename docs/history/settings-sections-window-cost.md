# Settings sections and bounded detached pages: window-move cost

Measurements for [ADR-053](../DECISIONS.md#adr-053--at-most-three-detached-pages-settings-sections-are-pages-of-their-own),
taken on 2026-11-26 (Windows 10 build 19045, `.venv` Python 3.10.11, 50 synthetic tasks, 1400x880 window on
the left monitor).

## Method

`python -m benchmarks.window_move_cost --baseline <tree>` runs every scenario in a fresh process with a
disposable database. The child opens the app and runs the scenario's steps: it shows pages, opens Settings
sections, or turns light caching on. It waits until the window is quiet and counts the native child windows
of the main window. Then it measures two 8-second phases: the window standing still, and the window moved
with `SetWindowPos` every 16 ms. It reads `\Process(dwm)\% Processor Time` (percent of one core) through
PDH, and the app's own process CPU. The baseline is a `git worktree` of `aed5625` (before). Before and after
alternate, in reversed order every other round, so background load spreads over both. The table gives
medians of three rounds. Physical dragging with a mouse was not performed.

Window counts are deterministic. dwm.exe numbers depend on everything else on the machine. An earlier
attempt in this session, run while other work was rendering, read up to 45% with the window standing still,
so its numbers were discarded.

## Results

| Scenario | Native windows before | after | dwm moving before | after | dwm added by moving before | after | app CPU moving before | after |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Day only | 421 | 421 | 9.6% | 10.5% | 7.2% | 5.8% | 3.3% | 3.3% |
| Settings (opens on General) | 1,253 | **501** | 19.5% | **8.0%** | 15.2% | **6.4%** | 7.8% | **3.7%** |
| Settings, Scheduling open | 1,253 | 1,219 | 16.8% | 16.1% | 15.0% | 11.7% | 8.2% | 6.8% |
| Settings, Scheduling then Tasks and data open | 1,253 | 1,229 | 18.5% | 17.8% | 16.9% | 15.8% | 8.3% | 7.4% |
| Account, How to Use, then Settings | 1,530 | **778** | 19.7% | **12.5%** | 18.5% | **10.9%** | 7.4% | 6.4% |
| Light caching, every tab and section, back on General | 1,257 | **519** | 16.8% | **11.9%** | 16.8% | **11.9%** | 8.0% | 5.7% |

On the old tree, "section" steps did nothing: Settings held every section at once.

Native windows per part, counted before the change: Settings ~824, of which the Default scheduling
preferences card was 715, How to Use ~277, Account ~106, Day ~421.

## Reading

- Opening Settings costs about as much as Day alone. It held three times Day's windows before.
- While Scheduling is open, the preferences editor is attached and the cost is unchanged. It is also kept
  for 30 s after another section is opened, as the section left last. Building only the rows in view would
  be the next step (ROADMAP R6).
- The bound on detached pages does not change the moving cost: withdrawn windows cost little while moving
  (round 3b). It bounds memory and handles in a long session.
