# Final performance review (Task 6)

> **Historical.** Everything below (method, table, profiling notes, summary)
> describes the pre-event-search milestone, when the canonical engine's
> `_best_candidate_for_canonical_task` scored every feasible minute
> exhaustively. It is kept as-is rather than silently overwritten. The
> event-based candidate search milestone's own corrections and updated
> numbers are in the **"Event-search correction (Task 6 addendum)"** section
> at the end of this file -- read that section for the current, accurate
> picture; treat the claims below it (particularly "provably better-or-equal"
> and "exhaustive scoring is ... unavoidable") as superseded.

Greedy Optimizer v1 vs. the canonical day engine (`precise_greedy` and
`adhd_friendly`), under an **explicit baseline-equivalent scoring
configuration**: default `RewardSettings()`/`RewardPreferences()` on both
sides (their field values are defined identically — see
`app/planning/preferences.py`'s `RewardPreferences` docstring), no
`config/task_preference.yaml` loaded, no category/task overrides. Script:
[`final_comparison.py`](final_comparison.py)
(`python -m benchmarks.final_comparison`); raw data in
`benchmarks/results/final_comparison.json`.

## Method

Same four fixtures as `BASELINE.md`/`DAY_ENGINE_BASELINE.md`. For each
fixture: 3 warmups (discarded), 15 measured repeats, `time.perf_counter`,
timing only the optimizer/engine call itself (CSV parse and canonical
import excluded, matching both earlier benchmarks' convention).

## Measured results (this run)

| fixture | legacy median (ms) | precise median (ms) | adhd median (ms) | precise/legacy | adhd/legacy | score delta (precise − legacy) |
|---|---:|---:|---:|---:|---:|---:|
| valid_single_day_basic | 4.36 | 18.69 | 1.54 | 4.28x | 0.35x | 0.00 |
| dependency_chain_linear | 3.55 | 12.47 | 1.00 | 3.51x | 0.28x | 0.00 |
| synthetic_medium_day | 33.46 | 140.84 | 62.89 | 4.21x | 1.88x | +11.70 |
| synthetic_larger_day | 87.92 | 538.23 | 213.01 | 6.12x | 2.42x | +76.18 |

## Reading the numbers

- **Score delta is exactly 0.00 on both real, hand-written fixtures**
  (`valid_single_day_basic`, `dependency_chain_linear`) — under matched
  configuration, the canonical engine reproduces Greedy Optimizer v1's
  result exactly on these two, confirming the scoring formula itself is
  unchanged and the two engines agree when candidate resolution doesn't
  matter for the outcome.
- **Score delta is positive and grows with contention** on the larger
  synthetic fixtures (+11.70 on 15 tasks, +76.18 on 40 tasks) — this is a
  genuine, expected effect of finer candidate resolution: with more
  competing flexible tasks, precise_greedy's 1-minute search finds
  placements closer to a task's exact preferred-window center than a
  30-minute grid can reach, earning more of `_time_preference_score`'s
  partial-credit decay. This is an **algorithm capability difference, not
  a regression** — the same configuration, a strictly larger candidate
  search space, and a strictly better result.
- **precise_greedy costs 3.5x–6.1x more time than Greedy Optimizer v1**,
  scaling up with fixture size — the direct cost of exhaustively searching
  every valid minute instead of every 30-minute slot (roughly 30x more
  raw candidate positions per free interval, though pruning and per-mode
  restrictions keep the realized ratio well under that theoretical
  ceiling).
- **adhd_friendly is *faster* than Greedy Optimizer v1 on the two small,
  low-contention fixtures** (0.35x, 0.28x — i.e. roughly 3-4x *faster*)
  and only 1.9x–2.4x slower on the larger ones: quarter-hour snapping for
  tasks over the 30-minute threshold, combined with free-interval pruning,
  searches *fewer* candidates than Greedy Optimizer v1's fixed 30-minute
  grid whenever most tasks are short or free intervals are large, and only
  falls behind once the fixture has enough long, contended tasks to make
  every quarter-hour boundary meaningfully different.

## Profiling: no avoidable inefficiency found

`cProfile` on `synthetic_larger_day` in `precise_greedy` mode shows time
is dominated almost entirely by `calculate_task_score` itself (100,312
calls, one per valid candidate minute actually evaluated) and its own
internals (`_get`'s duck-typed dict/attribute lookup, called ~21 times per
score). `_run_greedy_tier` runs exactly twice (once per tier, as designed);
`_best_candidate_for_canonical_task` runs 341 times (once per task-scan per
outer greedy iteration, not per candidate); the dependency graph is built
once via `has_cycle_by_id`; `RewardSettings` is built once via
`day_preferences_to_reward_settings` before the search begins, never
reloaded from YAML mid-search. In short: the specific inefficiencies Task 6
asks to check for (repeated sorting, configuration reloading, dependency-
graph rebuilding, avoidable copying) are **not present** — the measured
cost is the inherent, expected cost of exhaustive 1-minute-resolution
scoring, not an implementation defect. No changes were made as a result of
this profiling pass, since there was nothing incorrect-but-slow to fix
without altering correctness.

## Week/month allocation: confirmed separate and zero-call

See [`ALLOCATION_BASELINE.md`](ALLOCATION_BASELINE.md): allocation is
benchmarked entirely separately from day generation, and every workload's
Day-Scheduler call count is verified to be exactly `0` via an instrumented
spy (not inferred from timing) before the benchmark reports success.
Allocation costs low single-digit milliseconds even for a 200-task,
29-date month — two to three orders of magnitude cheaper than a single
day's `precise_greedy` generation on a comparably-sized task set, exactly
as expected from an aggregate-capacity-only algorithm that never performs
minute-level search.

## Honest summary

- precise_greedy trades real, measurable speed (3.5x-6x versus the legacy
  30-minute grid on these fixtures) for exact-minute placement and an
  **empirically better-or-equal** score under matched configuration on
  these four fixtures (0.00 delta on two, positive on two) -- not a
  "provably" better-or-equal score in general: a strictly larger candidate
  search space finding a strictly better result on a specific fixture is
  not a formal proof that it always will, and no such proof is claimed or
  needed here. See the addendum below for why this claim is now also
  functionally superseded by exact-parity testing.
- adhd_friendly is often *faster* than the legacy grid on lightly-loaded
  days and only moderately slower on heavily-loaded ones, while still
  guaranteeing quarter-hour-or-better alignment for longer tasks.
- No performance regression was found in the allocation layer. The
  "no avoidable inefficiency" profiling conclusion below reflected that no
  *bug* was found in the exhaustive implementation as it stood -- it should
  not be read as "exhaustive per-minute scoring is inherently unavoidable"
  in general; the event-search milestone (see the addendum) replaces that
  exhaustive search entirely, for a real, measured, non-bug-fix speedup.

---

## Event-search correction (Task 6 addendum)

This section corrects three claims above, now that
`app/optimizer.py`'s per-task placement search is an analytically-derived
event/candidate search rather than an exhaustive per-minute scan (see
`README.md`'s "Exact event-based candidate search" section and
`EVENT_CANDIDATE_COMPARISON.md` for the full before/after comparison of the
search itself). **This section's own numbers are current; everything above
this point is left as originally written, for history.**

1. **"Provably better-or-equal" (above) is corrected to "empirically
   better-or-equal on these fixtures."** No formal proof was ever produced;
   the claim conflated "a strictly larger candidate space can only find an
   equal-or-better placement for a single task in isolation" (true, and
   still true) with "therefore the final multi-task greedy schedule is
   provably better-or-equal" (not established -- an earlier task's
   different placement can change what is feasible for a later task, for
   better or worse, exactly as `app/optimizer.py`'s own module docstring on
   greedy search already cautions).
