"""
benchmarks/scheduling_modes_comparison.py

Runs the five scheduling modes (docs/scheduling-modes.md) on identical,
deterministic day scenarios and records, per scenario and mode: runtime,
baseline reward B(S) (recomputed), the engine's stored insertion score, the
mode component and objective, scheduled/unscheduled counts and minutes with
reasons, first start, last finish, idle gap minutes, fixed/break minutes and
hard-constraint violations (always expected to be 0).

    python benchmarks/scheduling_modes_comparison.py [--output results.json] [--repeats 5]

Runtime is reported as min/median/max over --repeats identical runs with the
environment (Python, platform). Placements are identical across repeats (the
engine is deterministic), which is checked. A Markdown table is written next
to the JSON (same name, .md).

Objectives of different modes are different quantities and are not
comparable as one quality scale; a greedy result is never claimed globally
optimal. Normal/ADHD compatibility with the pre-change engine is checked
separately by benchmarks/scheduling_mode_baseline.py --check.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.optimizer import MandatoryTaskSchedulingError, evaluate_day_output, generate_day_schedule  # noqa: E402
from app.planning.catch_up import CatchUpEvidence, CategoryEvidence, task_bonuses  # noqa: E402
from app.planning.models import (  # noqa: E402
    DaySchedule,
    FixedBlock,
    LocalTimeWindow,
    Task,
    TaskRegistry,
)
from app.planning.preferences import (  # noqa: E402
    DayWindowSpec,
    OptimizerMode,
    PreferenceOverrides,
    resolve_day_preferences,
)
from app.mode_objectives import mode_weight  # noqa: E402

DAY = date(2026, 3, 2)
START = datetime(2026, 3, 2, tzinfo=timezone.utc)
NAMESPACE = uuid.UUID("5c3e1f9a-7b2d-4c1e-9f0a-000000000005")


def task(name: str, duration: int, **fields) -> Task:
    return Task(id=uuid.uuid5(NAMESPACE, name), name=name, category=fields.pop("category", "study"),
                estimated_duration_minutes=duration, priority=fields.pop("priority", 5), **fields)


def block(label: str, start: int, end: int) -> FixedBlock:
    return FixedBlock(id=uuid.uuid5(NAMESPACE, "block:" + label), label=label, category="break", planned_date=DAY,
                      timezone="UTC", planned_start=START + timedelta(minutes=start),
                      planned_end=START + timedelta(minutes=end))


def window(start: int, end: int) -> LocalTimeWindow:
    return LocalTimeWindow(start_minute=start, end_minute=end)


def scenarios() -> dict:
    ordinary = [task("Read", 60, preferred_time_window=window(540, 600)),
                task("Write", 45, preferred_time_window=window(780, 840)),
                task("Email", 20), task("Review", 30, preferred_time_window=window(1020, 1050))]
    constrained_a = task("Draft", 90, required=True)
    constrained = [constrained_a, task("Edit", 60, dependency_ids=[constrained_a.id]),
                   task("Submit", 15, deadline=START + timedelta(minutes=900)), task("Gym", 45, category="health")]
    workload = [task(f"Occurrence {index}", 25 + (index % 4) * 10, priority=1 + index % 9,
                     category=("study", "work", "health")[index % 3]) for index in range(20)]
    infeasible = [task("Huge A", 400, required=True), task("Huge B", 400, required=True)]
    odd = [task("Tick", 1), task("Odd 13", 13, preferred_time_window=window(613, 640)), task("Odd 47", 47),
           task("Optional too long", 500)]
    return {
        "non_round_minutes_and_unschedulable_optional": (odd, [block("Break", 607, 619)], (600, 1000)),
        "ordinary": (ordinary, [block("Lunch", 720, 750)], (480, 1320)),
        "constrained_breaks_deadlines_dependencies": (
            constrained, [block("Lunch", 720, 780), block("Class", 600, 660)], (480, 1200)),
        "preferences": (ordinary[:2] + [task("Call", 30, preferred_time_window=window(600, 630))], [], (480, 1320)),
        "large_recurring_workload": (workload, [block("Lunch", 720, 780)], (420, 1380)),
        "infeasible_required_work": (infeasible, [block("Busy", 600, 900)], (480, 1200)),
        "sparse_history": (workload[:6], [], (480, 1200)),
    }


#: Catch-Up evidence per scenario (fixed, synthetic): "sparse_history" has too little to act on.
EVIDENCE = {
    "sparse_history": {"health": CategoryEvidence(misses=2, completions=1)},
}
DEFAULT_EVIDENCE = {"health": CategoryEvidence(misses=9, completions=3), "work": CategoryEvidence(misses=1, completions=9)}


def violations(output, blocks) -> int:
    spans = sorted((p.planned_start, p.planned_end) for p in output.placements)
    count = sum(1 for a, b in zip(spans, spans[1:]) if a[1] > b[0])
    count += sum(1 for start, end in spans for b in blocks if start < b.planned_end and b.planned_start < end)
    for placement in output.placements:
        source = output.tasks.get(placement.task_id)
        minutes = (placement.planned_end - placement.planned_start).total_seconds() / 60
        if source is not None and minutes != source.estimated_duration_minutes:
            count += 1
        if source is not None and source.deadline is not None and placement.planned_end > source.deadline:
            count += 1
    return count


def run(repeats: int = 5) -> dict:
    results: dict = {}
    for name, (tasks, blocks, (lo, hi)) in scenarios().items():
        registry = TaskRegistry()
        for item in tasks:
            registry.add(item)
        schedule = DaySchedule(date=DAY, timezone="UTC", fixed_blocks=blocks, task_ids=[t.id for t in tasks],
                               tasks=registry)
        results[name] = {}
        for mode in OptimizerMode:
            prefs = resolve_day_preferences(date=DAY, timezone="UTC", date_overrides=PreferenceOverrides(
                optimizer_mode=mode, day_window=DayWindowSpec(start_minute=lo, end_minute=hi)))
            bonuses = None
            if mode == OptimizerMode.CATCH_UP:
                evidence = CatchUpEvidence(as_of=START, lookback_days=90,
                                           by_category=EVIDENCE.get(name, DEFAULT_EVIDENCE))
                bonuses, _ = task_bonuses(evidence, tasks, mode_weight(5.0))
            runtimes, outputs, failure = [], [], None
            for _ in range(repeats):
                started = time.perf_counter()
                try:
                    outputs.append(generate_day_schedule(schedule, prefs, task_bonuses=bonuses))
                except MandatoryTaskSchedulingError as error:
                    failure = error
                runtimes.append((time.perf_counter() - started) * 1000)
            runtime = {"min": round(min(runtimes), 3), "median": round(statistics.median(runtimes), 3),
                       "max": round(max(runtimes), 3), "runs": repeats}
            if failure is not None:
                results[name][mode.value] = {
                    "runtime_ms": runtime, "status": "infeasible (required work cannot fit)",
                    "reasons": sorted({f.reason_code.value for f in failure.failures})}
                continue
            output = outputs[0]
            deterministic = all([(p.task_id, p.planned_start, p.planned_end) for p in other.placements]
                                == [(p.task_id, p.planned_start, p.planned_end) for p in output.placements]
                                for other in outputs[1:])
            evaluation = evaluate_day_output(output, prefs, task_bonuses=bonuses)
            results[name][mode.value] = {
                "runtime_ms": runtime, "deterministic": deterministic, "status": "generated",
                "baseline_reward": round(evaluation.baseline_reward, 4),
                "stored_insertion_score": round(evaluation.stored_score, 4),
                "adhd_component": round(evaluation.adhd_component, 4),
                "mode_component": round(evaluation.mode_bonus, 4), "objective": round(evaluation.objective, 4),
                "scheduled": evaluation.scheduled_count, "scheduled_minutes": evaluation.scheduled_minutes,
                "unscheduled": len(output.unscheduled),
                "unscheduled_minutes": sum(registry.get(e.task_id).estimated_duration_minutes
                                           for e in output.unscheduled),
                "unscheduled_reasons": sorted({e.reason_code.value for e in output.unscheduled}),
                # Minutes into the day window; null when nothing was scheduled.
                "first_start_minute": evaluation.first_start_minute,
                "last_finish_minute": evaluation.last_finish_minute,
                "idle_gap_minutes": evaluation.idle_minutes, "fixed_minutes": evaluation.fixed_minutes,
                "constraint_violations": violations(output, blocks),
            }
    return results


COLUMNS = ("status", "scheduled", "scheduled_minutes", "unscheduled", "first_start_minute", "last_finish_minute",
           "idle_gap_minutes", "fixed_minutes", "baseline_reward", "mode_component", "objective",
           "constraint_violations")


def table(data: dict) -> str:
    lines = ["# Scheduling modes comparison", "", data["note"], "",
             f"Environment: {data['environment']}", ""]
    for scenario, modes in data["scenarios"].items():
        lines += [f"## {scenario}", "", "| mode | median ms | " + " | ".join(COLUMNS) + " |",
                  "|" + "---|" * (len(COLUMNS) + 2)]
        for mode, row in modes.items():
            cells = [str(row.get(column, "—") if row.get(column) is not None else "—") for column in COLUMNS]
            lines.append(f"| {mode} | {row['runtime_ms']['median']} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path,
                        default=ROOT / "benchmarks" / "results" / "prompt5_scheduling_modes_comparison.json")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    data = {"note": "Objectives of different modes are not comparable on one scale; no global optimum is claimed. "
                    "Normal/ADHD equality with the pre-change engine: benchmarks/scheduling_mode_baseline.py --check.",
            "environment": f"Python {platform.python_version()} on {platform.platform()}",
            "scenarios": run(args.repeats)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(table(data) + "\n", encoding="utf-8")
    print(f"wrote {args.output} and {args.output.with_suffix('.md')}")


if __name__ == "__main__":
    main()
