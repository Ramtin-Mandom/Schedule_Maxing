"""
benchmarks/final_comparison.py

Task 6's final performance review: Greedy Optimizer v1 vs. the canonical
day engine (precise_greedy and adhd_friendly), on the same four fixtures,
under an explicit **baseline-equivalent scoring configuration** -- default
RewardSettings()/RewardPreferences() on both sides (they are defined with
identical field values; see app/planning/preferences.py's RewardPreferences
docstring), no config/task_preference.yaml loaded, no category/task
overrides -- so any remaining score difference reflects what finer
candidate resolution can *find*, not a scoring-configuration difference.

Isolation fix (Task 6 event-search milestone): _run_legacy previously called
legacy_optimize(date=1, day_schedule=...) with no config_path, which lets
app.optimizer.optimize_day_schedule fall through to
app.reward.load_reward_settings(None) -- the *real*, discovered
config/task_preference.yaml, not an isolated default. _run_canonical never
touched YAML at all (resolve_day_preferences builds RewardPreferences()
directly in code). This repository's checked-in config/task_preference.yaml
happens to carry the same scalar weights as RewardSettings()'s own
defaults, so the prior runs' numbers were not actually affected by this gap
-- but the code did not match its own documented "no
config/task_preference.yaml loaded" claim, and would silently stop matching
it the moment anyone edited that file. Both sides now use an explicitly
isolated, guaranteed-empty config (see ISOLATED_CONFIG_PATH), matching what
tests/test_optimizer.py's isolate_reward_config_search fixture already does
for the test suite.

Usage:
    python -m benchmarks.final_comparison
    python -m benchmarks.final_comparison --repeats 25 --warmups 5
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from app.data_processor import load_schedule_from_csv, read_csv_rows
from app.optimizer import generate_day_schedule
from app.optimizer import combine_fixed_and_optimized_scheduled_tasks as legacy_optimize
from app.planning.compat import import_legacy_csv_rows
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
    samples: int


def _timing_stats(samples_seconds: list[float]) -> TimingStats:
    samples_ms = [value * 1000.0 for value in samples_seconds]
    return TimingStats(
        median_ms=round(statistics.median(samples_ms), 4),
        min_ms=round(min(samples_ms), 4),
        max_ms=round(max(samples_ms), 4),
        samples=len(samples_ms),
    )


def _run_legacy(path: Path, warmups: int, repeats: int, isolated_config_path: Path) -> dict:
    for _ in range(warmups):
        schedule_input = load_schedule_from_csv(str(path))
        legacy_optimize(date=1, day_schedule=schedule_input.schedules[1], config_path=isolated_config_path)

    samples = []
    last_output = None
    for _ in range(repeats):
        schedule_input = load_schedule_from_csv(str(path))
        t0 = time.perf_counter()
        last_output = legacy_optimize(date=1, day_schedule=schedule_input.schedules[1], config_path=isolated_config_path)
        t1 = time.perf_counter()
        samples.append(t1 - t0)

    return {
        "scheduled_count": len(last_output.scheduled_tasks),
        "unscheduled_count": len(last_output.unscheduled_tasks),
        "total_score": last_output.total_score,
        "time": asdict(_timing_stats(samples)),
    }


def _run_canonical(path: Path, mode: OptimizerMode, warmups: int, repeats: int) -> dict:
    rows = read_csv_rows(str(path))

    def build():
        imported = import_legacy_csv_rows(rows, anchor_date=ANCHOR_DATE, tz_name=TIMEZONE)
        day_index = min(imported.keys())
        day_schedule = imported[day_index].day_schedule
        # Baseline-equivalent: default preferences, no YAML layer, no
        # category/task overrides -- matches RewardSettings()'s own defaults.
        preferences = resolve_day_preferences(
            date=day_schedule.date, timezone=TIMEZONE, date_overrides=PreferenceOverrides(optimizer_mode=mode)
        )
        return day_schedule, preferences

    for _ in range(warmups):
        day_schedule, preferences = build()
        generate_day_schedule(day_schedule, preferences)

    samples = []
    last_output = None
    for _ in range(repeats):
        day_schedule, preferences = build()
        t0 = time.perf_counter()
        last_output = generate_day_schedule(day_schedule, preferences)
        t1 = time.perf_counter()
        samples.append(t1 - t0)

    return {
        "scheduled_count": len(last_output.placements),
        "unscheduled_count": len(last_output.unscheduled),
        "total_score": last_output.total_score,
        "time": asdict(_timing_stats(samples)),
    }


def run_benchmark(warmups: int = DEFAULT_WARMUPS, repeats: int = DEFAULT_REPEATS) -> dict:
    fixtures_report = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        isolated_config_path = Path(tmp_dir) / "empty_task_preference.yaml"
        isolated_config_path.write_text("{}\n", encoding="utf-8")

        for name, path in FIXTURES:
            legacy = _run_legacy(path, warmups, repeats, isolated_config_path)
            precise = _run_canonical(path, OptimizerMode.PRECISE_GREEDY, warmups, repeats)
            adhd = _run_canonical(path, OptimizerMode.ADHD_FRIENDLY, warmups, repeats)

            fixtures_report.append(
                {
                    "fixture": name,
                    "legacy_greedy_v1": legacy,
                    "canonical_precise_greedy": precise,
                    "canonical_adhd_friendly": adhd,
                    "precise_vs_legacy_ratio": round(precise["time"]["median_ms"] / legacy["time"]["median_ms"], 2),
                    "adhd_vs_legacy_ratio": round(adhd["time"]["median_ms"] / legacy["time"]["median_ms"], 2),
                    "score_delta_precise_minus_legacy": round(precise["total_score"] - legacy["total_score"], 2),
                }
            )

    return {
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "method": {
            "note": (
                "Baseline-equivalent: default RewardSettings()/RewardPreferences() on both sides. Legacy is "
                "pointed at an explicit isolated empty config file (not the real config/task_preference.yaml) "
                "so neither side loads YAML -- see this module's docstring for why that isolation matters even "
                "though this repository's checked-in YAML happens to carry the same scalar values as the "
                "built-in defaults."
            ),
            "warmups": warmups,
            "repeats": repeats,
            "timer": "time.perf_counter",
        },
        "fixtures": fixtures_report,
    }


def _print_summary(report: dict) -> None:
    print("Final performance comparison (Task 6): Greedy Optimizer v1 vs. canonical engine")
    print(f"  python: {report['environment']['python_version'].splitlines()[0]}")
    print(f"  warmups={report['method']['warmups']} repeats={report['method']['repeats']}")
    print()
    header = (
        f"{'fixture':<26}{'legacy(ms)':>12}{'precise(ms)':>13}{'adhd(ms)':>11}"
        f"{'precise/legacy':>16}{'adhd/legacy':>13}{'score delta':>13}"
    )
    print(header)
    print("-" * len(header))
    for fixture in report["fixtures"]:
        print(
            f"{fixture['fixture']:<26}"
            f"{fixture['legacy_greedy_v1']['time']['median_ms']:>12.4f}"
            f"{fixture['canonical_precise_greedy']['time']['median_ms']:>13.4f}"
            f"{fixture['canonical_adhd_friendly']['time']['median_ms']:>11.4f}"
            f"{fixture['precise_vs_legacy_ratio']:>16.2f}"
            f"{fixture['adhd_vs_legacy_ratio']:>13.2f}"
            f"{fixture['score_delta_precise_minus_legacy']:>13.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--output", type=Path, default=BASE_DIR / "benchmarks" / "results" / "final_comparison.json")
    args = parser.parse_args()

    report = run_benchmark(warmups=args.warmups, repeats=args.repeats)
    _print_summary(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2)
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