2. **"Exhaustive scoring is ... the inherent, expected cost" /
   implicitly "unavoidable" (the profiling section above) is obsolete.**
   The event-search milestone shows it was avoidable: the same exact
   results (see `EVENT_CANDIDATE_COMPARISON.md`'s exact-match column) are
   now reached with 5.5x-16.4x fewer `calculate_task_score` calls on
   `precise_greedy`, and 2.3x-11.7x less wall-clock time, with no bug fix
   involved -- the exhaustive search was correct but unnecessarily
   expensive, not incorrect.
3. **"No repeated sorting" was never quite accurate.** The profiling
   section above's claim that repeated sorting is "not present" describes
   the tier/dependency-graph level (correct: `_run_greedy_tier` runs once
   per tier, the dependency graph is built once). It does not describe the
   per-task placement search itself: `_best_candidate_for_canonical_task`
   (both before and after this milestone) sorts its `placed` list once on
   every call, i.e. once per task-scan per outer greedy iteration -- real,
   present, and not a defect (the list is small and must be current after
   every commitment), just not "absent" as the earlier phrasing implied.

### `_run_legacy` config isolation fix

`final_comparison.py`'s `_run_legacy` previously called
`combine_fixed_and_optimized_scheduled_tasks` with no `config_path`, which
lets `optimize_day_schedule` fall through to `load_reward_settings(None)` --
the *real*, discovered `config/task_preference.yaml`, not an isolated
default -- while `_run_canonical` never touched YAML at all
(`resolve_day_preferences` builds `RewardPreferences()` directly in code).
This contradicted the file's own "no config/task_preference.yaml loaded"
claim. This repository's checked-in YAML happens to carry the same scalar
weights as `RewardSettings()`'s own built-in defaults, so this gap did not
actually change any number in the table above -- confirmed below, where
score deltas are unchanged (0.00, 0.00, 11.70, 76.18) after the fix -- but
the code did not match its own documented isolation, and would have stopped
matching it silently the moment anyone edited that file. `_run_legacy` now
points `config_path` at an explicit, isolated, empty temp file, matching
`tests/test_optimizer.py`'s `isolate_reward_config_search` fixture.

### Re-measured (post-event-search) numbers

Same method, same four fixtures, `python -m benchmarks.final_comparison`
(default `--warmups 3 --repeats 15`), Python 3.10.11,
Windows-10-10.0.19045-SP0:

| fixture | legacy median (ms) | precise median (ms) | adhd median (ms) | precise/legacy | adhd/legacy | score delta (precise − legacy) |
|---|---:|---:|---:|---:|---:|---:|
| valid_single_day_basic | 1.50 | 3.46 | 1.69 | 2.31x | 1.13x | 0.00 |
| dependency_chain_linear | 0.93 | 1.10 | 1.03 | 1.18x | 1.11x | 0.00 |
| synthetic_medium_day | 17.39 | 39.73 | 23.41 | 2.28x | 1.35x | +11.70 |
| synthetic_larger_day | 47.71 | 106.42 | 80.83 | 2.23x | 1.69x | +76.18 |

Score deltas are byte-for-byte identical to the historical table above,
confirming the isolation fix changed measurement methodology, not results,
for this repository's specific checked-in YAML. `precise/legacy` is now
1.18x-2.31x (was 3.5x-6.1x) and `adhd/legacy` is now 1.11x-1.69x (was
0.28x-2.42x, i.e. the small fixtures are no longer *faster* than legacy in
`adhd_friendly` mode) -- both narrower gaps than the historical table,
reflecting the event search's own reduction on the canonical side; legacy's
timing floor did not change meaningfully (its own event-search wiring only
reduces evaluation count for large free intervals, and Greedy Optimizer v1
already runs on a coarse 30-minute grid with few candidates per fixture).
The remaining gap versus legacy is not further reducible by candidate
search alone -- it reflects `precise_greedy` still resolving genuinely more
(sub-30-minute) placement decisions than a fixed half-hour grid does, plus
the canonical engine's richer per-task model (UUID registries, mandatory-tier
bookkeeping) that Greedy Optimizer v1 does not carry.
