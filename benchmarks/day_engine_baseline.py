"""
benchmarks/day_engine_baseline.py

Benchmark for the canonical day engine (app.optimizer.generate_day_schedule)
in both precise_greedy and adhd_friendly modes, on the synthetic CSV
fixtures (benchmarks/generate_fixtures.py). Each fixture's first day is
imported with the app's own CSV importer
(app.planning.csv_import.parse_legacy_csv_file, a fixed anchor date; see
ANCHOR_DATE below).

Usage:
    python -m benchmarks.day_engine_baseline
    python -m benchmarks.day_engine_baseline --repeats 25 --warmups 5
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from app.optimizer import MandatoryTaskSchedulingError, generate_day_schedule
from app.planning.csv_import import parse_legacy_csv_file
from app.planning.models import DaySchedule, TaskRegistry
from app.planning.preferences import OptimizerMode, PreferenceOverrides, resolve_day_preferences

BASE_DIR = Path(__file__).resolve().parent.parent
ANCHOR_DATE = date(2026, 1, 5)
TIMEZONE = "UTC"

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
class ModeResult:
    mode: str
    scheduled_count: int
    unscheduled_count: int
    total_score: float
    generate_time: TimingStats
    mandatory_error: str | None = None


@dataclass
class FixtureResult:
    fixture: str
    fixed_block_count: int
    flexible_task_count: int
    modes: list[ModeResult]


def _timing_stats(samples_seconds: list[float]) -> TimingStats:
    samples_ms = [value * 1000.0 for value in samples_seconds]
    return TimingStats(
        median_ms=round(statistics.median(samples_ms), 4),
        min_ms=round(min(samples_ms), 4),
        max_ms=round(max(samples_ms), 4),
        stdev_ms=round(statistics.pstdev(samples_ms), 4) if len(samples_ms) > 1 else 0.0,
        samples=len(samples_ms),
    )


def _run_mode(day_schedule, prefs, warmups: int, repeats: int) -> ModeResult:
    for _ in range(warmups):
        try:
            generate_day_schedule(day_schedule, prefs)
        except MandatoryTaskSchedulingError:
            pass

    samples: list[float] = []
    last_result = None
    last_error: str | None = None

    for _ in range(repeats):
        t0 = time.perf_counter()
        try:
            last_result = generate_day_schedule(day_schedule, prefs)
            last_error = None
        except MandatoryTaskSchedulingError as exc:
            last_result = None
            last_error = str(exc)
        t1 = time.perf_counter()
        samples.append(t1 - t0)

    return ModeResult(
        mode=prefs.optimizer_mode.value,
        scheduled_count=len(last_result.placements) if last_result else 0,
        unscheduled_count=len(last_result.unscheduled) if last_result else 0,
        total_score=last_result.total_score if last_result else 0.0,
        generate_time=_timing_stats(samples),
        mandatory_error=last_error,
    )



def _load_first_day(path: Path) -> DaySchedule:
    """The fixture's first day as a canonical DaySchedule, imported like the app's own CSV import."""
    parsed = parse_legacy_csv_file(path, anchor_date=ANCHOR_DATE, timezone=TIMEZONE)
    day = parsed.start_date
    tasks = [task for task in parsed.tasks if day in task.preferred_dates]
    return DaySchedule(
        date=day, timezone=TIMEZONE, fixed_blocks=[block for block in parsed.fixed_blocks if block.planned_date == day],
        task_ids=[task.id for task in tasks], tasks=TaskRegistry(tasks={task.id: task for task in tasks}),
    )

def run_benchmark(warmups: int = DEFAULT_WARMUPS, repeats: int = DEFAULT_REPEATS) -> dict:
    results: list[FixtureResult] = []

    for name, path in FIXTURES:
        if not path.exists():
            raise FileNotFoundError(f"benchmark fixture not found: {path}")

        day_schedule = _load_first_day(path)  # single-day fixtures: just the first day

        modes = []
        for mode in (OptimizerMode.PRECISE_GREEDY, OptimizerMode.ADHD_FRIENDLY):
            prefs = resolve_day_preferences(
                date=day_schedule.date, timezone=TIMEZONE,
                date_overrides=PreferenceOverrides(optimizer_mode=mode),
            )
            modes.append(_run_mode(day_schedule, prefs, warmups, repeats))

        results.append(
            FixtureResult(
                fixture=name,
                fixed_block_count=len(day_schedule.fixed_blocks),
                flexible_task_count=len(day_schedule.task_ids),
                modes=modes,
            )
        )

    return {
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "method": {
            "engine": "app.optimizer.generate_day_schedule (Task 4 canonical day engine)",
            "anchor_date": ANCHOR_DATE.isoformat(),
            "timezone": TIMEZONE,
            "warmups": warmups,
            "repeats": repeats,
            "timer": "time.perf_counter",
        },
        "fixtures": [
            {
                "fixture": r.fixture,
                "fixed_block_count": r.fixed_block_count,
                "flexible_task_count": r.flexible_task_count,
                "modes": [asdict(m) for m in r.modes],
            }
            for r in results
        ],
    }


def _print_summary(report: dict) -> None:
    print("Canonical day engine baseline benchmark (Task 4)")
    print(f"  python: {report['environment']['python_version'].splitlines()[0]}")
    print(f"  platform: {report['environment']['platform']}")
    print(f"  warmups={report['method']['warmups']} repeats={report['method']['repeats']}")
    print()

    header = f"{'fixture':<26}{'mode':<16}{'tasks':>7}{'sched':>7}{'unsched':>9}{'score':>9}{'generate(ms)':>15}"
    print(header)
    print("-" * len(header))
    for fixture in report["fixtures"]:
        for mode in fixture["modes"]:
            print(
                f"{fixture['fixture']:<26}{mode['mode']:<16}"
                f"{fixture['flexible_task_count']:>7}"
                f"{mode['scheduled_count']:>7}"
                f"{mode['unscheduled_count']:>9}"
                f"{mode['total_score']:>9.2f}"
                f"{mode['generate_time']['median_ms']:>15.4f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--output", type=Path, default=BASE_DIR / "benchmarks" / "results" / "day_engine_baseline.json"
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
