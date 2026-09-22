"""
app/main.py

CLI entry point. Task 6 / Schedule Maxing v2 makes this a thin canonical
service consumer: it imports a legacy CSV into canonical models (an
explicit anchor date and timezone -- never inferred from today), allocates
every imported task across the CSV's date range, generates exactly one
explicitly selected date via the Day Scheduler, prints it, and exports it
two ways: the existing half-hour-block CSV (kept byte-for-byte compatible,
clearly labeled as legacy/lossy) and a new exact-interval JSON export (the
canonical DayScheduleOutput itself, losslessly serialized, so a 3-minute
task or a 10:13 boundary is never rounded away).

`python -m app.main` with no arguments preserves the original demo: it
loads samples/inputs/valid_single_day_basic.csv (a single day), under a
documented fixed sample anchor date (SAMPLE_ANCHOR_DATE below) and UTC,
schedules it in precise_greedy mode, prints the result, and writes both
export files to samples/outputs/. Every other combination requires
explicit --anchor-date/--timezone: this module never silently treats an
imported legacy day index as "today".

Multi-day CSV input is allocated across its full date range, then only the
explicitly selected date (--select-date, or the CSV's single date when
there is only one) is generated in detail -- never every date automatically.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date as date_
from pathlib import Path

from app.data_processor import read_csv_rows
from app.optimizer import MandatoryTaskSchedulingError, _to_offset
from app.planning.allocation import allocate_tasks
from app.planning.compat import import_legacy_csv_rows
from app.planning.preferences import (
    OptimizerMode,
    PreferenceOverrides,
    day_preferences_overrides_from_reward_settings,
    resolve_day_preferences,
)
from app.planning.service import generate_selected_day
from app.reward import load_reward_settings

#: The documented fixed anchor used only for the zero-argument demo run.
#: Any real legacy import must pass --anchor-date explicitly; this constant
#: exists so the demo is reproducible, not as a hidden default for general use.
SAMPLE_ANCHOR_DATE = date_(2026, 1, 5)
SAMPLE_TIMEZONE = "UTC"


def minutes_to_time(minutes: int) -> str:
    hour = minutes // 60
    minute = minutes % 60

    # Use hour-of-day (mod 24) for the AM/PM calculation so an end time of
    # 1440 (24:00, i.e. midnight) displays as "12:00 AM" instead of "12:00 PM".
    hour_of_day = hour % 24

    suffix = "AM" if hour_of_day < 12 else "PM"
    display_hour = hour_of_day % 12

    if display_hour == 0:
        display_hour = 12

    return f"{display_hour}:{minute:02d} {suffix}"


def minutes_to_24_hour_time(minutes: int) -> str:
    """
    Converts minutes from the start of the day into 24-hour time.

    Example:
        870 -> "14:30"
    """
    hour = minutes // 60
    minute = minutes % 60

    return f"{hour:02d}:{minute:02d}"


def print_day_schedule(date: int, day_output) -> None:
    print("=" * 60)
    print(f"Optimized Schedule for Day {date}")
    print("=" * 60)

    print(f"Total Score: {day_output.total_score:.2f}")
    print()

    scheduled_tasks = sorted(
        day_output.scheduled_tasks,
        key=lambda task: task.time_window.start_time,
    )

    if not scheduled_tasks:
        print("No tasks were scheduled.")
    else:
        print("Scheduled Tasks:")
        print("-" * 60)

        for task in scheduled_tasks:
            start = minutes_to_time(task.time_window.start_time)
            end = minutes_to_time(task.time_window.end_time)

            print(f"{start} - {end}")
            print(f"  Task:     {task.name}")
            print(f"  Category: {task.category}")
            print(f"  Tag:      {task.tag}")
            print(f"  Score:    {task.score:.2f}")
            print("-" * 60)

    if day_output.unscheduled_tasks:
        print()
        print("Unscheduled Tasks:")
        print("-" * 60)

        for task in day_output.unscheduled_tasks:
            print(f"- {task.name}")
            print(f"  reason: {task.reason}")
            print("-" * 60)
    print()


def export_day_schedule_to_csv(day_output, output_path: Path) -> None:
    """
    Exports the final schedule into 30-minute time blocks.

    The CSV contains two columns:
        time, task

    If a task lasts 3 hours, it appears in 6 rows.
    If no task is active during a block, the task value is "-".

    This is the original, established legacy export format -- kept
    byte-for-byte compatible. It is lossy by construction (30-minute
    blocks): a task shorter than 30 minutes, or one that starts off the
    30-minute grid, is not represented exactly. See
    export_exact_schedule_to_json for a lossless alternative.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    scheduled_tasks = sorted(
        day_output.scheduled_tasks,
        key=lambda task: task.time_window.start_time,
    )

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["time", "task"])

        for block_start in range(0, 24 * 60, 30):
            task_name = "-"

            for task in scheduled_tasks:
                task_start = task.time_window.start_time
                task_end = task.time_window.end_time

                if task_start <= block_start < task_end:
                    task_name = task.name
                    break

            writer.writerow([
                minutes_to_24_hour_time(block_start),
                task_name,
            ])

    print(f"Legacy 30-minute-block CSV exported to: {output_path}")


