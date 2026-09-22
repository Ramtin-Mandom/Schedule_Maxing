# Event-based candidate search vs. exhaustive reference (Task 6)

`app.optimizer._best_candidate_for_canonical_task` (the production per-task
placement search, both `precise_greedy` and `adhd_friendly`) vs. its own
independent exhaustive reference, `_best_candidate_for_canonical_task_exhaustive`
(every feasible minute in `precise_greedy`; every mode-eligible minute --
1-minute for short `adhd_friendly` tasks, quarter-hour lattice for longer
ones -- in `adhd_friendly`, matching `_candidate_starts` exactly). Both are
driven through the real production entry point
(`app.optimizer.generate_day_schedule`) via a module-level monkeypatch of
the per-task search function, so both sides run the identical greedy
orchestration, dependency/deadline handling, and mandatory-tier logic --
only the placement *search* differs. Script:
[`event_candidate_comparison.py`](event_candidate_comparison.py)
(`python -m benchmarks.event_candidate_comparison`); raw data in
`benchmarks/results/event_candidate_comparison.json`.

`adhd_friendly` is benchmarked against its own mode-eligible exhaustive
reference, not against unrestricted one-minute scheduling -- see the
"Grid-constrained workloads" note below for why its reduction numbers are
much smaller than `precise_greedy`'s.

## Method

3 warmups (discarded), 15 measured repeats, `time.perf_counter`, timing
only `generate_day_schedule` itself. A separate, untimed instrumented run
(after the timed samples) counts real `calculate_task_score` calls for each
side, so counting overhead never distorts the reported timings. The
benchmark **raises** (fails) if the event search and the exhaustive
reference ever disagree on total score, per-placement identity
(task/start/end/score), or scheduled/unscheduled accounting for any
workload -- every row below already passed that check.

Fixtures: the same four `optimizer_baseline.py`/`day_engine_baseline.py`
fixtures, plus two new deterministic synthetic workloads built directly as
canonical models (fixed seed, no CSV/YAML round trip needed for their
settings):
- `non_midnight_tagged_adhd` -- 20 tasks, day window 06:15-21:15 (not
  midnight-aligned), active `tag_relations`, `short_gap_bonus` enabled
  (`weight=4.0`), 4 fixed blocks fragmenting the day.
- `large_fragmented_day` -- 60 tasks, 12 fixed blocks, fragmentation penalty
  active, exercising many small free intervals at once.

## Measured results (this run)

| workload | mode | tasks | fixed | exhaustive median (ms) | event median (ms) | speedup | exhaustive evals | event evals | eval reduction | exact match |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| valid_single_day_basic | precise_greedy | 3 | 4 | 19.34 | 3.34 | 5.78x | 3288 | 459 | 7.16x | yes |
| valid_single_day_basic | adhd_friendly | 3 | 4 | 1.52 | 1.56 | 0.97x | 236 | 236 | 1.00x | yes |
| dependency_chain_linear | precise_greedy | 4 | 2 | 12.38 | 1.06 | 11.68x | 2854 | 174 | 16.40x | yes |
| dependency_chain_linear | adhd_friendly | 4 | 2 | 0.99 | 1.00 | 0.98x | 194 | 194 | 1.00x | yes |
| synthetic_medium_day | precise_greedy | 15 | 4 | 145.75 | 33.98 | 4.29x | 24441 | 4448 | 5.50x | yes |
| synthetic_medium_day | adhd_friendly | 15 | 4 | 62.23 | 21.36 | 2.91x | 10525 | 2862 | 3.68x | yes |
| synthetic_larger_day | precise_greedy | 40 | 4 | 545.50 | 96.17 | 5.67x | 100312 | 12717 | 7.89x | yes |
| synthetic_larger_day | adhd_friendly | 40 | 4 | 213.45 | 61.98 | 3.44x | 38628 | 9003 | 4.29x | yes |
| non_midnight_tagged_adhd | precise_greedy | 20 | 4 | 384.83 | 126.76 | 3.04x | 61978 | 15708 | 3.95x | yes |
| non_midnight_tagged_adhd | adhd_friendly | 20 | 4 | 269.54 | 136.55 | 1.97x | 40774 | 14280 | 2.85x | yes |
| large_fragmented_day | precise_greedy | 60 | 12 | 2492.39 | 1104.11 | 2.26x | 383603 | 127240 | 3.02x | yes |
| large_fragmented_day | adhd_friendly | 60 | 12 | 2164.82 | 1123.79 | 1.93x | 321196 | 122091 | 2.63x | yes |

Python 3.10.11, Windows-10-10.0.19045-SP0. Command:
`python -m benchmarks.event_candidate_comparison` (default `--warmups 3 --repeats 15`).

## Reading the numbers

- **Exact semantic match on all 12 workload/mode combinations** -- every
  total score, every placement's task/start/end/rounded-score, and every
  scheduled/unscheduled accounting is identical between the event search
  and the exhaustive reference. This is the acceptance bar Task 6 sets:
  fewer evaluations *and* an identical result, not just a faster wrong
  answer.
- **`precise_greedy` gets real, substantial reductions** (5.5x-16.4x fewer
  `calculate_task_score` calls, 2.3x-11.7x faster wall-clock) that grow with
  free-interval size relative to the number of actual reward-component
  breakpoints -- `dependency_chain_linear`'s long, mostly-unconstrained free
  intervals see the largest reduction (16.4x), while `large_fragmented_day`'s
  many small, contended intervals (60 tasks, 12 fixed blocks) see a smaller
  but still real 3.0x, because more of the day falls inside a bounded
  "danger zone" (near a neighbor or the short-gap-active range) that is
  evaluated directly rather than via the monotonic binary search.
- **Grid-constrained workloads (`adhd_friendly`) see little to no
  reduction, by design.** A quarter-hour (or legacy's half-hour) lattice
  already bounds the candidate count to at most `1440 / step` points before
  any event-search machinery runs (see README.md's "Exact event-based
  candidate search" section) -- so for `adhd_friendly`, the "event search"
  and "exhaustive reference" are the same lattice enumeration by
  construction on small fixtures (1.00x on the two smallest), and the
  measured reduction on larger fixtures (2.6x-4.3x) comes entirely from
  short tasks (duration <= 30 minutes) within `adhd_friendly`, which still
  use full 1-minute resolution and do benefit from the machinery. This is
  not a comparison against unrestricted one-minute scheduling and is not
  labeled as one.
- **`non_midnight_tagged_adhd` confirms the machinery works correctly under
  the exact conditions Task 6's regressions were found in**: a day window
  that does not start at local midnight, active `tag_relations`, and an
  enabled `short_gap_bonus` all together -- still an exact match, at a real
  2.0x-3.0x reduction.

## Relationship to `benchmarks/FINAL_COMPARISON.md`

`FINAL_COMPARISON.md` is now historical (see the note at its top): it
measures Greedy Optimizer v1 vs. the canonical engine from *before* this
event-search milestone, when `precise_greedy` scored every feasible minute.
This benchmark instead measures the event search against its own
same-milestone exhaustive reference, and should be read as: "the canonical
engine, which `FINAL_COMPARISON.md` showed costs ~2-6x more than Greedy
Optimizer v1's 30-minute grid for exact-minute placement, now finds that
same exact result 2x-12x faster than it used to" -- narrowing, not
eliminating, the legacy/canonical timing gap `FINAL_COMPARISON.md` first
measured (see that file's own addendum for updated head-to-head numbers).
