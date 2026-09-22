"""Tests for app/planning/csv_export.py: the stored-planning CSV contract is
read from SQLite (through PlanningService), survives a fresh reopen
byte-for-byte, quotes awkward values correctly, keeps exact intervals
(off-grid minutes, seconds, microseconds, local vs UTC), filters by range,
and never writes to the database.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.execution.db import get_connection
from app.planning.application import PlanningService
from app.planning.csv_export import COLUMNS, export_planning_csv
from app.planning.models import FixedBlock, LocalTimeWindow, ScheduledTask, Task
from app.planning.repository import PlanningRepository

NY = ZoneInfo("America/New_York")
DAY = date(2026, 3, 2)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert tuple(reader.fieldnames) == COLUMNS
        return list(reader)


def populate(service: PlanningService) -> tuple[Task, Task, FixedBlock, ScheduledTask]:
    awkward = Task(
        name='Essay, part "one"\nand two', category="study", tags=["a,b", 'quo"te'],
        estimated_duration_minutes=45, priority=7, preferred_dates=[DAY],
        preferred_time_window=LocalTimeWindow(start_minute=613, end_minute=1440),
        deadline=datetime(2026, 3, 3, 17, 0, tzinfo=NY),
    )
    dependent = Task(name="Review", category="study", estimated_duration_minutes=30, priority=5,
                     preferred_dates=[DAY + timedelta(days=5)], dependency_ids=[awkward.id])
    service.save_tasks([awkward, dependent])
    block = service.save_fixed_block(FixedBlock(
        label="Lecture, room 2", planned_date=DAY, timezone="America/New_York",
        planned_start=datetime(2026, 3, 2, 8, 0, tzinfo=NY), planned_end=datetime(2026, 3, 2, 9, 15, tzinfo=NY),
    ))
    start = datetime(2026, 3, 2, 15, 13, 7, 250000, tzinfo=timezone.utc)
    [placement] = service.replace_placements(DAY, DAY, [ScheduledTask(
        task_id=awkward.id, planned_date=DAY, timezone="UTC", planned_start=start,
        planned_end=start + timedelta(minutes=3, seconds=30), score=12.3456789,
    )]).placements
    return awkward, dependent, block, placement


def test_export_contains_every_entity_with_exact_values(planning_service: PlanningService, tmp_path: Path) -> None:
    awkward, dependent, block, placement = populate(planning_service)

    result = export_planning_csv(planning_service, tmp_path / "out.csv")
    rows = read_rows(result.path)

    assert (result.tasks, result.fixed_blocks, result.placements) == (2, 1, 1)
    assert [row["record_type"] for row in rows] == ["task", "task", "fixed_block", "placement"]
    task_row, dependent_row, block_row, placement_row = rows

    assert task_row["id"] == str(awkward.id)
    assert task_row["name"] == 'Essay, part "one"\nand two'  # quoting round-trips
    assert json.loads(task_row["tags"]) == ["a,b", 'quo"te']
    assert task_row["preferred_window"] == "10:13-24:00"
    assert task_row["deadline"] == "2026-03-03T17:00:00-05:00"  # original offset kept
    assert json.loads(dependent_row["dependency_ids"]) == [str(awkward.id)]
    assert task_row["version"] == "1" and task_row["created_at"] == awkward.created_at.isoformat()

    assert block_row["name"] == "Lecture, room 2"
    assert (block_row["start_local"], block_row["end_local"]) == ("08:00", "09:15")
    assert (block_row["start_utc"], block_row["end_utc"]) == ("2026-03-02T13:00:00+00:00", "2026-03-02T14:15:00+00:00")
    assert block_row["duration_minutes"] == "75"

    assert placement_row["task_id"] == str(awkward.id) and placement_row["name"] == awkward.name
    assert placement_row["start_utc"] == "2026-03-02T15:13:07.250000+00:00"
    assert placement_row["end_utc"] == "2026-03-02T15:16:37.250000+00:00"
    assert placement_row["duration_minutes"] == "3.5"
    assert float(placement_row["score"]) == placement.score


def test_export_is_identical_after_a_fresh_reopen(db_path: Path, tmp_path: Path) -> None:
    connection = get_connection(db_path)
    populate(PlanningService(PlanningRepository(connection)))
    export_planning_csv(PlanningService(PlanningRepository(connection)), tmp_path / "before.csv")
    connection.close()

    reopened = get_connection(db_path)
    try:
        export_planning_csv(PlanningService(PlanningRepository(reopened)), tmp_path / "after.csv")
    finally:
        reopened.close()

    assert (tmp_path / "before.csv").read_bytes() == (tmp_path / "after.csv").read_bytes()


def test_range_export_includes_only_that_range(planning_service: PlanningService, tmp_path: Path) -> None:
    awkward, dependent, _, _ = populate(planning_service)

    rows = read_rows(export_planning_csv(planning_service, tmp_path / "range.csv", start_date=DAY, end_date=DAY).path)

    assert {row["id"] for row in rows if row["record_type"] == "task"} == {str(awkward.id)}
    assert [row["record_type"] for row in rows].count("placement") == 1


def test_export_never_writes_to_the_database(planning_service: PlanningService, connection, tmp_path: Path) -> None:
    populate(planning_service)
    changes_before = connection.total_changes
    snapshot = (planning_service.list_tasks(), planning_service.list_fixed_blocks(), planning_service.list_placements())

    export_planning_csv(planning_service, tmp_path / "a.csv")
    export_planning_csv(planning_service, tmp_path / "b.csv", start_date=DAY, end_date=DAY)

    assert connection.total_changes == changes_before
    assert (planning_service.list_tasks(), planning_service.list_fixed_blocks(),
            planning_service.list_placements()) == snapshot