def export_exact_schedule_to_json(canonical_output, output_path: Path) -> None:
    """
    Exact-interval export: the canonical DayScheduleOutput itself,
    losslessly serialized (real dates, aware UTC instants to the minute --
    never rounded to a 30-minute grid), so a 3-minute task or a 10:13
    boundary survives exactly. This is additive: it does not change
    export_day_schedule_to_csv's established column schema at all.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(canonical_output.model_dump_json(indent=2), encoding="utf-8")
    print(f"Exact-interval JSON exported to: {output_path}")


def print_canonical_day_schedule(day_output) -> None:
    print("=" * 60)
    print(f"Generated Schedule for {day_output.date} ({day_output.timezone})")
    print("=" * 60)
    print(f"Total Score: {day_output.total_score:.2f}")
    print()

    placements = sorted(day_output.placements, key=lambda placement: placement.planned_start)
    fixed_blocks = sorted(day_output.fixed_blocks, key=lambda block: block.planned_start)

    if not placements and not fixed_blocks:
        print("No tasks were scheduled.")
    else:
        print("Scheduled:")
        print("-" * 60)
        for block in fixed_blocks:
            print(f"{block.planned_start.isoformat()} - {block.planned_end.isoformat()}")
            print(f"  Fixed:    {block.label}")
            print("-" * 60)
        for placement in placements:
            task = day_output.tasks.get(placement.task_id)
            print(f"{placement.planned_start.isoformat()} - {placement.planned_end.isoformat()}")
            print(f"  Task:     {task.name if task else placement.task_id}")
            print(f"  Category: {task.category if task else '?'}")
            print(f"  Score:    {placement.score:.2f}")
            print("-" * 60)

    if day_output.unscheduled:
        print()
        print("Unscheduled:")
        print("-" * 60)
        for entry in day_output.unscheduled:
            task = day_output.tasks.get(entry.task_id)
            print(f"- {task.name if task else entry.task_id}")
            print(f"  reason [{entry.reason_code.value}]: {entry.explanation}")
            print("-" * 60)
    print()


def _to_legacy_export_view(canonical_output, day_start_utc):
    """
    Adapt a canonical DayScheduleOutput into the small legacy-shaped view
    export_day_schedule_to_csv expects (.scheduled_tasks with
    .name/.time_window.start_time/.end_time as local minutes-from-midnight),
    by looking each placement's name up through the output's own task
    registry -- canonical ScheduledTask intentionally carries no name/
    category of its own (see app/planning/models.py).
    """
    from types import SimpleNamespace

    def _offset(instant):
        return _to_offset(instant, day_start_utc)

    scheduled = []
    for block in canonical_output.fixed_blocks:
        scheduled.append(
            SimpleNamespace(
                name=block.label,
                time_window=SimpleNamespace(start_time=_offset(block.planned_start), end_time=_offset(block.planned_end)),
            )
        )
    for placement in canonical_output.placements:
        task = canonical_output.tasks.get(placement.task_id)
        scheduled.append(
            SimpleNamespace(
                name=task.name if task else str(placement.task_id),
                time_window=SimpleNamespace(start_time=_offset(placement.planned_start), end_time=_offset(placement.planned_end)),
            )
        )

    return SimpleNamespace(scheduled_tasks=scheduled)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=None, help="Legacy schedule CSV to import (default: the sample fixture).")
    parser.add_argument(
        "--anchor-date", type=str, default=None,
        help="Explicit anchor date (YYYY-MM-DD) for legacy day-index -> real-date conversion. "
        "Required for any --csv other than the sample fixture.",
    )
    parser.add_argument("--timezone", type=str, default=None, help="IANA timezone for the imported schedule (default: UTC).")
    parser.add_argument(
        "--mode", type=str, choices=[mode.value for mode in OptimizerMode], default=OptimizerMode.PRECISE_GREEDY.value,
        help="Day Scheduler candidate mode (default: precise_greedy).",
    )
    parser.add_argument(
        "--select-date", type=str, default=None,
        help="Explicit date (YYYY-MM-DD) to generate in detail. Defaults to the CSV's only date when there is "
        "exactly one, otherwise the earliest date (a multi-day CSV is always allocated across its full range "
        "first, but only this one date is ever generated in detail).",
    )
    parser.add_argument("--legacy-csv-out", type=Path, default=None, help="Legacy 30-minute-block CSV export path.")
    parser.add_argument("--exact-json-out", type=Path, default=None, help="Exact-interval canonical JSON export path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    base_dir = Path(__file__).resolve().parent.parent
    args = _parse_args(argv)

    using_sample_default = args.csv is None
    csv_path = args.csv or (base_dir / "samples" / "inputs" / "valid_single_day_basic.csv")

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    if args.anchor_date is not None:
        anchor_date = date_.fromisoformat(args.anchor_date)
    elif using_sample_default:
        anchor_date = SAMPLE_ANCHOR_DATE
    else:
        raise ValueError(
            "--anchor-date is required when importing a CSV other than the built-in sample fixture -- "
            "this module never infers a calendar date for a legacy day index from today's date."
        )

    tz_name = args.timezone or SAMPLE_TIMEZONE
    mode = OptimizerMode(args.mode)

    print(f"Importing {csv_path} with anchor_date={anchor_date} timezone={tz_name!r} (day 1 == {anchor_date}).")
    print(
        "Note: an ID-less legacy CSV receives fresh UUIDs on every import -- re-running this import does not "
        "preserve identity across runs. Use --exact-json-out from a prior run and a canonical JSON loader for "
        "identity-preserving round trips."
    )

    rows = read_csv_rows(str(csv_path))
    imported = import_legacy_csv_rows(rows, anchor_date=anchor_date, tz_name=tz_name)

    for day_index, result in sorted(imported.items()):
        for diagnostic in result.diagnostics:
            print(f"  [import diagnostic] day {day_index}: {diagnostic.message}")

    if not imported:
        print("No schedule days found in the input CSV.")
        return

    reward_yaml_layer = day_preferences_overrides_from_reward_settings(load_reward_settings())

    tasks_registry = None
    fixed_blocks_by_date: dict[date_, list] = {}
    preferences_by_date = {}
    all_task_ids: list = []

    for day_index, result in imported.items():
        day_schedule = result.day_schedule
        if tasks_registry is None:
            tasks_registry = day_schedule.tasks
        else:
            tasks_registry.tasks.update(day_schedule.tasks.tasks)

        fixed_blocks_by_date[day_schedule.date] = day_schedule.fixed_blocks
        preferences_by_date[day_schedule.date] = resolve_day_preferences(
            date=day_schedule.date, timezone=tz_name,
            yaml_overrides=reward_yaml_layer,
            date_overrides=PreferenceOverrides(optimizer_mode=mode),
        )
        all_task_ids.extend(day_schedule.task_ids)

    all_dates = sorted(preferences_by_date.keys())

    allocation = allocate_tasks(
        start_date=all_dates[0], end_date=all_dates[-1], tasks=tasks_registry, task_ids=all_task_ids,
        preferences_by_date=preferences_by_date, fixed_blocks_by_date=fixed_blocks_by_date,
    )
    if allocation.unallocated:
        print(f"Allocation left {len(allocation.unallocated)} task(s) unallocated across [{all_dates[0]}, {all_dates[-1]}]:")
        for entry in allocation.unallocated:
            task = tasks_registry.get(entry.task_id)
            print(f"  - {task.name if task else entry.task_id} [{entry.reason_code.value}]: {entry.explanation}")

    if args.select_date is not None:
        select_date = date_.fromisoformat(args.select_date)
    else:
        select_date = all_dates[0]
        if len(all_dates) > 1:
            print(
                f"Multiple dates imported ({len(all_dates)}); generating only {select_date} "
                "(pass --select-date to choose another)."
            )

    try:
        canonical_output, _state = generate_selected_day(
            allocation, select_date, tasks_registry, preferences_by_date, fixed_blocks_by_date,
        )
    except MandatoryTaskSchedulingError as exc:
        print(f"Could not generate {select_date}: {len(exc.failures)} required task(s) could not be placed.")
        for failure in exc.failures:
            task = tasks_registry.get(failure.task_id)
            print(f"  - {task.name if task else failure.task_id} [{failure.reason_code.value}]: {failure.explanation}")
        raise

    print_canonical_day_schedule(canonical_output)

    day_start_utc, _ = preferences_by_date[select_date].to_local_day_window().to_utc_instants()
    legacy_view = _to_legacy_export_view(canonical_output, day_start_utc)

    # Default output filenames are derived from the actual input CSV's own
    # stem (falling back to the sample fixture's name only when the sample
    # itself is what was imported), so importing a different CSV never
    # silently overwrites the sample's own checked-in output files.
    output_dir = base_dir / "samples" / "outputs"
    output_stem = csv_path.stem
    legacy_csv_out = args.legacy_csv_out or (output_dir / f"{output_stem}.csv")
    exact_json_out = args.exact_json_out or (output_dir / f"{output_stem}.exact.json")

    export_day_schedule_to_csv(legacy_view, legacy_csv_out)
    export_exact_schedule_to_json(canonical_output, exact_json_out)


if __name__ == "__main__":
    main()
