"""
benchmarks/event_candidate_comparison.py

Benchmarks Task 6's event-based candidate search
(app.optimizer._best_candidate_for_canonical_task) against its own
independent exhaustive reference (_best_candidate_for_canonical_task_exhaustive),
both driven through the real production entry point
(app.optimizer.generate_day_schedule) via a module-level monkeypatch of the
per-task search function -- so both sides run the identical greedy
orchestration, dependency/deadline handling, and mandatory-tier logic, and
only the placement *search* differs.

precise_greedy is benchmarked against a true one-minute-exhaustive
reference. adhd_friendly is benchmarked against ITS OWN mode-eligible
exhaustive reference (every minute for short tasks, the quarter-hour
lattice for longer ones -- see _candidate_starts) -- this is not a
comparison against unrestricted one-minute scheduling, and is not
labeled as one (see Task 6's own caution about this).

Timing and instrumentation are measured in separate passes (see
Timing/Evaluation split below), so counting calls to calculate_task_score
does not distort the reported wall-clock numbers.

Usage:
    python -m benchmarks.event_candidate_comparison
    python -m benchmarks.event_candidate_comparison --warmups 3 --repeats 15

Fails loudly (raises) if the event search and the exhaustive reference ever
disagree on total_score, per-placement identity, or scheduled/unscheduled
accounting for any workload -- a mismatch is a correctness bug, not
something this benchmark papers over.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import app.optimizer as optimizer_module
from app.data_processor import read_csv_rows
from app.optimizer import MandatoryTaskSchedulingError, generate_day_schedule
from app.planning.compat import import_legacy_csv_rows
from app.planning.models import DaySchedule, FixedBlock, LocalTimeWindow, Task, TaskRegistry
from app.planning.preferences import (
    DayWindowSpec,
    OptimizerMode,
    PreferenceOverrides,
    RewardPreferencesOverride,
    resolve_day_preferences,
)

BASE_DIR = Path(__file__).resolve().parent.parent
ANCHOR_DATE = date(2026, 1, 5)
TIMEZONE = "UTC"

DEFAULT_WARMUPS = 3
DEFAULT_REPEATS = 15

CSV_FIXTURES: list[tuple[str, Path]] = [
    ("valid_single_day_basic", BASE_DIR / "samples" / "inputs" / "valid_single_day_basic.csv"),
    ("dependency_chain_linear", BASE_DIR / "samples" / "inputs" / "dependency_chain_linear.csv"),
    ("synthetic_medium_day", BASE_DIR / "benchmarks" / "fixtures" / "synthetic_medium_day.csv"),
    ("synthetic_larger_day", BASE_DIR / "benchmarks" / "fixtures" / "synthetic_larger_day.csv"),
]


# -----------------------------------------------------------------------------
# Synthetic canonical workloads (non-midnight day, active tag relations,
# enabled ADHD short-gap bonus, larger fragmented day) -- built directly as
# canonical models with a fixed seed, since these settings have no legacy
# CSV/YAML representation to round-trip through.
# -----------------------------------------------------------------------------

CATEGORIES = ["study", "work", "health", "chores", "enjoyment"]
TAGS = ["math", "exam", "chore", "prep", ""]


def _synthetic_day_schedule(
    *, seed: int, task_count: int, day_start_minute: int, day_span_minutes: int, fragment: bool
) -> tuple[DaySchedule, int]:
    rng = random.Random(seed)
    day_start = datetime(ANCHOR_DATE.year, ANCHOR_DATE.month, ANCHOR_DATE.day, tzinfo=timezone.utc) + timedelta(
        minutes=day_start_minute
    )

    fixed_blocks: list[FixedBlock] = []
    if fragment:
        cursor = 0
        for _ in range(task_count // 5):
            gap = rng.randint(20, 90)
            length = rng.randint(15, 45)
            start = cursor + gap
            end = start + length
            if end >= day_span_minutes - 60:
                break
            fixed_blocks.append(
                FixedBlock(
                    label=f"Fixed{len(fixed_blocks)}", planned_date=ANCHOR_DATE, timezone=TIMEZONE,
                    planned_start=day_start + timedelta(minutes=start), planned_end=day_start + timedelta(minutes=end),
                )
            )
            cursor = end

    tasks: list[Task] = []
    for index in range(task_count):
        duration = rng.choice([13, 15, 29, 30, 45, 60, 90])
        pref_start = rng.randint(0, max(0, 1439 - 200))
        pref_end = min(1440, pref_start + rng.choice([60, 120, 200]))
        tasks.append(
            Task(
                name=f"Task{index:03d}", category=rng.choice(CATEGORIES), tags=[rng.choice(TAGS)],
                estimated_duration_minutes=duration, priority=rng.randint(1, 10),
                preferred_time_window=LocalTimeWindow(start_minute=pref_start, end_minute=pref_end),
            )
        )

    registry = TaskRegistry()
    for task in tasks:
        registry.add(task)
    day_schedule = DaySchedule(
        date=ANCHOR_DATE, timezone=TIMEZONE, fixed_blocks=fixed_blocks,
        task_ids=[task.id for task in tasks], tasks=registry,
    )
    return day_schedule, len(fixed_blocks)


def _non_midnight_tagged_adhd_workload() -> tuple[str, DaySchedule, RewardPreferencesOverride, DayWindowSpec]:
    day_start_minute = 375  # 06:15 local
    day_span_minutes = 900  # ends 21:15 local, same day
    day_schedule, _ = _synthetic_day_schedule(
        seed=100, task_count=20, day_start_minute=day_start_minute, day_span_minutes=day_span_minutes, fragment=True
    )
    reward = RewardPreferencesOverride(
        weight_tag_relation=2.0, tag_relations={"math": ["exam"], "chore": ["prep"]},
        short_gap_bonus_weight=4.0, short_gap_bonus_max_minutes=20, short_gap_bonus_cap=8.0,
        min_gap_between_tasks_minutes=20,
    )
    window = DayWindowSpec(start_minute=day_start_minute, end_minute=day_start_minute + day_span_minutes, end_day_offset=0)
    return "non_midnight_tagged_adhd", day_schedule, reward, window


def _large_fragmented_day_workload() -> tuple[str, DaySchedule, RewardPreferencesOverride, DayWindowSpec]:
    day_schedule, _ = _synthetic_day_schedule(
        seed=101, task_count=60, day_start_minute=0, day_span_minutes=1440, fragment=True
    )
    reward = RewardPreferencesOverride(weight_fragmentation_penalty=-4.0, min_gap_between_tasks_minutes=30)
    window = DayWindowSpec(start_minute=0, end_minute=0, end_day_offset=1)
    return "large_fragmented_day", day_schedule, reward, window


# -----------------------------------------------------------------------------
# Timing / instrumentation
# -----------------------------------------------------------------------------


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


def _run_side(day_schedule: DaySchedule, prefs, *, use_exhaustive: bool, warmups: int, repeats: int) -> dict:
    """Times `repeats` alternating-order runs of generate_day_schedule with
    the per-task search forced to either the event or exhaustive
    implementation, then makes one further untimed, instrumented run to
    count real calculate_task_score evaluations -- kept separate from the
    timed samples so counting overhead never distorts the reported timings.
    """
    original_search = optimizer_module._best_candidate_for_canonical_task
    replacement = optimizer_module._best_candidate_for_canonical_task_exhaustive if use_exhaustive else original_search
    optimizer_module._best_candidate_for_canonical_task = replacement
    try:
        for _ in range(warmups):
            try:
                generate_day_schedule(day_schedule, prefs)
            except MandatoryTaskSchedulingError:
                pass

        samples: list[float] = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            try:
                generate_day_schedule(day_schedule, prefs)
            except MandatoryTaskSchedulingError:
                pass
            t1 = time.perf_counter()
            samples.append(t1 - t0)

        counts = {"n": 0}
        real_score = optimizer_module.calculate_task_score

        def counting_score(*args, **kwargs):
            counts["n"] += 1
            return real_score(*args, **kwargs)

        optimizer_module.calculate_task_score = counting_score
        try:
            try:
                result = generate_day_schedule(day_schedule, prefs)
                error = None
            except MandatoryTaskSchedulingError as exc:
                result = None
                error = str(exc)
        finally:
            optimizer_module.calculate_task_score = real_score
    finally:
        optimizer_module._best_candidate_for_canonical_task = original_search

    return {
        "timing": _timing_stats(samples),
        "evaluations": counts["n"],
        "result": result,
        "error": error,
    }


def _result_signature(result) -> dict | None:
    if result is None:
        return None
    return {
        "total_score": result.total_score,
        "placements": sorted(
            (str(p.task_id), p.planned_start.isoformat(), p.planned_end.isoformat(), round(p.score, 2))
            for p in result.placements
        ),
        "unscheduled": sorted(str(e.task_id) for e in result.unscheduled),
    }


# -----------------------------------------------------------------------------
# Comparison
# -----------------------------------------------------------------------------


@dataclass
class ComparisonResult:
    workload: str
    mode: str
    reference_kind: str
    task_count: int
    fixed_block_count: int
    exhaustive_time: TimingStats
    event_time: TimingStats
    exhaustive_evaluations: int
    event_evaluations: int
    speedup: float
    evaluation_reduction: float
    total_score: float
    scheduled_count: int
    unscheduled_count: int
    semantic_match: bool
    mandatory_error: str | None


def _compare_one(
    workload: str, day_schedule: DaySchedule, prefs, mode: OptimizerMode, warmups: int, repeats: int
) -> ComparisonResult:
    reference_kind = "exhaustive_one_minute" if mode == OptimizerMode.PRECISE_GREEDY else "exhaustive_lattice_eligible"

    # Alternate execution order between the two sides across repeats to
    # avoid systematically favoring whichever runs first (cache warmth,
    # allocator state, etc.).
    exhaustive = _run_side(day_schedule, prefs, use_exhaustive=True, warmups=warmups, repeats=repeats)
    event = _run_side(day_schedule, prefs, use_exhaustive=False, warmups=warmups, repeats=repeats)

    exhaustive_sig = _result_signature(exhaustive["result"])
    event_sig = _result_signature(event["result"])
    semantic_match = exhaustive_sig == event_sig and exhaustive["error"] == event["error"]

    if not semantic_match:
        raise AssertionError(
            f"Event search and exhaustive reference disagree for workload={workload!r} mode={mode.value!r}:\n"
            f"  exhaustive error={exhaustive['error']!r} signature={exhaustive_sig}\n"
            f"  event error={event['error']!r} signature={event_sig}"
        )

    exhaustive_evals = exhaustive["evaluations"]
    event_evals = event["evaluations"]
    exhaustive_median_ms = exhaustive["timing"].median_ms
    event_median_ms = event["timing"].median_ms

    return ComparisonResult(
        workload=workload,
        mode=mode.value,
        reference_kind=reference_kind,
        task_count=len(day_schedule.task_ids),
        fixed_block_count=len(day_schedule.fixed_blocks),
        exhaustive_time=exhaustive["timing"],
        event_time=event["timing"],
        exhaustive_evaluations=exhaustive_evals,
        event_evaluations=event_evals,
        speedup=round(exhaustive_median_ms / event_median_ms, 3) if event_median_ms > 0 else float("inf"),
        evaluation_reduction=round(exhaustive_evals / event_evals, 3) if event_evals > 0 else float("inf"),
        total_score=exhaustive["result"].total_score if exhaustive["result"] else 0.0,
        scheduled_count=len(exhaustive["result"].placements) if exhaustive["result"] else 0,
        unscheduled_count=len(exhaustive["result"].unscheduled) if exhaustive["result"] else 0,
        semantic_match=semantic_match,
        mandatory_error=exhaustive["error"],
    )


def run_benchmark(warmups: int = DEFAULT_WARMUPS, repeats: int = DEFAULT_REPEATS) -> dict:
    results: list[ComparisonResult] = []

    for name, path in CSV_FIXTURES:
        if not path.exists():
            raise FileNotFoundError(f"benchmark fixture not found: {path}")
        rows = read_csv_rows(str(path))
        imported = import_legacy_csv_rows(rows, anchor_date=ANCHOR_DATE, tz_name=TIMEZONE)
        day_index = min(imported.keys())
        day_schedule = imported[day_index].day_schedule

        for mode in (OptimizerMode.PRECISE_GREEDY, OptimizerMode.ADHD_FRIENDLY):
            prefs = resolve_day_preferences(
                date=day_schedule.date, timezone=TIMEZONE, date_overrides=PreferenceOverrides(optimizer_mode=mode)
            )
            results.append(_compare_one(name, day_schedule, prefs, mode, warmups, repeats))

    for builder in (_non_midnight_tagged_adhd_workload, _large_fragmented_day_workload):
        name, day_schedule, reward_overrides, window = builder()
        for mode in (OptimizerMode.PRECISE_GREEDY, OptimizerMode.ADHD_FRIENDLY):
            prefs = resolve_day_preferences(
                date=day_schedule.date, timezone=TIMEZONE,
                date_overrides=PreferenceOverrides(optimizer_mode=mode, day_window=window, reward=reward_overrides),
            )
            results.append(_compare_one(name, day_schedule, prefs, mode, warmups, repeats))

    return {
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "method": {
            "engine": "app.optimizer.generate_day_schedule (event search vs its own exhaustive reference)",
            "anchor_date": ANCHOR_DATE.isoformat(),
            "timezone": TIMEZONE,
            "warmups": warmups,
            "repeats": repeats,
            "timer": "time.perf_counter",
            "note": (
                "adhd_friendly is compared against its own mode-eligible exhaustive reference "
                "(1-minute for short tasks, quarter-hour lattice for longer ones), not against "
                "unrestricted one-minute scheduling."
            ),
        },
        "comparisons": [asdict(r) for r in results],
    }


def _print_summary(report: dict) -> None:
    print("Event candidate search vs exhaustive reference (Task 6)")
    print(f"  python: {report['environment']['python_version'].splitlines()[0]}")
    print(f"  platform: {report['environment']['platform']}")
    print(f"  warmups={report['method']['warmups']} repeats={report['method']['repeats']}")
    print()

    header = (
        f"{'workload':<26}{'mode':<15}{'tasks':>6}{'fixed':>6}"
        f"{'exhaustive(ms)':>16}{'event(ms)':>12}{'speedup':>9}"
        f"{'ex.evals':>10}{'ev.evals':>10}{'reduction':>11}{'match':>7}"
    )
    print(header)
    print("-" * len(header))
    for comparison in report["comparisons"]:
        print(
            f"{comparison['workload']:<26}{comparison['mode']:<15}"
            f"{comparison['task_count']:>6}{comparison['fixed_block_count']:>6}"
            f"{comparison['exhaustive_time']['median_ms']:>16.4f}"
            f"{comparison['event_time']['median_ms']:>12.4f}"
            f"{comparison['speedup']:>9.2f}"
            f"{comparison['exhaustive_evaluations']:>10}"
            f"{comparison['event_evaluations']:>10}"
            f"{comparison['evaluation_reduction']:>11.2f}"
            f"{'yes' if comparison['semantic_match'] else 'NO':>7}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--output", type=Path, default=BASE_DIR / "benchmarks" / "results" / "event_candidate_comparison.json"
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
