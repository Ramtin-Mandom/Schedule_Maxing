# Normal / ADHD scheduling baseline (captured before recurrence expansion)

`benchmarks/results/prompt1_scheduling_mode_baseline.json`, written by
`python -m benchmarks.scheduling_mode_baseline` for the later comparison of
`docs/next-milestone-prompts/06-integration.md`.

- **Source:** the unmodified committed tree of revision
  `ec072e7e7b86655eff6240b8a936fd16ceb28ed7`, exported with `git archive` into a
  scratch directory and run with `--source-root` (the checkout was never switched;
  the file records where the application was imported from).
- **Environment:** CPython 3.10.11, Windows 10 (19045), pydantic 2.13.3,
  SQLAlchemy 2.0.54, tzdata 2026.2, PyYAML 6.0.3.
- **Inputs:** uuid5 ids, fixed audit timestamps and clock, seed `20260930`, the week
  2026-03-02 .. 2026-03-08 in `UTC` and `America/New_York`, an explicit preference
  template (not `config/task_preference.yaml`), modes `precise_greedy` (Normal) and
  `adhd_friendly` (ADHD).
- **Scenarios** per zone and mode: one date through `app.optimizer.generate_day_schedule`;
  the persisted week through `app.planning.workflow.generate` (in-memory SQLite);
  and an over-committed variant of each, so unscheduled (`no_valid_slot`) and
  unallocated (`capacity_exceeded`) reasons are recorded.
- **Recorded:** every placement (task, local interval, score), unscheduled and
  unallocated reasons, day/week totals. Timings (`_elapsed_ms`) are informational and
  ignored by `--check`.

`python -m benchmarks.scheduling_mode_baseline --check benchmarks/results/prompt1_scheduling_mode_baseline.json`
compares a fresh run of the current code with it; after the recurrence work it reports
"identical to the saved baseline".

No older published benchmark number was reused.
