"""The canonical planning CSV with recurring series (docs/recurrence.md): a complete export -- series anchors,
occurrences with their identity and exception states, lineage and reserved-slot tombstones -- imports into an
empty store exactly; re-importing it is a no-op; an older file without the recurrence columns still imports
(its templates need configuration); and a file cannot rewrite an occurrence's identity."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.planning import series as series_ops
from app.planning.application import PlanningService
from app.planning.csv_canonical import parse_canonical_csv
from app.planning.csv_import import CsvImportError
from app.planning.csv_export import COLUMNS, export_planning_csv
from app.planning.errors import InvalidEntityError
from app.planning.models import OccurrenceState, RecurrenceSpec, Task
from app.planning.repository import PlanningRepository
from app.planning.series import EditScope

MON = date(2026, 3, 2)
RECURRENCE_COLUMNS = ("series_id", "occurrence_slot", "occurrence_state", "series_version", "series_predecessor_id")


def store(path: Path):
    connection = get_connection(path)
    return connection, PlanningService(PlanningRepository(connection))


def populated(tmp_path: Path):
    connection, service = store(tmp_path / "source.db")
    walk = service.create_task(Task(name="Walk", category="exercise", estimated_duration_minutes=30, priority=5,
                                    recurrence=RecurrenceSpec(frequency="daily", start_date=MON, timezone="UTC")))
    created = series_ops.expand_occurrences(service, MON, MON + timedelta(days=4)).created
    series_ops.delete_occurrence(service, created[0].id, expected_version=1, skip=True)
    series_ops.edit_occurrence(service, created[1].model_copy(update={"priority": 9}), expected_version=1)
    stored = service.get_task(walk.id)
    series_ops.edit_series(service, stored.model_copy(update={"name": "Run"}), expected_version=stored.version,
                           scope=EditScope.FUTURE, cutoff=MON + timedelta(days=3))
    return connection, service, walk


def test_a_complete_export_round_trips_series_occurrences_lineage_and_reserved_slots(tmp_path: Path) -> None:
    connection, source, walk = populated(tmp_path)
    exported = tmp_path / "all.csv"
    export_planning_csv(source, exported, include_deleted=True)
    connection.close()
    text = exported.read_text(encoding="utf-8")
    assert set(RECURRENCE_COLUMNS) <= set(next(csv.reader(io.StringIO(text))))

    target_connection, target = store(tmp_path / "target.db")
    target.apply_record_batch(parse_canonical_csv(text))
    _, source = store(tmp_path / "source.db")
    exported_again = tmp_path / "again.csv"
    export_planning_csv(target, exported_again, include_deleted=True)
    assert exported_again.read_text(encoding="utf-8") == text  # identical records, identity included

    occurrences = {task.occurrence_slot: task for task in target.occurrences_of_series([walk.id])[walk.id]}
    assert occurrences[MON].occurrence_state == OccurrenceState.SKIPPED and occurrences[MON].deleted_at is not None
    assert occurrences[MON + timedelta(days=1)].occurrence_state == OccurrenceState.MODIFIED
    assert {task.occurrence_state for slot, task in occurrences.items() if slot >= MON + timedelta(days=3)} == {
        OccurrenceState.SUPERSEDED}
    [successor] = target.series_successors([walk.id])[walk.id]
    assert successor.name == "Run" and successor.recurrence.start_date == MON + timedelta(days=3)
    # The reserved slots stay reserved after the import: expanding the same days mints nothing old again.
    again = series_ops.expand_occurrences(target, MON, MON + timedelta(days=4))
    assert {task.series_id for task in again.created} == {successor.id}
    assert target.apply_record_batch(parse_canonical_csv(text)).created == dict.fromkeys(
        ("project", "task", "fixed_block", "placement"), 0)  # importing again: nothing new
    target_connection.close()


def test_an_older_file_without_recurrence_columns_still_imports_its_templates_as_needing_configuration(
    tmp_path: Path,
) -> None:
    connection, service = store(tmp_path / "source.db")
    service.create_task(Task(name="Standup", category="work", estimated_duration_minutes=15, priority=5,
                             recurrence=RecurrenceSpec(frequency="weekly", weekdays=[0])))
    exported = tmp_path / "old.csv"
    export_planning_csv(service, exported)
    connection.close()
    reader = csv.DictReader(io.StringIO(exported.read_text(encoding="utf-8")))
    old_columns = [column for column in COLUMNS if column not in RECURRENCE_COLUMNS]
    rows = []
    for row in reader:
        rule = row["recurrence"].replace(', "start_date": null', "").replace(', "timezone": null', "")
        rows.append({**{column: row[column] for column in old_columns}, "recurrence": rule})
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=old_columns)
    writer.writeheader()
    writer.writerows(rows)
    assert "start_date" not in buffer.getvalue()

    target_connection, target = store(tmp_path / "target.db")
    target.apply_record_batch(parse_canonical_csv(buffer.getvalue()))
    [template] = target.list_series()
    assert template.needs_configuration and template.recurrence.weekdays == [0]
    target_connection.close()


def test_a_file_cannot_rewrite_an_occurrences_identity(tmp_path: Path) -> None:
    connection, service, walk = populated(tmp_path)
    exported = tmp_path / "all.csv"
    export_planning_csv(service, exported, include_deleted=True)
    reader = csv.DictReader(io.StringIO(exported.read_text(encoding="utf-8")))
    rows = list(reader)
    for row in rows:
        if row["occurrence_slot"] == (MON + timedelta(days=2)).isoformat() and row["series_id"] == str(walk.id):
            row["occurrence_slot"] = (MON + timedelta(days=9)).isoformat()
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    with pytest.raises(CsvImportError, match="derived from its series and slot"):  # refused before anything is read
        parse_canonical_csv(buffer.getvalue())
    # Even a batch built in code (no derived-id check of its own) cannot move an occurrence to another series.
    occurrence = next(task for task in service.occurrences_of_series([walk.id])[walk.id] if task.deleted_at is None)
    other = service.create_task(Task(name="Other", category="work", estimated_duration_minutes=15, priority=5,
                                     recurrence=RecurrenceSpec(frequency="daily", start_date=MON, timezone="UTC")))
    from app.planning.application import RecordBatch
    from app.planning.recurrence import occurrence_task_id

    moved = occurrence.model_copy(update={"series_id": other.id})  # bypasses validation, as a crafted record would
    assert moved.id != occurrence_task_id(other.id, occurrence.occurrence_slot)
    with pytest.raises(InvalidEntityError):
        service.apply_record_batch(RecordBatch(tasks=[moved]), allow_updates=True)
    connection.close()
