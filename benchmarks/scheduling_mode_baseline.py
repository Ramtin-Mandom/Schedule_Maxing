"""
benchmarks/scheduling_mode_baseline.py

Deterministic Normal (precise_greedy) and ADHD (adhd_friendly) scheduling
baseline, captured before the recurrence work of the next milestone so a
later comparison (docs/next-milestone-prompts/06-integration.md) can be made
against a fresh result of the *unchanged* code, never against older
published numbers.

Two scenarios per mode, both built only from fixed inputs -- uuid5-derived
ids, fixed audit timestamps, explicit dates and IANA time zones, an explicit
preference template (independent of config/task_preference.yaml edits) and a
seeded pseudo-random task pool:

    day_engine   app.optimizer.generate_day_schedule on one date's DaySchedule
                 (the canonical day engine, as benchmarks/day_engine_baseline.py)
    workflow     the persisted path every client runs: an in-memory SQLite
                 database, PlanningService with a fixed clock, and
                 app.planning.workflow.generate over a week (allocation, then
                 each date's day engine, then the transactional save)

Each scenario also has an over-committed variant (more work than fits), so
unscheduled and unallocated reasons are part of the baseline.

Recorded: source revision and dirty status, runtime environment and package
versions, the settings used, every placement (task, local interval, score),
every unscheduled/unallocated reason and the total scores. Only the
"results" section is deterministic; timings are informational.

Usage:
    python -m benchmarks.scheduling_mode_baseline                 # write the baseline
    python -m benchmarks.scheduling_mode_baseline --check FILE    # compare a fresh run with FILE
    # A baseline of an exported, unmodified tree (e.g. `git archive <rev>`), without touching the checkout:
    python -m benchmarks.scheduling_mode_baseline --source-root DIR --source-revision REV
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = BASE_DIR / "benchmarks" / "results" / "prompt1_scheduling_mode_baseline.json"

SEED = 20260930
NAMESPACE = uuid.UUID("0b6f3c52-8d0e-4c43-9a51-6a8b8f0e2a11")
CLOCK = datetime(2026, 2, 27, 12, 0, tzinfo=timezone.utc)
WEEK_START = date(2026, 3, 2)  # a Monday; no DST transition in either zone that week
WEEK_END = WEEK_START + timedelta(days=6)
ZONES = ("UTC", "America/New_York")
MODES = ("precise_greedy", "adhd_friendly")
CATEGORIES = ("health", "enjoyment", "study", "work", "chores")


def _id(name: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, name)


def _template():
    from app.planning.preferences import DayWindowSpec, PreferenceOverrides, RewardPreferencesOverride

    return PreferenceOverrides(
        day_window=DayWindowSpec(start_minute=7 * 60, end_minute=23 * 60),
        category_multipliers={"health": 1.2, "study": 1.1, "work": 1.0, "enjoyment": 0.9, "chores": 0.8},
        reward=RewardPreferencesOverride(
            weight_importance=5.0, weight_time_bonus=3.0, weight_tag_relation=2.0,
            weight_fragmentation_penalty=-4.0, weight_category_bonus=1.0, max_time_distance_minutes=240,
            same_tag_window_minutes=120, min_gap_between_tasks_minutes=30,
            short_gap_bonus_weight=0.5, short_gap_bonus_max_minutes=20, short_gap_bonus_cap=2.0,
        ),
    )


def _tasks(zone: str, *, overloaded: bool = False):
    """A seeded week pool: required/dated/deadline/windowed/dependent work (more than fits when overloaded)."""
    from app.planning.models import LocalTimeWindow, Task

    variant = "overloaded" if overloaded else "normal"
    rng = random.Random(f"{SEED}:{zone}:{variant}")
    tasks: list[Task] = []
    for index in range(44 if overloaded else 18):
        day = WEEK_START + timedelta(days=rng.randrange(7))
        kind = index % 6
        fields: dict = {
            "id": _id(f"{zone}:task:{index}"), "name": f"Task {index:02d}", "category": CATEGORIES[index % 5],
            "tags": sorted(rng.sample(["deep", "light", "outdoor", "desk", "call"], k=rng.randint(0, 2))),
            "estimated_duration_minutes": rng.choice([20, 25, 30, 45, 50, 60, 75, 90, 120]),
            "priority": rng.randint(1, 10), "created_at": CLOCK + timedelta(seconds=index),
            "updated_at": CLOCK + timedelta(seconds=index),
        }
        if overloaded:
            # Wednesday is over-committed: pinned work beyond its capacity, never required (so nothing raises).
            fields.update(estimated_duration_minutes=rng.choice([60, 90, 120]))
            if index % 2 == 0:
                fields.update(required_date=WEEK_START + timedelta(days=2))
            else:
                fields.update(preferred_dates=[day])
        elif kind == 0:
            fields.update(required=True, required_date=day)
        elif kind == 1:
            fields.update(preferred_dates=[day])
        elif kind == 2:
            local = datetime.combine(day, datetime.min.time(), ZoneInfo(zone)) + timedelta(hours=rng.randint(12, 22))
            fields.update(deadline=local, preferred_dates=[day])
        elif kind == 3:
            start = rng.choice([8, 9, 13, 17]) * 60
            fields.update(preferred_time_window=LocalTimeWindow(start_minute=start, end_minute=start + 180),
                          preferred_dates=[day])
        elif kind == 4 and tasks:
            fields.update(dependency_ids=[tasks[rng.randrange(len(tasks))].id], preferred_dates=[day])
        else:
            fields.update(preferred_dates=[day])
        tasks.append(Task(**fields))
    return tasks


def _blocks(zone: str):
    from app.planning.models import FixedBlock

    tz = ZoneInfo(zone)
    blocks = []
    for offset in range(7):
        day = WEEK_START + timedelta(days=offset)
        for label, category, start, minutes in (("Lunch", "food", 12 * 60 + 30, 45), ("Class", "event", 15 * 60, 90)):
            if label == "Class" and offset in (5, 6):
                continue
            local = datetime.combine(day, datetime.min.time(), tz) + timedelta(minutes=start)
            blocks.append(FixedBlock(
                id=_id(f"{zone}:block:{day}:{label}"), label=label, category=category, planned_date=day, timezone=zone,
                planned_start=local, planned_end=local + timedelta(minutes=minutes),
                created_at=CLOCK, updated_at=CLOCK,
            ))
    return blocks


def _placement_rows(output) -> list[dict]:
    tz = ZoneInfo(output.timezone)
    return [
        {
            "task_id": str(placement.task_id), "task": output.tasks.get(placement.task_id).name,
            "date": placement.planned_date.isoformat(),
            "start": placement.planned_start.astimezone(tz).strftime("%H:%M"),
            "end": placement.planned_end.astimezone(tz).strftime("%H:%M"),
            "score": round(placement.score, 4),
        }
        for placement in sorted(output.placements, key=lambda p: (p.planned_start, str(p.task_id)))
    ]


def _unscheduled_rows(output) -> list[dict]:
    return sorted(
        ({"task_id": str(entry.task_id), "reason": entry.reason_code.value, "explanation": entry.explanation}
         for entry in output.unscheduled),
        key=lambda row: row["task_id"],
    )


def _day_engine(zone: str, mode: str, *, overloaded: bool = False) -> dict:
    from app.optimizer import MandatoryTaskSchedulingError, generate_day_schedule
    from app.planning.models import DaySchedule, TaskRegistry
    from app.planning.preferences import OptimizerMode, PreferenceOverrides, resolve_day_preferences

    day = WEEK_START + timedelta(days=2)
    pool = _tasks(zone, overloaded=True)[:24] if overloaded else _tasks(zone)[:12]
    tasks = [task.model_copy(update={"dependency_ids": [], "required_date": None, "deadline": None,
                                      "preferred_dates": [], "required": False}) for task in pool]
    schedule = DaySchedule(
        date=day, timezone=zone, fixed_blocks=[block for block in _blocks(zone) if block.planned_date == day],
        task_ids=[task.id for task in tasks], tasks=TaskRegistry(tasks={task.id: task for task in tasks}),
    )
    preferences = resolve_day_preferences(
        date=day, timezone=zone, yaml_overrides=_template(),
        date_overrides=PreferenceOverrides(optimizer_mode=OptimizerMode(mode)),
    )
    started = time.perf_counter()
    try:
        output = generate_day_schedule(schedule, preferences)
    except MandatoryTaskSchedulingError as error:
        return {"date": day.isoformat(), "error": str(error)}
    elapsed = time.perf_counter() - started
    return {
        "date": day.isoformat(), "task_count": len(tasks), "placements": _placement_rows(output),
        "unscheduled": _unscheduled_rows(output), "total_score": output.total_score,
        "_elapsed_ms": round(elapsed * 1000, 3),
    }


def _workflow(zone: str, mode: str, *, overloaded: bool = False) -> dict:
    from app.execution.db import get_connection
    from app.planning import workflow
    from app.planning.application import PlanningService, RangeScope
    from app.planning.preferences import OptimizerMode, PreferenceOverrides
    from app.planning.repository import PlanningRepository

    connection = get_connection(":memory:")
    try:
        ticks = iter(CLOCK + timedelta(milliseconds=step) for step in range(1_000_000))
        service = PlanningService(PlanningRepository(connection), clock=lambda: next(ticks),
                                  preference_template=_template())
        service.create_tasks(_tasks(zone, overloaded=overloaded))
        for block in _blocks(zone):
            service.create_fixed_block(block)
        service.save_user_preferences(PreferenceOverrides(optimizer_mode=OptimizerMode(mode)))
        started = time.perf_counter()
        outcome = workflow.generate(
            service, range_start=WEEK_START, range_end=WEEK_END, scope=RangeScope.PLANNED, timezone_name=zone,
            clock=lambda: CLOCK,
        )
        elapsed = time.perf_counter() - started
        days = {
            day.isoformat(): {"placements": _placement_rows(output), "unscheduled": _unscheduled_rows(output),
                              "total_score": output.total_score}
            for day, output in sorted(outcome.outputs.items())
        }
        allocation = outcome.allocation
        return {
            "status": outcome.status,
            "assignments": {str(task_id): day.isoformat() for task_id, day in sorted(
                allocation.assignments.items(), key=lambda item: str(item[0]))},
            "unallocated": sorted(
                ({"task_id": str(entry.task_id), "reason": entry.reason_code.value, "required": entry.required}
                 for entry in allocation.unallocated), key=lambda row: row["task_id"]),
            "days": days,
            "week_total_score": round(sum(day["total_score"] for day in days.values()), 2),
            "_elapsed_ms": round(elapsed * 1000, 3),
        }
    finally:
        connection.close()


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        return f"unavailable ({error})"


def _versions() -> dict:
    found = {}
    for package in ("pydantic", "SQLAlchemy", "tzdata", "PyYAML"):
        try:
            found[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            found[package] = None
    return found


def run(source: dict | None = None) -> dict:
    results = {
        zone: {mode: {
            "day_engine": _day_engine(zone, mode), "workflow": _workflow(zone, mode),
            "day_engine_overloaded": _day_engine(zone, mode, overloaded=True),
            "workflow_overloaded": _workflow(zone, mode, overloaded=True),
        } for mode in MODES}
        for zone in ZONES
    }
    return {
        "source": source or {"revision": _git("rev-parse", "HEAD"),
                             "dirty_status": _git("status", "--porcelain").splitlines()},
        "environment": {"python": sys.version, "platform": platform.platform(), "packages": _versions()},
        "settings": {
            "seed": SEED, "id_namespace": str(NAMESPACE), "clock": CLOCK.isoformat(),
            "range": [WEEK_START.isoformat(), WEEK_END.isoformat()], "zones": list(ZONES), "modes": list(MODES),
            "range_scope": "planned", "generation_mode": "full",
            "preference_template": _template().model_dump(mode="json"),
        },
        "results": results,
    }


def _deterministic(report: dict) -> dict:
    def strip(value):
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items() if not key.startswith("_")}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return strip(report["results"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", type=Path, help="compare a fresh run's deterministic results with this file")
    parser.add_argument("--source-root", type=Path, help="import the application from this exported tree instead")
    parser.add_argument("--source-revision", help="the revision the exported tree was made from (recorded)")
    args = parser.parse_args()

    source = None
    if args.source_root is not None:
        root = args.source_root.resolve()
        sys.path.insert(0, str(root))
        for name in [name for name in sys.modules if name in ("app", "config") or name.startswith(("app.", "config."))]:
            del sys.modules[name]
        import app.planning.models as planning_models  # `app` is a namespace package: check a real module

        imported_from = Path(planning_models.__file__).resolve().parents[2]
        if imported_from != root:
            raise SystemExit(f"the application was imported from {imported_from}, not from {root}")
        source = {"revision": args.source_revision or "unknown", "tree": "git archive export (unmodified)",
                  "dirty_status": [], "imported_from": str(imported_from)}
    report = run(source)
    if args.check is not None:
        saved = json.loads(args.check.read_text(encoding="utf-8"))
        same = _deterministic(saved) == _deterministic(report)
        print("identical to the saved baseline" if same else "DIFFERENT from the saved baseline")
        return 0 if same else 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for zone in ZONES:
        for mode in MODES:
            entry = report["results"][zone][mode]
            overloaded_day, overloaded_week = entry["day_engine_overloaded"], entry["workflow_overloaded"]
            print(f"{zone:<18}{mode:<16} day={entry['day_engine'].get('total_score')} "
                  f"week={entry['workflow']['week_total_score']} | overloaded day={overloaded_day.get('total_score')} "
                  f"unscheduled={len(overloaded_day.get('unscheduled', []))} week={overloaded_week['week_total_score']} "
                  f"unallocated={len(overloaded_week['unallocated'])}")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
