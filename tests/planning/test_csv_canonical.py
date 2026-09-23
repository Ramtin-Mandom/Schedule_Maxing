"""The canonical, identity-preserving planning CSV (format version 2):
export (app/planning/csv_export.py) -> import (app/planning/csv_canonical.py +
PlanningService.apply_record_batch) reproduces every record, relationship,
version, timestamp, tombstone, category, recurrence and placement metadata;
collisions are deterministic and never overwrite silently; malformed or
invalid batches change nothing; legacy ID-less CSVs keep working.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.execution.db import get_connection
from app.planning.application import PlanningService
from app.planning.csv_canonical import is_canonical_csv, parse_canonical_csv
from app.planning.csv_export import COLUMNS, COLUMNS_V1, export_planning_csv
from app.planning.csv_import import CsvImportError, ImportMode
from app.planning.errors import EntityInUseError, InvalidEntityError, InvalidReferenceError, VersionConflictError
from app.planning.models import FixedBlock, LocalTimeWindow, Project, RecurrenceSpec, ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.ui.planning_controller import PlanningController
from tests.planning.conftest import FakeClock

NY = ZoneInfo("America/New_York")
DAY = date(2026, 3, 2)


def open_service(path: Path, clock: FakeClock | None = None):
    connection = get_connection(path)
    return connection, PlanningService(PlanningRepository(connection), clock=clock or FakeClock())


def snapshot(service: PlanningService) -> dict:
    return {
        "projects": service.list_projects(include_deleted=True),
        "tasks": service.list_tasks(include_deleted=True),
        "blocks": service.list_fixed_blocks(include_deleted=True),
        "placements": service.list_placements(include_deleted=True),
    }


def populate(service: PlanningService) -> dict:
    project = service.save_project(Project(name="Thesis", description="Chapter 3, \"methods\""))
    base = Task(
        name="Read, annotate", category="study", tags=["a,b", "c"], estimated_duration_minutes=45, priority=7,
        project_id=project.id, preferred_dates=[DAY], preferred_time_window=LocalTimeWindow(start_minute=613, end_minute=1440),
        deadline=datetime(2026, 3, 3, 17, tzinfo=NY), created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    weekly = Task(
        name="Weekly review", category="work", estimated_duration_minutes=30, priority=4, required=True,
        required_date=DAY, dependency_ids=[base.id], created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        recurrence=RecurrenceSpec(frequency="weekly", interval=2, weekdays=[0, 4], end_date=date(2026, 6, 1)),
    )
    doomed = Task(name="Dropped", category="chores", estimated_duration_minutes=15, priority=2,
                  created_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    service.save_tasks([base, weekly, doomed])
    service.update_task(base.model_copy(update={"priority": 8}), expected_version=1)  # version 2
    service.delete_task(doomed.id, expected_version=1)  # a tombstone
    block = service.save_fixed_block(FixedBlock(
        label="Sleep", category="sleep", planned_date=DAY, timezone="America/New_York",
        planned_start=datetime(2026, 3, 2, 0, 0, tzinfo=NY), planned_end=datetime(2026, 3, 2, 7, 0, tzinfo=NY),
    ))
    start = datetime(2026, 3, 2, 15, 13, 7, 250000, tzinfo=timezone.utc)
    [placement] = service.replace_placements(DAY, DAY, [ScheduledTask(
        task_id=base.id, planned_date=DAY, timezone="UTC", planned_start=start,
        planned_end=start + timedelta(minutes=3), score=12.3456789, optimization_metadata={"mode": "adhd", "n": [1, 2]},
    )]).placements
    return {"project": project, "base": base, "weekly": weekly, "doomed": doomed, "block": block, "placement": placement}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write_rows(path: Path, rows: list[dict[str, str]], columns=COLUMNS) -> Path:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def exported(tmp_path: Path):
    connection, service = open_service(tmp_path / "source.db")
    try:
        records = populate(service)
        path = export_planning_csv(service, tmp_path / "planning.csv", include_deleted=True).path
        yield path, snapshot(service), records
    finally:
        connection.close()


# -----------------------------------------------------------------------------
# Round trip
# -----------------------------------------------------------------------------


def test_export_import_round_trip_preserves_identity_and_every_field(exported, tmp_path: Path) -> None:
    path, original, records = exported
    assert is_canonical_csv(path.read_text(encoding="utf-8"))

    connection, target = open_service(tmp_path / "target.db")
    try:
        result = target.apply_record_batch(parse_canonical_csv(path.read_text(encoding="utf-8")))
        assert result.created == {"project": 1, "task": 3, "fixed_block": 1, "placement": 1}
        assert snapshot(target) == original

        weekly = target.get_task(records["weekly"].id)
        assert weekly.recurrence == records["weekly"].recurrence and weekly.dependency_ids == [records["base"].id]
        assert target.get_task(records["base"].id).version == 2 and target.get_task(records["base"].id).project_id
        assert target.get_task(records["doomed"].id) is None  # arrived as a tombstone, stays one
        [block] = target.fixed_blocks_for_date(DAY)
        assert block.category == "sleep"
        [placement] = target.placements_for_date(DAY)
        assert placement.optimization_metadata == {"mode": "adhd", "n": [1, 2]} and placement.score == 12.3456789
    finally:
        connection.close()


def test_reimporting_the_same_file_is_a_no_op(exported, tmp_path: Path) -> None:
    path, original, _ = exported
    connection, target = open_service(tmp_path / "target.db")
    try:
        batch = parse_canonical_csv(path.read_text(encoding="utf-8"))
        target.apply_record_batch(batch)
        again = target.apply_record_batch(batch)
        assert sum(again.unchanged.values()) == 6 and sum(again.created.values()) == sum(again.updated.values()) == 0
        assert snapshot(target) == original
    finally:
        connection.close()


# -----------------------------------------------------------------------------
# Collisions and preconditions
# -----------------------------------------------------------------------------


def _import_edited(tmp_path: Path, rows, service: PlanningService, *, allow_updates: bool):
    path = write_rows(tmp_path / "edited.csv", rows)
    return service.apply_record_batch(parse_canonical_csv(path.read_text(encoding="utf-8")), allow_updates=allow_updates)


def test_divergent_records_are_refused_unless_updates_are_allowed_with_a_valid_version(tmp_path: Path) -> None:
    connection, service = open_service(tmp_path / "app.db")
    try:
        records = populate(service)
        path = export_planning_csv(service, tmp_path / "planning.csv").path
        before = snapshot(service)
        rows = read_rows(path)
        base_row = next(row for row in rows if row["id"] == str(records["base"].id))
        base_row["name"] = "Renamed in a spreadsheet"

        with pytest.raises(VersionConflictError, match="allow updates"):
            _import_edited(tmp_path, rows, service, allow_updates=False)
        assert snapshot(service) == before

        result = _import_edited(tmp_path, rows, service, allow_updates=True)
        assert result.updated["task"] == 1 and result.unchanged["task"] == 1
        renamed = service.get_task(records["base"].id)
        assert (renamed.name, renamed.version, renamed.created_at) == ("Renamed in a spreadsheet", 3, records["base"].created_at)
    finally:
        connection.close()


def test_csv_versions_cannot_overwrite_newer_data(tmp_path: Path) -> None:
    connection, service = open_service(tmp_path / "app.db")
    try:
        records = populate(service)
        rows = read_rows(export_planning_csv(service, tmp_path / "planning.csv").path)
        # Someone edits the task after the export...
        newer = service.update_task(service.get_task(records["base"].id).model_copy(update={"priority": 1}),
                                    expected_version=2)
        base_row = next(row for row in rows if row["id"] == str(records["base"].id))
        base_row["name"] = "Stale edit"

        with pytest.raises(VersionConflictError):  # ...so the exported version 2 is no longer a valid precondition
            _import_edited(tmp_path, rows, service, allow_updates=True)
        base_row["version"] = "99"  # and a made-up version cannot jump ahead either
        with pytest.raises(VersionConflictError):
            _import_edited(tmp_path, rows, service, allow_updates=True)
        assert service.get_task(records["base"].id) == newer
    finally:
        connection.close()


def test_tombstones_round_trip_and_deleted_records_are_never_revived(tmp_path: Path) -> None:
    connection, service = open_service(tmp_path / "app.db")
    try:
        records = populate(service)
        rows = read_rows(export_planning_csv(service, tmp_path / "planning.csv", include_deleted=True).path)

        doomed_row = next(row for row in rows if row["id"] == str(records["doomed"].id))
        doomed_row["deleted_at"] = ""  # try to bring it back
        with pytest.raises(VersionConflictError, match="cannot bring it back"):
            _import_edited(tmp_path, rows, service, allow_updates=True)

        doomed_row["deleted_at"] = "2026-01-10T00:00:00+00:00"
        block_row = next(row for row in rows if row["record_type"] == "fixed_block")
        block_row["deleted_at"] = "2026-03-01T00:00:00+00:00"  # delete the block through the import
        result = _import_edited(tmp_path, rows, service, allow_updates=True)
        assert result.deleted == {"project": 0, "task": 0, "fixed_block": 1, "placement": 0}
        assert service.fixed_blocks_for_date(DAY) == []
        assert service.list_fixed_blocks(include_deleted=True)[0].version == records["block"].version + 1
    finally:
        connection.close()


# -----------------------------------------------------------------------------
# Whole-batch validation
# -----------------------------------------------------------------------------


def test_malformed_rows_are_all_reported_and_nothing_is_written(exported, tmp_path: Path) -> None:
    path, _, _ = exported
    rows = read_rows(path)
    rows[0]["id"] = "not-a-uuid"
    rows[1]["created_at"] = "2026-01-01T00:00:00"  # naive
    rows[3]["record_type"] = "gadget"

    with pytest.raises(CsvImportError) as error:
        parse_canonical_csv(write_rows(tmp_path / "bad.csv", rows).read_text(encoding="utf-8"))
    assert [issue.line for issue in error.value.issues] == [2, 3, 5]


@pytest.mark.parametrize("problem", ["missing_dependency", "missing_project", "cycle", "overlap", "dependent_of_deleted"])
def test_invalid_batches_roll_back_completely(tmp_path: Path, problem: str) -> None:
    connection, service = open_service(tmp_path / "app.db")
    try:
        records = populate(service)
        before = snapshot(service)
        batch_rows = read_rows(export_planning_csv(service, tmp_path / "planning.csv").path)
        extra = Task(name="New and valid", category="study", estimated_duration_minutes=30, priority=5)
        rows = []
        if problem == "missing_dependency":
            rows = [_task_row(extra.model_copy(update={"id": uuid.uuid4(), "dependency_ids": [uuid.uuid4()]}))]
        elif problem == "missing_project":
            rows = [_task_row(extra.model_copy(update={"id": uuid.uuid4(), "project_id": uuid.uuid4()}))]
        elif problem == "cycle":
            base_row = next(row for row in batch_rows if row["id"] == str(records["base"].id))
            base_row["dependency_ids"] = json.dumps([str(records["weekly"].id)])
            rows = [base_row]
        elif problem == "overlap":
            rows = [_block_row(FixedBlock(
                label="Nap", planned_date=DAY, timezone="America/New_York",
                planned_start=datetime(2026, 3, 2, 6, tzinfo=NY), planned_end=datetime(2026, 3, 2, 8, tzinfo=NY),
            ))]
        elif problem == "dependent_of_deleted":
            base_row = next(row for row in batch_rows if row["id"] == str(records["base"].id))
            base_row["deleted_at"] = "2026-03-01T00:00:00+00:00"  # weekly still depends on it
            rows = [base_row]
        rows = [_task_row(extra)] + rows
        path = write_rows(tmp_path / "batch.csv", rows)

        expected = {
            "missing_dependency": InvalidReferenceError, "missing_project": InvalidReferenceError,
            "cycle": InvalidEntityError, "overlap": InvalidEntityError, "dependent_of_deleted": EntityInUseError,
        }[problem]
        with pytest.raises(expected):
            service.apply_record_batch(parse_canonical_csv(path.read_text(encoding="utf-8")), allow_updates=True)
        assert snapshot(service) == before  # the valid first row was not written either
    finally:
        connection.close()


def test_ownership_must_be_consistent(tmp_path: Path) -> None:
    connection, service = open_service(tmp_path / "app.db")
    try:
        owner, other = uuid.uuid4(), uuid.uuid4()
        mixed = [_task_row(Task(name=f"T{i}", category="c", estimated_duration_minutes=5, priority=1, user_id=user))
                 for i, user in enumerate((owner, other))]
        with pytest.raises(InvalidEntityError, match="owners"):
            service.apply_record_batch(parse_canonical_csv(write_rows(tmp_path / "m.csv", mixed).read_text(encoding="utf-8")))

        mine = service.save_task(Task(name="Mine", category="c", estimated_duration_minutes=5, priority=1, user_id=owner))
        takeover = [_task_row(mine.model_copy(update={"user_id": other}))]
        with pytest.raises(InvalidEntityError, match="different owner"):
            service.apply_record_batch(
                parse_canonical_csv(write_rows(tmp_path / "t.csv", takeover).read_text(encoding="utf-8")), allow_updates=True
            )
        assert service.get_task(mine.id).user_id == owner
    finally:
        connection.close()


def test_format_rules(tmp_path: Path) -> None:
    task = Task(name="T", category="c", estimated_duration_minutes=5, priority=1, preferred_dates=[DAY])
    row = _task_row(task)

    with pytest.raises(CsvImportError, match="format version 1"):
        parse_canonical_csv(write_rows(tmp_path / "v1.csv", [row], COLUMNS_V1).read_text(encoding="utf-8"))
    with pytest.raises(CsvImportError, match="already appears"):
        parse_canonical_csv(write_rows(tmp_path / "dup.csv", [row, row]).read_text(encoding="utf-8"))
    with pytest.raises(CsvImportError, match="date is derived"):
        parse_canonical_csv(write_rows(tmp_path / "d.csv", [{**row, "date": "2026-03-09"}]).read_text(encoding="utf-8"))

    blank = parse_canonical_csv(write_rows(tmp_path / "b.csv", [{**row, "id": ""}]).read_text(encoding="utf-8"))
    assert blank.tasks[0].id != task.id  # a row without an id gets a fresh UUID, like a legacy import
    assert parse_canonical_csv(write_rows(tmp_path / "ok.csv", [row]).read_text(encoding="utf-8")).tasks == [task]


# -----------------------------------------------------------------------------
# Through the controller: format detection, legacy compatibility
# -----------------------------------------------------------------------------


def test_controller_detects_the_format_and_keeps_legacy_imports_working(exported, tmp_path: Path) -> None:
    path, original, _ = exported
    connection = get_connection(tmp_path / "target.db")
    try:
        service = PlanningService(PlanningRepository(connection))
        controller = PlanningController(service=service, timezone="UTC", project_root=str(tmp_path))

        refused = controller.import_csv_file(str(path), anchor_date=DAY, mode=ImportMode.REPLACE)
        assert not refused.ok and "Append" in refused.error
        merged = controller.import_csv_file(str(path), anchor_date=DAY, mode=ImportMode.APPEND)
        assert merged.ok, merged.error
        assert snapshot(service) == original

        legacy = tmp_path / "legacy.csv"
        legacy.write_text(
            "date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
            "1,Sleep,sleep,fixed,true,0,480,480,0,\n1,Study,study,math,false,540,720,60,8,\n",
            encoding="utf-8",
        )
        first = controller.import_csv_file(str(legacy), anchor_date=date(2026, 4, 6), mode=ImportMode.APPEND).value
        flexible_only = tmp_path / "legacy_tasks.csv"
        flexible_only.write_text(
            "date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
            "1,Study,study,math,false,540,720,60,8,\n",
            encoding="utf-8",
        )
        second = controller.import_csv_file(str(flexible_only), anchor_date=date(2026, 4, 6), mode=ImportMode.APPEND).value
        assert first.tasks[0].id != second.tasks[0].id  # ID-less rows get fresh UUIDs every time
        assert first.fixed_blocks[0].category == "sleep"  # the legacy row's category is kept
    finally:
        connection.close()


def _task_row(task: Task) -> dict[str, str]:
    return _row_via_export(tasks=[task])


def _block_row(block: FixedBlock) -> dict[str, str]:
    return _row_via_export(blocks=[block])


def _row_via_export(*, tasks=(), blocks=()) -> dict[str, str]:
    from app.planning import csv_export

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    for task in tasks:
        writer.writerow(csv_export._task_row(task))
    for block in blocks:
        writer.writerow(csv_export._block_row(block))
    return next(csv.DictReader(io.StringIO(buffer.getvalue())))
