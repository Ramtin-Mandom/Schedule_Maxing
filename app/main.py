"""
app/main.py

CLI entry point (Milestone 2): a thin consumer of the persistence-backed
planning service. Like the desktop app it reads and writes the application
SQLite database -- by default the per-user data location (see
config.settings; SCHEDULE_MAXING_DATA_DIR overrides it), or the file given
with --db-path -- and never imports anything implicitly.

    python -m app.main [--db-path FILE]
        Summarize what is stored. Nothing is imported or changed.

    python -m app.main [--db-path FILE] --select-date YYYY-MM-DD [--start-date D --end-date D]
        Allocate the stored tasks planned in [start, end] (default: just the
        selected date), generate exactly the selected date with the Day
        Scheduler, save its placements (reusing unchanged placement ids),
        print it, and optionally export it.

    python -m app.main --import-csv FILE --anchor-date YYYY-MM-DD [--import-mode append|replace] ...
        Import a legacy CSV through the transactional importer
        (app/planning/csv_import.py: the whole file is validated first, then
        written in one transaction), then schedule the selected date (default:
        the file's first date) over the file's date range. --csv is kept as
        an alias of --import-csv. The anchor date is always explicit.

    python -m app.main --demo
        The explicit sample: imports samples/inputs/valid_single_day_basic.csv
        at a fixed anchor date into a temporary in-memory database (or into
        --db-path if one is given) and schedules it. Plain startup never adds
        sample data.

    --import-csv also accepts a stored-planning CSV written by
    --export-planning-csv (it has record ids): that file is merged by id
    (app/planning/csv_canonical.py) instead -- unchanged records are
    skipped, a record that differs from the saved one is refused unless
    --allow-updates is given *and* its version matches the saved version.

    --mode sets the Day Scheduler mode as the saved user preference (it is
    kept for later runs and by the desktop app); without --mode the saved
    preference, or the default precise_greedy, is used.

Exports: --legacy-csv-out writes the original `time,task` 30-minute-block
CSV (byte-for-byte compatible, lossy: shorter or off-grid intervals and ids
cannot be represented); --exact-json-out writes the generated
DayScheduleOutput losslessly; --export-planning-csv writes the stored
planning CSV (app/planning/csv_export.py: tasks, fixed blocks, and exact
placement intervals with ids) for the scheduled range -- or everything,
when nothing is scheduled. For --demo and CSV imports, the first two
default to samples/outputs/<csv stem>.* as before.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date as date_
from pathlib import Path

from app.execution.db import get_connection, resolve_db_path
from app.optimizer import _to_offset
from app.planning.application import PlanningService, RangeScope
from app.planning.csv_canonical import is_canonical_csv, parse_canonical_csv
from app.planning.csv_import import CsvImportError, ImportMode, parse_legacy_csv_file, read_csv_text
from app.planning.preferences import OptimizerMode
from app.planning.repository import PlanningRepository
from app.ui.planning_controller import PlanningController
from config import settings

#: The documented fixed anchor used only by the explicit --demo run. Any real
#: legacy import must pass --anchor-date explicitly; this constant exists so
#: the demo is reproducible, not as a hidden default for general use.
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db-path", type=Path, default=None,
        help="Application database file (default: the per-user data location; --demo alone uses a temporary "
        "in-memory database).",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Explicitly import and schedule the sample fixture (never done implicitly).",
    )
    parser.add_argument(
        "--import-csv", "--csv", dest="import_csv", type=Path, default=None,
        help="Legacy schedule CSV to import (validated completely, written in one transaction).",
    )
    parser.add_argument(
        "--import-mode", choices=[mode.value for mode in ImportMode], default=ImportMode.APPEND.value,
        help="append: add the file's rows (default). replace: first clear the dates the file covers.",
    )
    parser.add_argument(
        "--anchor-date", type=str, default=None,
        help="Explicit date (YYYY-MM-DD) of legacy day 1. Required with --import-csv.",
    )
    parser.add_argument(
        "--timezone", type=str, default=None,
        help=f"IANA timezone for imported/scheduled days (default: {settings.DEFAULT_TIMEZONE}).",
    )
    parser.add_argument(
        "--mode", type=str, choices=[mode.value for mode in OptimizerMode], default=None,
        help="Day Scheduler candidate mode, saved as your preference (default: the saved preference, else "
        "precise_greedy).",
    )
    parser.add_argument(
        "--allow-updates", action="store_true",
        help="For a stored-planning CSV import: apply records that differ from the saved ones when their version "
        "matches the saved version (otherwise such a file is refused).",
    )
    parser.add_argument(
        "--select-date", type=str, default=None,
        help="The one date (YYYY-MM-DD) to generate in detail. Defaults to an import's first date; without an "
        "import and without this option nothing is scheduled.",
    )
    parser.add_argument("--start-date", type=str, default=None, help="Allocation range start (default: see above).")
    parser.add_argument("--end-date", type=str, default=None, help="Allocation range end (default: see above).")
    parser.add_argument("--legacy-csv-out", type=Path, default=None, help="Legacy 30-minute-block CSV export path.")
    parser.add_argument("--exact-json-out", type=Path, default=None, help="Exact-interval canonical JSON export path.")
    parser.add_argument(
        "--export-planning-csv", type=Path, default=None,
        help="Write the stored planning CSV (tasks, fixed blocks, exact placements with ids).",
    )
    return parser.parse_args(argv)


def _date_arg(value: str | None, option: str) -> date_ | None:
    if value is None:
        return None
    try:
        return date_.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{option} must be YYYY-MM-DD, got {value!r}") from error


def _print_stored_summary(service: PlanningService) -> None:
    tasks = service.list_tasks()
    blocks = service.list_fixed_blocks()
    placements = service.list_placements()
    print(f"Stored: {len(tasks)} task(s), {len(blocks)} fixed block(s), {len(placements)} scheduled placement(s).")
    scheduled_dates = sorted({placement.planned_date for placement in placements})
    if scheduled_dates:
        print(f"Scheduled dates: {scheduled_dates[0]} .. {scheduled_dates[-1]} ({len(scheduled_dates)} date(s)).")
    print("Nothing was scheduled. Pass --select-date YYYY-MM-DD to generate and save one date.")


def main(argv: list[str] | None = None) -> None:
    base_dir = Path(__file__).resolve().parent.parent
    args = _parse_args(argv)

    if args.demo and args.import_csv is not None:
        raise ValueError("use either --demo or --import-csv, not both")
    csv_path = base_dir / "samples" / "inputs" / "valid_single_day_basic.csv" if args.demo else args.import_csv
    if csv_path is not None and not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    canonical = csv_path is not None and not args.demo and is_canonical_csv(read_csv_text(csv_path))
    if args.anchor_date is not None:
        anchor_date = _date_arg(args.anchor_date, "--anchor-date")
    elif args.demo:
        anchor_date = SAMPLE_ANCHOR_DATE
    elif csv_path is not None and not canonical:
        raise ValueError(
            "--anchor-date is required when importing a CSV -- this program never infers a calendar date "
            "for a legacy day index from today's date."
        )
    else:
        anchor_date = None

    tz_name = args.timezone or (SAMPLE_TIMEZONE if args.demo else settings.DEFAULT_TIMEZONE)
    mode = OptimizerMode(args.mode) if args.mode is not None else None
    select_date = _date_arg(args.select_date, "--select-date")
    start_date = _date_arg(args.start_date, "--start-date")
    end_date = _date_arg(args.end_date, "--end-date")
    db_path = args.db_path if args.db_path is not None else (":memory:" if args.demo else None)

    connection = get_connection(db_path)
    try:
        service = PlanningService(PlanningRepository(connection))
        controller = PlanningController(service=service, timezone=tz_name)
        print(f"Database: {resolve_db_path(db_path)}")
        if mode is not None:
            saved_mode = controller.set_engine_mode(mode)
            if not saved_mode.ok:
                print(f"Could not save the scheduler mode: {saved_mode.error}")
                raise SystemExit(1)
            print(f"Scheduler mode {mode.value} saved as your preference.")

        imported = None
        if csv_path is not None and canonical:
            _import_canonical(controller, csv_path, ImportMode(args.import_mode), allow_updates=args.allow_updates)
        elif csv_path is not None:
            imported = _import(controller, csv_path, anchor_date, tz_name, ImportMode(args.import_mode))

        if select_date is None and imported is not None:
            select_date = imported.start_date
            if imported.end_date > imported.start_date:
                print(
                    f"The file covers {imported.start_date} .. {imported.end_date}; generating only {select_date} "
                    "(pass --select-date to choose another)."
                )

        if select_date is None:
            _print_stored_summary(service)
            if args.export_planning_csv is not None:
                _export_planning(controller, args.export_planning_csv, None, None)
            return

        start_date = start_date or (imported.start_date if imported is not None else select_date)
        end_date = end_date or (imported.end_date if imported is not None else select_date)
        if not start_date <= select_date <= end_date:
            raise ValueError(f"--select-date {select_date} must lie within the range {start_date} .. {end_date}")

        allocation = controller.allocate_range(start_date, end_date, scope=RangeScope.PLANNED)
        if not allocation.ok:
            print(f"Allocation failed: {allocation.error}")
            raise SystemExit(1)
        registry = service.get_tasks(entry.task_id for entry in allocation.value.unallocated)
        if allocation.value.unallocated:
            print(f"Allocation left {len(allocation.value.unallocated)} task(s) unallocated across [{start_date}, {end_date}]:")
            for entry in allocation.value.unallocated:
                task = registry.get(entry.task_id)
                print(f"  - {task.name if task else entry.task_id} [{entry.reason_code.value}]: {entry.explanation}")

        generated = controller.generate_day(select_date)
        if not generated.ok:
            print(f"{generated.error}\nNothing was saved for {select_date}; its previously saved schedule is unchanged.")
            raise SystemExit(1)
        canonical_output = generated.value
        print(f"Saved the schedule for {select_date}.")
        print_canonical_day_schedule(canonical_output)

        day_start_utc, _ = controller.resolve_preferences(select_date).value.to_local_day_window().to_utc_instants()
        legacy_view = _to_legacy_export_view(canonical_output, day_start_utc)

        # For an import, default output filenames are derived from the input
        # CSV's own stem (as before), so importing a different CSV never
        # overwrites the sample's checked-in outputs. Without an import,
        # nothing is written unless an output path is given.
        output_dir = base_dir / "samples" / "outputs"
        legacy_csv_out = args.legacy_csv_out or (output_dir / f"{csv_path.stem}.csv" if csv_path else None)
        exact_json_out = args.exact_json_out or (output_dir / f"{csv_path.stem}.exact.json" if csv_path else None)
        if legacy_csv_out is not None:
            export_day_schedule_to_csv(legacy_view, legacy_csv_out)
        if exact_json_out is not None:
            export_exact_schedule_to_json(canonical_output, exact_json_out)
        if args.export_planning_csv is not None:
            _export_planning(controller, args.export_planning_csv, start_date, end_date)
    finally:
        connection.close()


def _import(controller: PlanningController, csv_path: Path, anchor_date: date_, tz_name: str, mode: ImportMode):
    print(f"Importing {csv_path} ({mode.value}) with anchor_date={anchor_date} timezone={tz_name!r} (day 1 == {anchor_date}).")
    print(
        "Note: a legacy CSV has no ids -- every import creates new tasks and fixed blocks, so appending the "
        "same file twice stores it twice. Use --import-mode replace to replace the dates it covers."
    )
    try:
        parsed = parse_legacy_csv_file(csv_path, anchor_date=anchor_date, timezone=tz_name)
    except CsvImportError as error:
        print(error)
        raise SystemExit(2) from error

    applied = controller.apply_import(parsed, mode)
    if not applied.ok:
        print(f"The CSV was not imported; nothing was saved: {applied.error}")
        raise SystemExit(2)
    result = applied.value
    print(f"Imported {len(result.tasks)} task(s) and {len(result.fixed_blocks)} fixed block(s).")
    if result.cleared is not None:
        cleared = result.cleared
        print(
            f"Replaced {parsed.start_date} .. {parsed.end_date}: removed {cleared.deleted_tasks} task(s), "
            f"{cleared.deleted_fixed_blocks} fixed block(s), {cleared.deleted_placements} placement(s); "
            "execution history was kept."
        )
    return parsed


def _import_canonical(controller: PlanningController, csv_path: Path, mode: ImportMode, *, allow_updates: bool) -> None:
    print(f"Importing the stored-planning CSV {csv_path}, merged by record id (the anchor date does not apply).")
    try:
        parse_canonical_csv(read_csv_text(csv_path))  # report format problems with line numbers first
    except CsvImportError as error:
        print(error)
        raise SystemExit(2) from error
    applied = controller.import_csv_file(str(csv_path), anchor_date=date_.min, mode=mode, allow_updates=allow_updates)
    if not applied.ok:
        print(f"The CSV was not imported; nothing was saved: {applied.error}")
        raise SystemExit(2)
    result = applied.value
    for label, counts in (("Created", result.created), ("Updated", result.updated), ("Deleted", result.deleted),
                          ("Unchanged", result.unchanged)):
        print(f"{label}: " + ", ".join(f"{count} {kind}(s)" for kind, count in counts.items()))


def _export_planning(controller: PlanningController, path: Path, start_date, end_date) -> None:
    exported = controller.export_planning_csv(str(path), start_date=start_date, end_date=end_date)
    if not exported.ok:
        print(f"Planning CSV export failed: {exported.error}")
        raise SystemExit(1)
    result = exported.value
    print(
        f"Stored planning CSV exported to: {result.path} ({result.tasks} task(s), {result.fixed_blocks} fixed "
        f"block(s), {result.placements} placement(s))"
    )


if __name__ == "__main__":
    main()
