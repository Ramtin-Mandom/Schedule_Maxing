# Canonical day engine — provisional performance results (Task 4)

This records **provisional** measured performance of the new canonical day
engine (`app.optimizer.generate_day_schedule`, added by Task 4) in both
`precise_greedy` and `adhd_friendly` modes, on the same four fixtures used
for the [Greedy Optimizer v1 baseline](BASELINE.md) (Task 1). It is a
same-environment sanity check, not yet the rigorous, baseline-equivalent
comparison Task 6 will do — see **Important caveat** below.

## Method

- Script: [`benchmarks/day_engine_baseline.py`](day_engine_baseline.py)
  (`python -m benchmarks.day_engine_baseline`). Standard library only.
- Fixtures converted from their legacy CSV form to canonical models via
  `app.planning.compat.import_legacy_csv_rows`, with a fixed anchor date
  (`2026-01-05`) and `UTC` timezone — the same fixture files Task 1's
  benchmark uses (`samples/inputs/valid_single_day_basic.csv`,
  `dependency_chain_linear.csv`, and the two seeded synthetic fixtures).
- For each fixture, both `precise_greedy` and `adhd_friendly` modes are run
  with 3 warmup iterations (discarded) and 15 measured repeats, timing only
  `generate_day_schedule` itself via `time.perf_counter` (import/conversion
  cost is excluded, same convention as Task 1's benchmark).
- Default `DayPreferences` (`resolve_day_preferences`) are used, with only
  `optimizer_mode` overridden — i.e. the new template's neutral category
  weights (`health`/`enjoyment`/`study`/`work`/`chores` at `1.0`) and default
  reward weights, not a config reproducing Greedy Optimizer v1's exact
  historical settings.

## Important caveat: not yet a baseline-equivalent comparison

The category names in these legacy fixtures (`study`, `food`, `health`,
`sleep`, `leisure`, ...) mostly don't overlap with the new template's five
primary categories, and RewardSettings' defaults differ slightly in a few
places from what Milestone 0's benchmark implicitly used. As a result, the
**scores differ slightly** between the Task 1 and Task 4 runs on the same
input (e.g. `synthetic_medium_day` scored 341.18 under Greedy Optimizer v1
vs 352.88 under the canonical engine) — this reflects **scoring/candidate
resolution differences**, not a like-for-like apples-to-apples run. Task 6's
final performance review is explicitly scoped to build an
"explicit baseline-equivalent scoring configuration" so old-vs-new numbers
become directly comparable; until then, treat the timing numbers below as
**within-canonical-engine, cross-mode** comparisons (precise vs ADHD), and
the cross-milestone timing ratios as directional only.

## Measured results (this run)

| fixture | mode | flexible tasks | scheduled | unscheduled | score | generate median (ms) |
|---|---|---:|---:|---:|---:|---:|
| valid_single_day_basic | precise_greedy | 3 | 3 | 0 | 89.00 | 18.73 |
| valid_single_day_basic | adhd_friendly | 3 | 3 | 0 | 89.00 | 1.51 |
| dependency_chain_linear | precise_greedy | 4 | 4 | 0 | 178.00 | 12.47 |
| dependency_chain_linear | adhd_friendly | 4 | 4 | 0 | 178.00 | 0.99 |
| synthetic_medium_day | precise_greedy | 15 | 12 | 3 | 352.88 | 141.59 |
| synthetic_medium_day | adhd_friendly | 15 | 12 | 3 | 352.88 | 62.48 |
| synthetic_larger_day | precise_greedy | 40 | 13 | 27 | 539.81 | 543.21 |
| synthetic_larger_day | adhd_friendly | 40 | 13 | 27 | 539.81 | 214.41 |

(Full precision, stdev, and environment metadata in
`benchmarks/results/day_engine_baseline.json`.)

## Reading the numbers

- **adhd_friendly is consistently faster than precise_greedy** on every
  fixture (roughly 2–2.5x on the small fixtures, ~2.3x on the larger ones):
  quarter-hour snapping for tasks over the 30-minute threshold prunes the
  candidate set far more than precise_greedy's 1-minute resolution allows,
  exactly as expected from the free-interval-pruned candidate generation
  (see `app/optimizer.py`'s `_candidate_starts`).
- **precise_greedy is markedly slower than Greedy Optimizer v1's fixed
  30-minute grid** on every fixture (roughly 5–17x the Task 1 median,
  scaling with fixture size) — expected and by design: 1-minute resolution
  evaluates on the order of 30x more candidate start times per free
  interval than a 30-minute grid does. This is the primary "algorithm
  limit" to report honestly for this milestone: precision has a real,
  measurable performance cost, most visible on `synthetic_larger_day`.
- Both canonical-engine modes schedule the same counts as each other on
  every fixture here (candidate resolution didn't change *which* tasks fit,
  only *how finely* their start times were searched) — this is fixture-
  dependent, not a general guarantee.

## Limitations

- Same environment caveats as Task 1's benchmark: Windows wall-clock timing
  has more jitter than a dedicated CI runner.
- Not yet a solver-vs-solver apples-to-apples comparison against Greedy
  Optimizer v1 (see caveat above) — that is Task 6's job.
- Week/month allocation performance is out of scope here (Task 5).
