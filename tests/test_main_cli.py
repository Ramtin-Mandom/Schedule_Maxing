"""Integration tests for app/main.py's CLI on the persistence-backed service
(Milestone 2): plain startup reads SQLite and imports nothing, the sample is
only used with an explicit --demo, CSV imports go through the transactional
importer with an explicit anchor date, only the selected date is generated,
results are saved and reused, and the exact JSON / stored-planning CSV
exports keep exact intervals that the legacy 30-minute CSV cannot represent.

Every run passes a temporary --db-path (and tests/conftest.py redirects the
default data location to a temporary folder as a safety net).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.main import main
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository

SAMPLE_CSV = "samples/inputs/valid_single_day_basic.csv"
MULTI_DAY_CSV = "samples/inputs/valid_multi_day_two_days.csv"


def stored(db_path: Path) -> tuple[list, list, list]:
    connection = get_connection(db_path)
    try:
        service = PlanningService(PlanningRepository(connection))
        return service.list_tasks(), service.list_fixed_blocks(), service.list_placements()
    finally:
        connection.close()


def outputs(tmp_path: Path) -> list[str]:
    return ["--legacy-csv-out", str(tmp_path / "legacy.csv"), "--exact-json-out", str(tmp_path / "exact.json")]


# -----------------------------------------------------------------------------
# Startup contract
# -----------------------------------------------------------------------------


def test_plain_startup_reads_sqlite_and_imports_nothing(tmp_path, capsys):
    db_path = tmp_path / "app.db"

    main(["--db-path", str(db_path)])
    main(["--db-path", str(db_path)])

    assert stored(db_path) == ([], [], [])
    assert "Stored: 0 task(s)" in capsys.readouterr().out
    assert not (tmp_path / "legacy.csv").exists()


def test_plain_startup_summarizes_previously_saved_data(tmp_path, capsys):
    db_path = tmp_path / "app.db"
    main(["--db-path", str(db_path), "--import-csv", SAMPLE_CSV, "--anchor-date", "2026-01-05", *outputs(tmp_path)])
    before = stored(db_path)
    capsys.readouterr()

    main(["--db-path", str(db_path)])

    assert stored(db_path) == before  # nothing added, nothing overwritten
    out = capsys.readouterr().out
    assert "Stored: 3 task(s), 4 fixed block(s), 3 scheduled placement(s)." in out


def test_demo_is_explicit_and_uses_a_throwaway_database(tmp_path):
    main(["--demo", *outputs(tmp_path)])

    data = json.loads((tmp_path / "exact.json").read_text())
    assert data["date"] == "2026-01-05"
    assert {task["name"] for task in data["tasks"]["tasks"].values()} == {"Study Session", "Gym", "Free Reading"}
    assert (tmp_path / "legacy.csv").exists()


def test_demo_into_an_explicit_database_persists_the_sample(tmp_path):
    db_path = tmp_path / "demo.db"
    main(["--demo", "--db-path", str(db_path), *outputs(tmp_path)])
    tasks, blocks, placements = stored(db_path)
    assert len(tasks) == 3 and len(blocks) == 4 and len(placements) == 3


# -----------------------------------------------------------------------------
# Import + selected-date generation (coverage kept from the pre-SQLite CLI)
# -----------------------------------------------------------------------------


def test_csv_import_requires_explicit_anchor_date(tmp_path):
    with pytest.raises(ValueError, match="anchor-date is required"):
        main(["--db-path", str(tmp_path / "app.db"), "--csv", MULTI_DAY_CSV, *outputs(tmp_path)])
    assert not (tmp_path / "app.db").exists()  # refused before anything was opened


def test_multi_day_csv_generates_only_the_selected_date(tmp_path):
    db_path = tmp_path / "app.db"
    main(["--db-path", str(db_path), "--csv", MULTI_DAY_CSV, "--anchor-date", "2026-01-05", "--timezone", "UTC",
          *outputs(tmp_path)])

    data = json.loads((tmp_path / "exact.json").read_text())
    assert data["date"] == "2026-01-05"
    task_names = {task["name"] for task in data["tasks"]["tasks"].values()}
    # Day 2's tasks (Edit Draft / Submit Assignment) must not appear in day 1's output.
    assert "Edit Draft" not in task_names
    assert "Submit Assignment" not in task_names

    tasks, blocks, placements = stored(db_path)
    assert len(tasks) == 4 and len(blocks) == 8  # both days were imported ...
    assert {placement.planned_date.isoformat() for placement in placements} == {"2026-01-05"}  # ... one generated


def test_explicit_select_date_picks_the_other_day(tmp_path):
    main(["--db-path", str(tmp_path / "app.db"), "--csv", MULTI_DAY_CSV, "--anchor-date", "2026-01-05",
          "--timezone", "UTC", "--select-date", "2026-01-06", *outputs(tmp_path)])

    data = json.loads((tmp_path / "exact.json").read_text())
    assert data["date"] == "2026-01-06"


def test_mode_option_reaches_the_day_scheduler(tmp_path):
    main(["--db-path", str(tmp_path / "app.db"), "--csv", SAMPLE_CSV, "--anchor-date", "2026-01-05",
          "--mode", "adhd_friendly", *outputs(tmp_path)])
    assert json.loads((tmp_path / "exact.json").read_text())["placements"]


def test_exact_export_preserves_short_task_and_non_grid_boundary(tmp_path):
    """A 3-minute task starting at a non-30-minute boundary (10:13) must
    survive exactly in the exact JSON export, even though the legacy
    30-minute-block CSV export cannot represent it faithfully."""
    csv_path = tmp_path / "odd_timing.csv"
    csv_path.write_text(
        "date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
        "1,Quick Note,work,,false,613,616,3,5,\n",
        encoding="utf-8",
    )

    main(["--db-path", str(tmp_path / "app.db"), "--csv", str(csv_path), "--anchor-date", "2026-01-05",
          "--timezone", "UTC", *outputs(tmp_path)])

    [placement] = json.loads((tmp_path / "exact.json").read_text())["placements"]
    assert placement["planned_start"] == "2026-01-05T10:13:00Z"
    assert placement["planned_end"] == "2026-01-05T10:16:00Z"


def test_midnight_boundary_preserved_exactly(tmp_path):
    """samples/inputs/end_of_day_boundary_1440.csv's fixed block ending
    exactly at 24:00 must round-trip to the following date at 00:00."""
    main(["--db-path", str(tmp_path / "app.db"), "--csv", "samples/inputs/end_of_day_boundary_1440.csv",
          "--anchor-date", "2026-01-05", "--timezone", "UTC", *outputs(tmp_path)])

    data = json.loads((tmp_path / "exact.json").read_text())
    fixed_blocks = {block["label"]: block for block in data["fixed_blocks"]}
    assert fixed_blocks["Late Night Work"]["planned_end"] == "2026-01-06T00:00:00Z"


# -----------------------------------------------------------------------------
# Persistence-specific behavior
# -----------------------------------------------------------------------------


def test_scheduling_stored_tasks_later_reuses_saved_placement_ids(tmp_path):
    db_path = tmp_path / "app.db"
    main(["--db-path", str(db_path), "--csv", MULTI_DAY_CSV, "--anchor-date", "2026-01-05", *outputs(tmp_path)])
    first = json.loads((tmp_path / "exact.json").read_text())["placements"]

    # No import this time: schedule both stored days, then day 1 again.
    main(["--db-path", str(db_path), "--start-date", "2026-01-05", "--end-date", "2026-01-06",
          "--select-date", "2026-01-06", *outputs(tmp_path)])
    main(["--db-path", str(db_path), "--start-date", "2026-01-05", "--end-date", "2026-01-06",
          "--select-date", "2026-01-05", *outputs(tmp_path)])
    again = json.loads((tmp_path / "exact.json").read_text())["placements"]

    assert [p["id"] for p in again] == [p["id"] for p in first]
    assert [p["planned_start"] for p in again] == [p["planned_start"] for p in first]
    assert {p.planned_date.isoformat() for p in stored(db_path)[2]} == {"2026-01-05", "2026-01-06"}


def test_invalid_csv_exits_nonzero_and_saves_nothing(tmp_path, capsys):
    db_path = tmp_path / "app.db"
    with pytest.raises(SystemExit) as info:
        main(["--db-path", str(db_path), "--csv", "samples/inputs/dependency_missing_reference.csv",
              "--anchor-date", "2026-01-05", *outputs(tmp_path)])

    assert info.value.code == 2
    assert "Nonexistent Task" in capsys.readouterr().out
    assert stored(db_path) == ([], [], [])
    assert not (tmp_path / "exact.json").exists()


def test_replace_import_replaces_only_the_files_dates(tmp_path):
    db_path = tmp_path / "app.db"
    main(["--db-path", str(db_path), "--csv", MULTI_DAY_CSV, "--anchor-date", "2026-01-05", *outputs(tmp_path)])
    main(["--db-path", str(db_path), "--csv", SAMPLE_CSV, "--anchor-date", "2026-01-06",
          "--import-mode", "replace", *outputs(tmp_path)])

    tasks, blocks, _ = stored(db_path)
    by_date = {}
    for task in tasks:
        by_date.setdefault(task.preferred_dates[0].isoformat(), set()).add(task.name)
    assert by_date["2026-01-05"] == {"Research Topic", "Write Draft"}  # untouched
    assert by_date["2026-01-06"] == {"Study Session", "Gym", "Free Reading"}  # replaced
    assert len(blocks) == 8


def test_export_planning_csv_from_the_cli(tmp_path):
    db_path = tmp_path / "app.db"
    export = tmp_path / "planning.csv"
    main(["--db-path", str(db_path), "--csv", SAMPLE_CSV, "--anchor-date", "2026-01-05",
          "--export-planning-csv", str(export), *outputs(tmp_path)])

    with export.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["record_type"] for row in rows].count("placement") == 3
    assert {row["name"] for row in rows if row["record_type"] == "fixed_block"} == {"Sleep", "Breakfast", "Lunch", "Dinner"}


# -----------------------------------------------------------------------------
# Milestone 3: persisted engine mode, identity-preserving CSV round trip
# -----------------------------------------------------------------------------


def test_mode_is_saved_as_the_user_preference_and_reused(tmp_path):
    db_path = tmp_path / "app.db"
    main(["--db-path", str(db_path), "--csv", SAMPLE_CSV, "--anchor-date", "2026-01-05",
          "--mode", "adhd_friendly", *outputs(tmp_path)])
    main(["--db-path", str(db_path)])  # a later run without --mode keeps the saved preference

    connection = get_connection(db_path)
    try:
        service = PlanningService(PlanningRepository(connection))
        assert service.user_preferences().overrides.optimizer_mode.value == "adhd_friendly"
        assert service.generation_record(service.list_placements()[0].planned_date).engine_mode.value == "adhd_friendly"
    finally:
        connection.close()


def test_stored_planning_csv_round_trips_through_the_cli(tmp_path, capsys):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    exported = tmp_path / "planning.csv"
    main(["--db-path", str(source), "--csv", SAMPLE_CSV, "--anchor-date", "2026-01-05", *outputs(tmp_path)])
    main(["--db-path", str(source), "--export-planning-csv", str(exported)])

    main(["--db-path", str(target), "--import-csv", str(exported), *outputs(tmp_path)])
    main(["--db-path", str(target), "--import-csv", str(exported), *outputs(tmp_path)])  # again: a no-op

    assert stored(target) == stored(source)  # same ids, versions, timestamps, categories, placements
    assert "Unchanged: 0 project(s), 3 task(s), 4 fixed_block(s), 3 placement(s)" in capsys.readouterr().out
