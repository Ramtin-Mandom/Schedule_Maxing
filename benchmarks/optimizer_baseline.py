"""
benchmarks/optimizer_baseline.py

Reproducible standard-library benchmark for Greedy Optimizer v1
(app/optimizer.py's optimize_day_schedule /
combine_fixed_and_optimized_scheduled_tasks), measured on the legacy CSV
pipeline (app/data_processor.py) exactly as app/main.py and app/app.py use
it today. This does not exercise app/planning/* at all -- it exists to
record what the current 30-minute-grid greedy optimizer actually costs,
before any future optimizer work changes it (see Task 6 in the project
plan).

Fixtures:
    - samples/inputs/valid_single_day_basic.csv   (checked-in sample)
    - samples/inputs/dependency_chain_linear.csv  (checked-in sample)
    - benchmarks/fixtures/synthetic_medium_day.csv (generated, seed=42)
    - benchmarks/fixtures/synthetic_larger_day.csv (generated, seed=43)

Each fixture, configuration, and random seed is fixed (see
generate_fixtures.py); only the fixed single "date" (day 1) present in
each fixture is used, and every fixture is single-day. Fixed configuration
means no config_path is passed, so Greedy Optimizer v1 runs on
RewardSettings' built-in defaults (see app/reward.py's module docstring)
-- the same defaults app/main.py and app/app.py use today.

Import (CSV parsing + Pydantic model construction) and optimization
(the greedy search itself) are timed separately, each with its own warmup
and repeat count, using time.perf_counter. Median and spread (population
standard deviation) are reported for both. This script never keeps a
second production optimizer around -- it only calls the one real
optimize_day_schedule / combine_fixed_and_optimized_scheduled_tasks
entry point that app/main.py already uses.

Usage:
    python -m benchmarks.optimizer_baseline
    python -m benchmarks.optimizer_baseline --repeats 25 --warmups 5
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.data_processor import load_schedule_from_csv
from app.optimizer import combine_fixed_and_optimized_scheduled_tasks

BASE_DIR = Path(__file__).resolve().parent.parent

FIXTURES: list[tuple[str, Path]] = [
    ("valid_single_day_basic", BASE_DIR / "samples" / "inputs" / "valid_single_day_basic.csv"),
    ("dependency_chain_linear", BASE_DIR / "samples" / "inputs" / "dependency_chain_linear.csv"),
    ("synthetic_medium_day", BASE_DIR / "benchmarks" / "fixtures" / "synthetic_medium_day.csv"),
    ("synthetic_larger_day", BASE_DIR / "benchmarks" / "fixtures" / "synthetic_larger_day.csv"),
]

DEFAULT_WARMUPS = 3
DEFAULT_REPEATS = 15


@dataclass
class TimingStats:
    median_ms: float
    min_ms: float
    max_ms: float
    stdev_ms: float
    samples: int


@dataclass
class FixtureResult:
    fixture: str
    fixed_block_count: int
    flexible_task_count: int
    scheduled_count: int
    unscheduled_count: int
    total_score: float
    import_time: TimingStats
    optimize_time: TimingStats


def _timing_stats(samples_seconds: list[float]) -> TimingStats:
    samples_ms = [value * 1000.0 for value in samples_seconds]
    return TimingStats(
        median_ms=round(statistics.median(samples_ms), 4),
        min_ms=round(min(samples_ms), 4),
        max_ms=round(max(samples_ms), 4),
        stdev_ms=round(statistics.pstdev(samples_ms), 4) if len(samples_ms) > 1 else 0.0,
        samples=len(samples_ms),
    )


def _run_fixture(name: str, path: Path, warmups: int, repeats: int) -> FixtureResult:
    if not path.exists():
        raise FileNotFoundError(f"benchmark fixture not found: {path}")

    for _ in range(warmups):
        schedule_input = load_schedule_from_csv(str(path))
        day_schedule = schedule_input.schedules[1]
        combine_fixed_and_optimized_scheduled_tasks(date=1, day_schedule=day_schedule)

    import_samples: list[float] = []
    optimize_samples: list[float] = []
    last_output = None
    last_day_schedule = None

    for _ in range(repeats):
        t0 = time.perf_counter()
        schedule_input = load_schedule_from_csv(str(path))
        day_schedule = schedule_input.schedules[1]
        t1 = time.perf_counter()

        output = combine_fixed_and_optimized_scheduled_tasks(date=1, day_schedule=day_schedule)
        t2 = time.perf_counter()

        import_samples.append(t1 - t0)
        optimize_samples.append(t2 - t1)
        last_output = output
        last_day_schedule = day_schedule

    assert last_output is not None and last_day_schedule is not None

    return FixtureResult(
        fixture=name,
        fixed_block_count=len(last_day_schedule.fixed_blocks),
        flexible_task_count=len(last_day_schedule.tasks),
        scheduled_count=len(last_output.scheduled_tasks),
        unscheduled_count=len(last_output.unscheduled_tasks),
        total_score=last_output.total_score,
        import_time=_timing_stats(import_samples),
        optimize_time=_timing_stats(optimize_samples),
    )


def run_benchmark(warmups: int = DEFAULT_WARMUPS, repeats: int = DEFAULT_REPEATS) -> dict:
    results = [_run_fixture(name, path, warmups, repeats) for name, path in FIXTURES]

    return {
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "method": {
            "optimizer": "Greedy Optimizer v1 (app.optimizer.optimize_day_schedule)",
            "time_slot_minutes": 30,
            "warmups": warmups,
            "repeats": repeats,
            "timer": "time.perf_counter",
            "config_path": None,
        },
        "fixtures": [asdict(result) for result in results],
    }


def _print_summary(report: dict) -> None:
    print("Greedy Optimizer v1 baseline benchmark")
    print(f"  python: {report['environment']['python_version'].splitlines()[0]}")
    print(f"  platform: {report['environment']['platform']}")
    print(f"  warmups={report['method']['warmups']} repeats={report['method']['repeats']}")
    print()

    header = (
        f"{'fixture':<26}{'fixed':>6}{'tasks':>7}{'sched':>7}{'unsched':>9}"
        f"{'score':>9}{'import(ms)':>13}{'optimize(ms)':>15}"
    )
    print(header)
    print("-" * len(header))

    for fixture in report["fixtures"]:
        print(
            f"{fixture['fixture']:<26}"
            f"{fixture['fixed_block_count']:>6}"
            f"{fixture['flexible_task_count']:>7}"
            f"{fixture['scheduled_count']:>7}"
            f"{fixture['unscheduled_count']:>9}"
            f"{fixture['total_score']:>9.2f}"
            f"{fixture['import_time']['median_ms']:>13.4f}"
            f"{fixture['optimize_time']['median_ms']:>15.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--output",
        type=Path,
        default=BASE_DIR / "benchmarks" / "results" / "greedy_v1_baseline.json",
    )
    args = parser.parse_args()

    report = run_benchmark(warmups=args.warmups, repeats=args.repeats)
    _print_summary(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2)
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
