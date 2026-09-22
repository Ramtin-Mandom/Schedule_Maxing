"""
benchmarks/allocation_baseline.py

Deterministic week/month allocation benchmark (Task 5). Measures
app.planning.allocation.allocate_tasks on synthetic, seeded task pools --
never a minute-level schedule -- and verifies (via an instrumented spy, not
a brittle timing assertion) that allocation calls
app.optimizer.generate_day_schedule zero times, for every workload.

Usage:
    python -m benchmarks.allocation_baseline
    python -m benchmarks.allocation_baseline --repeats 25 --warmups 5
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import app.optimizer as optimizer_module
from app.planning.allocation import allocate_tasks, month_dates, week_dates
from app.planning.models import Task, TaskRegistry
from app.planning.preferences import resolve_day_preferences

BASE_DIR = Path(__file__).resolve().parent.parent
TZ = "UTC"

DEFAULT_WARMUPS = 3
DEFAULT_REPEATS = 15


def _generate_tasks(seed: int, count: int) -> list[Task]:
    rng = random.Random(seed)
    categories = ["health", "enjoyment", "study", "work", "chores"]
    tasks = []
    for index in range(count):
        duration = rng.choice([30, 45, 60, 90, 120, 180])
        priority = rng.randint(1, 10)
        required = rng.random() < 0.2
        tasks.append(
            Task(
                name=f"Task {index:03d}", category=rng.choice(categories),
                estimated_duration_minutes=duration, priority=priority, required=required,
            )
        )
    # Deterministic acyclic dependency edges: each task may depend on an
    # earlier-indexed task only, so the graph is guaranteed acyclic.
    for index, task in enumerate(tasks):
        if index > 0 and rng.random() < 0.15:
            dependency = tasks[rng.randint(0, index - 1)]
            tasks[index] = task.model_copy(update={"dependency_ids": [dependency.id]})
    return tasks


@dataclass
class TimingStats:
    median_ms: float
    min_ms: float
    max_ms: float
    stdev_ms: float
    samples: int


def _timing_stats(samples_seconds: list[float]) -> TimingStats:
    samples_ms = [value * 1000.0 for value in samples_seconds]
    return TimingStats(
        median_ms=round(statistics.median(samples_ms), 4),
        min_ms=round(min(samples_ms), 4),
        max_ms=round(max(samples_ms), 4),
        stdev_ms=round(statistics.pstdev(samples_ms), 4) if len(samples_ms) > 1 else 0.0,
        samples=len(samples_ms),
    )


def _run_workload(name: str, dates: list[date], task_count: int, seed: int, warmups: int, repeats: int) -> dict:
    tasks = _generate_tasks(seed, task_count)
    registry = TaskRegistry()
    for task in tasks:
        registry.add(task)
    task_ids = [task.id for task in tasks]
    preferences_by_date = {day: resolve_day_preferences(date=day, timezone=TZ) for day in dates}

    # Spy: fail loudly if allocation ever calls the Day Scheduler.
    call_count = 0
    original = optimizer_module.generate_day_schedule

    def _spy(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original(*args, **kwargs)

    optimizer_module.generate_day_schedule = _spy
    try:
        for _ in range(warmups):
            allocate_tasks(
                start_date=dates[0], end_date=dates[-1], tasks=registry, task_ids=task_ids,
                preferences_by_date=preferences_by_date,
            )

        samples: list[float] = []
        last_result = None
        for _ in range(repeats):
            t0 = time.perf_counter()
            last_result = allocate_tasks(
                start_date=dates[0], end_date=dates[-1], tasks=registry, task_ids=task_ids,
                preferences_by_date=preferences_by_date,
            )
            t1 = time.perf_counter()
            samples.append(t1 - t0)
    finally:
        optimizer_module.generate_day_schedule = original

    return {
        "workload": name,
        "date_count": len(dates),
        "task_count": task_count,
        "assigned_count": len(last_result.assignments),
        "unallocated_count": len(last_result.unallocated),
        "day_scheduler_call_count": call_count,
        "allocate_time": asdict(_timing_stats(samples)),
    }


def run_benchmark(warmups: int = DEFAULT_WARMUPS, repeats: int = DEFAULT_REPEATS) -> dict:
    week = week_dates(date(2024, 6, 3))
    month = month_dates(2024, 2)  # leap February, exercised deliberately

    results = [
        _run_workload("week_20_tasks", week, 20, seed=101, warmups=warmups, repeats=repeats),
        _run_workload("week_60_tasks", week, 60, seed=102, warmups=warmups, repeats=repeats),
        _run_workload("month_leap_feb_80_tasks", month, 80, seed=103, warmups=warmups, repeats=repeats),
        _run_workload("month_leap_feb_200_tasks", month, 200, seed=104, warmups=warmups, repeats=repeats),
    ]

    return {
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "method": {
            "function": "app.planning.allocation.allocate_tasks",
            "warmups": warmups,
            "repeats": repeats,
            "timer": "time.perf_counter",
            "day_scheduler_spy": "app.optimizer.generate_day_schedule wrapped; call_count must be 0 for every workload",
        },
        "workloads": results,
    }


def _print_summary(report: dict) -> None:
    print("Allocation baseline benchmark (Task 5)")
    print(f"  python: {report['environment']['python_version'].splitlines()[0]}")
    print(f"  warmups={report['method']['warmups']} repeats={report['method']['repeats']}")
    print()
    header = f"{'workload':<28}{'dates':>7}{'tasks':>7}{'assigned':>10}{'unalloc':>9}{'sched calls':>13}{'allocate(ms)':>15}"
    print(header)
    print("-" * len(header))
    for workload in report["workloads"]:
        print(
            f"{workload['workload']:<28}{workload['date_count']:>7}{workload['task_count']:>7}"
            f"{workload['assigned_count']:>10}{workload['unallocated_count']:>9}"
            f"{workload['day_scheduler_call_count']:>13}{workload['allocate_time']['median_ms']:>15.4f}"
        )
        assert workload["day_scheduler_call_count"] == 0, (
            f"allocate_tasks called the Day Scheduler for workload {workload['workload']!r}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--output", type=Path, default=BASE_DIR / "benchmarks" / "results" / "allocation_baseline.json")
    args = parser.parse_args()

    report = run_benchmark(warmups=args.warmups, repeats=args.repeats)
    _print_summary(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2)
    print(f"\nWrote {args.output}")
    print("Day Scheduler call count was 0 for every workload -- confirmed.")


if __name__ == "__main__":
    main()
