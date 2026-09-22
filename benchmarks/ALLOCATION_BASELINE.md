# Allocation baseline (Task 5)

Deterministic, seeded week/month allocation benchmark for
`app.planning.allocation.allocate_tasks`. See
[`allocation_baseline.py`](allocation_baseline.py)
(`python -m benchmarks.allocation_baseline`).

## Method

- Four synthetic workloads (fixed seeds `101`-`104`, `random.Random`), each
  with a deterministic acyclic dependency graph (~15% of tasks depend on an
  earlier-indexed task) and ~20% of tasks marked `required`:
  - `week_20_tasks` / `week_60_tasks`: 7 dates, 20/60 tasks.
  - `month_leap_feb_80_tasks` / `month_leap_feb_200_tasks`: February 2024
    (29 dates, a leap year, exercised deliberately), 80/200 tasks.
- 3 warmups (discarded), 15 measured repeats, `time.perf_counter`.
- **Day-Scheduler call count is verified with a spy**, not inferred from
  timing: `app.optimizer.generate_day_schedule` is wrapped before each
  workload runs, and the wrapped call count is asserted to be exactly `0`
  for every workload before the benchmark reports success.

## Measured results

| workload | dates | tasks | assigned | unallocated | Day Scheduler calls | allocate median (ms) |
|---|---:|---:|---:|---:|---:|---:|
| week_20_tasks | 7 | 20 | 20 | 0 | **0** | 0.21 |
| week_60_tasks | 7 | 60 | 60 | 0 | **0** | 0.59 |
| month_leap_feb_80_tasks | 29 | 80 | 80 | 0 | **0** | 1.72 |
| month_leap_feb_200_tasks | 29 | 200 | 200 | 0 | **0** | 4.46 |

(Full precision in `benchmarks/results/allocation_baseline.json`.)

## Reading the numbers

- **Zero Day Scheduler calls, confirmed by instrumentation, on every
  workload** — allocation never invokes minute-level scheduling, exactly
  as required.
- Allocation is very fast relative to the day engine (Task 4): even 200
  tasks across a 29-date month completes in ~4.5ms median, versus
  hundreds of milliseconds for a single day's `precise_greedy` generation
  on a comparably-sized task set (see `DAY_ENGINE_BASELINE.md`) — expected,
  since allocation only reasons about aggregate per-date capacity in
  minutes, never minute-by-minute candidate search.
- Growth from 20→60 tasks (same 7 dates) and 80→200 tasks (same 29 dates)
  is worse than linear (the priority-guided topological sort and
  date-ranking work scale with both task and date count), but remains well
  within sub-10ms territory at these sizes — no scaling concern observed
  at personal-planning scale.

## Limitations

- Same environment caveats as the other benchmarks in this directory
  (Windows wall-clock jitter vs. a dedicated CI runner).
- These synthetic workloads have no fixed-block occupancy; a heavily
  fixed-block-constrained month would reduce free capacity per date and
  could change which tasks fit, though the allocation algorithm's own
  per-task cost is unaffected by that (capacity is precomputed once).
