# Scheduling modes

Five user-facing modes, chosen per account (user layer) or per date (date
layer) exactly like the earlier engine choice. A mode is an objective over one
shared search, not a separate optimizer: every mode runs the canonical greedy
event-candidate engine (`app/optimizer.py`) under the same hard constraints --
the day window, fixed blocks (breaks included), kept work (history and manual
placements, reserved as busy time), durations, dependencies, deadlines and
required work. Generation mode (FULL/INCREMENTAL) is independent.

| Mode | Stored value | Search | Objective |
| --- | --- | --- | --- |
| Normal | `precise_greedy` | baseline greedy, minute resolution | B(S) |
| ADHD | `adhd_friendly` | baseline greedy; tasks over 30 minutes on the local quarter hour | B(S) + configured short-gap bonus (default 0) |
| Early Finish | `early_finish` | baseline greedy, then bounded repacking | B(S) + W·N·(0.50·E + 0.35·C + 0.15·(1−U)) |
| Night Owl | `night_owl` | baseline greedy, then bounded repacking | B(S) + W·N·(0.50·T + 0.35·C + 0.15·U) |
| Catch-Up | `catch_up` | baseline greedy with history bonuses in the task choice | B(S) + Σ task bonuses |

Saved `precise_greedy`/`adhd_friendly` choices keep their meaning, and
user/date override precedence is unchanged.

## Audited baseline

Before this change the engine read the mode only to pick candidate starts:
`precise_greedy` at every minute; `adhd_friendly` on the local quarter-hour
lattice for tasks over 30 minutes, plus the short-gap bonus when
`short_gap_bonus_weight` > 0 (default 0). A placement's stored score is its
*insertion* score: the reward against the neighbors present when it was
inserted. Normal and ADHD placements, unscheduled reasons and stored scores
are unchanged by this work (the Prompt 1 baseline,
`benchmarks/scheduling_mode_baseline.py --check`, compares them). The one
behavior fix is that minute resolution now applies to every mode except ADHD
(the previous check `mode == precise_greedy` would have put any new mode on
the quarter-hour lattice).

## Formulas

- **B(S)**, the final baseline reward: every flexible placement re-scored with
  `calculate_task_score` against its actual chronological neighbors (fixed
  blocks included), with the date's reward settings
  (`app/optimizer.evaluate_day_output`). It is reported apart from the stored
  insertion scores; nothing stored is rewritten to refresh a report.
- **Metrics** (day-relative minutes; `app/mode_objectives.schedule_metrics`):
  A = 0, Z = window end, H = max(1, Z − A); F/L first start/last finish of the
  flexible placements; N their count; G the unoccupied minutes between F and L
  (fixed blocks and kept work occupied, overlaps counted once);
  E = (Z − L)/H, T = (F − A)/H, C = 1 − G/H, U = mean((startᵢ − A)/H), each
  clamped to [0, 1]. An empty schedule scores 0 on every component.
- **W** = max(1, |weight_importance|) of the date's reward settings (default
  5), fixed for the run -- the unit of one priority step of the baseline
  reward.
- **Ties.** Normal/ADHD/Catch-Up: the engine's existing order (input order on
  equal scores). Early: higher objective, then earlier L, then smaller G, then
  candidate order (the baseline first). Night: higher objective, then later F,
  then smaller G, then candidate order.

## Early Finish and Night Owl

The baseline greedy result fixes the work set: which tasks are placed (with
the required-prerequisite closure) and their durations. Repacking then moves
only those tasks -- left as early as possible (Early) or right as late as
possible (Night) -- around fixed blocks and kept work, respecting
dependencies (including prerequisites satisfied on earlier dates), deadlines
and the window. Candidate orders: every dependency-respecting order for up to
6 tasks (at most 720), otherwise the baseline's chronological order and the
priority order. The baseline placement always competes, so the result is
never worse on the objective; when nothing improves it, the baseline is kept.
Unscheduled work stays reported, never hidden to improve finish time. This is
a bounded heuristic with no global-optimum guarantee; a small-fixture test
compares it with an exhaustive search of every start minute.

## Catch-Up

`app/planning/catch_up.py`. Evidence is one bulk, owner-scoped read
(`PlanningService.schedule_history`) over the 90 days before *as-of* = the
current UTC day's midnight, taken before any search and folded into the inputs
fingerprint (so a saved Catch-Up schedule goes stale when outcomes change, and
a stale save is refused).

- Grouped by the historical category snapshot (the execution's category, else
  the placement's); a missing snapshot is unknown and earns nothing. Current
  categories are never substituted.
- One outcome per immutable occurrence (occurrence key; the latest terminal
  outcome of its lineage). Completed = completion; skipped = miss. Cancelled
  (user, move or regeneration), pending, in-progress, paused, future and
  missing executions are not evidence.
- With m misses, c completions, n = m + c: n < 5 → bonus 0 ("insufficient
  history"); else p = (m + 1)/(n + 4), reliability = n/(n + 5), bonus =
  W·reliability·p ∈ [0, W].
- The bonus is added only where the greedy search picks the next task (a
  task-wide constant cannot change where a task goes). Each new Catch-Up
  placement carries a bounded explanation in its optimization metadata
  (category, lookback, as-of, m/c/n, smoothing, bound, bonus). No history
  means exactly Normal.

## Where it shows

The Day page's engine choice lists all five; a generated date's run message
adds a line for Early Finish, Night Owl and Catch-Up (first start, last end,
idle minutes, baseline reward and mode bonus). `POST /planning/generate`
returns each day's `evaluation`. The REST capabilities list the modes
(`ENGINE_DESCRIPTIONS`).

## Storage and synchronization

Local schema v11 rebuilds `preference_overrides` with the wider mode CHECK
(rows, index and capture triggers unchanged; nothing marked for sync). Server
revision 0012 widens the four mode CHECK constraints; no row changes. Sync
sends a preference layer or schedule record naming a new mode only to a server
advertising `scheduling_modes`; otherwise it waits (counted as held).

## Comparison

`python benchmarks/scheduling_modes_comparison.py` writes
`benchmarks/results/prompt5_scheduling_modes_comparison.json`: runtime, B(S),
stored insertion score, mode component, objective, scheduled/unscheduled counts
and minutes with reasons, first start, last finish, idle gap minutes,
fixed/break minutes and constraint violations, for constrained days,
infeasible required work, preferences, dependencies, sparse history and a
20-task workload. Objectives of different modes are different quantities, not
one quality scale.
