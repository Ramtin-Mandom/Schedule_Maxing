"""Tests for app/planning/compat.py: legacy <-> canonical migration adapters."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.data_processor import load_schedule_from_csv, read_csv_rows
from app.planning.compat import (
    convert_legacy_day_schedule,
    date_to_legacy_day,
    import_legacy_csv_rows,
    legacy_day_to_date,
    resolve_legacy_dependency_field,
)

ANCHOR = date(2026, 1, 5)
TZ = "America/New_York"


# -----------------------------------------------------------------------------
# Legacy day-index <-> calendar date
# -----------------------------------------------------------------------------


def test_day_1_is_anchor_date():
    assert legacy_day_to_date(1, ANCHOR) == ANCHOR


def test_day_n_is_anchor_plus_n_minus_1():
    assert legacy_day_to_date(5, ANCHOR) == ANCHOR + timedelta(days=4)


def test_day_0_is_rejected():
    with pytest.raises(ValueError):
        legacy_day_to_date(0, ANCHOR)


def test_date_to_legacy_day_inverts_legacy_day_to_date():
    for day in range(1, 10):
        target = legacy_day_to_date(day, ANCHOR)
        assert date_to_legacy_day(target, ANCHOR) == day


def test_date_before_anchor_has_no_legacy_day():
    with pytest.raises(ValueError):
        date_to_legacy_day(ANCHOR - timedelta(days=1), ANCHOR)


# -----------------------------------------------------------------------------
# Dependency-name resolution
# -----------------------------------------------------------------------------


def test_resolve_exact_full_name_match_preserving_hyphen():
    task_id = uuid.uuid4()
    known = {"Pre-Calc Review": [task_id]}
    ids, diagnostics = resolve_legacy_dependency_field("Study Session", "Pre-Calc Review", known)
    assert ids == [task_id]
    assert diagnostics == []


def test_resolve_semicolon_separated_explicit_references():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    known = {"Task A": [id_a], "Task B": [id_b]}
    ids, diagnostics = resolve_legacy_dependency_field("Task C", "Task A;Task B", known)
    assert set(ids) == {id_a, id_b}
    assert diagnostics == []


def test_resolve_hyphen_delimited_when_unambiguous():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    known = {"Task A": [id_a], "Task B": [id_b]}
    ids, diagnostics = resolve_legacy_dependency_field("Task C", "Task A-Task B", known)
    assert set(ids) == {id_a, id_b}
    assert diagnostics == []


def test_resolve_missing_reference_is_ignored_with_diagnostic():
    ids, diagnostics = resolve_legacy_dependency_field("Task C", "Does Not Exist", {})
    assert ids == []
    assert len(diagnostics) == 1
    assert diagnostics[0].kind == "missing"


def test_resolve_ambiguous_duplicate_name_is_reported_not_guessed():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    known = {"Study Session": [id_a, id_b]}
    ids, diagnostics = resolve_legacy_dependency_field("Follow Up", "Study Session", known)
    assert ids == []
    assert len(diagnostics) == 1
    assert diagnostics[0].kind == "ambiguous"


def test_resolve_empty_field_returns_nothing():
    ids, diagnostics = resolve_legacy_dependency_field("Task C", "", {})
    assert ids == []
    assert diagnostics == []


def test_resolve_hyphenated_name_preferred_over_hyphen_split():
    """"Pre-Calc Review" must resolve as one task, not split into "Pre" and "Calc Review"."""
    task_id = uuid.uuid4()
    known = {"Pre-Calc Review": [task_id]}
    ids, diagnostics = resolve_legacy_dependency_field("Study Session", "Pre-Calc Review", known)
    assert ids == [task_id]


# -----------------------------------------------------------------------------
# convert_legacy_day_schedule (in-memory legacy objects)
# -----------------------------------------------------------------------------


def test_convert_legacy_day_schedule_assigns_ids_and_dates():
    schedule_input = load_schedule_from_csv("samples/inputs/valid_single_day_basic.csv")
    result = convert_legacy_day_schedule(
        schedule_input.schedules[1], day_index=1, anchor_date=ANCHOR, tz_name=TZ
    )

    assert result.day_schedule.date == ANCHOR
    assert result.day_schedule.timezone == TZ
    assert len(result.day_schedule.fixed_blocks) == 4
    assert len(result.day_schedule.tasks.tasks) == 3
    for task in result.day_schedule.tasks.tasks.values():
        assert isinstance(task.id, uuid.UUID)


def test_convert_legacy_day_schedule_resolves_dependency_chain():
    schedule_input = load_schedule_from_csv("samples/inputs/dependency_chain_linear.csv")
    result = convert_legacy_day_schedule(
        schedule_input.schedules[1], day_index=1, anchor_date=ANCHOR, tz_name=TZ
    )

    by_name = {task.name: task for task in result.day_schedule.tasks.tasks.values()}
    task_b = by_name["Task B"]
    task_a = by_name["Task A"]
    assert task_b.dependency_ids == [task_a.id]
    assert result.diagnostics == []


def test_convert_legacy_day_schedule_ignores_missing_dependency_with_diagnostic():
    schedule_input = load_schedule_from_csv("samples/inputs/dependency_missing_reference.csv")
    result = convert_legacy_day_schedule(
        schedule_input.schedules[1], day_index=1, anchor_date=ANCHOR, tz_name=TZ
    )
    # Missing dependency names are ignored (not a hard error) but recorded.
    assert any(diag.kind == "missing" for diag in result.diagnostics)


def test_convert_legacy_day_schedule_repeated_import_gets_fresh_ids():
    schedule_input = load_schedule_from_csv("samples/inputs/valid_single_day_basic.csv")
    first = convert_legacy_day_schedule(schedule_input.schedules[1], day_index=1, anchor_date=ANCHOR, tz_name=TZ)
    second = convert_legacy_day_schedule(schedule_input.schedules[1], day_index=1, anchor_date=ANCHOR, tz_name=TZ)

    first_ids = {task.id for task in first.day_schedule.tasks.tasks.values()}
    second_ids = {task.id for task in second.day_schedule.tasks.tasks.values()}
    assert first_ids.isdisjoint(second_ids)


def test_convert_legacy_day_schedule_requires_explicit_anchor():
    import inspect

    signature = inspect.signature(convert_legacy_day_schedule)
    assert "anchor_date" in signature.parameters
    assert signature.parameters["anchor_date"].default is inspect._empty


# -----------------------------------------------------------------------------
# import_legacy_csv_rows (raw CSV rows, new import adapter)
# -----------------------------------------------------------------------------


def test_import_legacy_csv_rows_preserves_hyphenated_task_name():
    rows = read_csv_rows("samples/inputs/dependency_name_contains_hyphen.csv")
    imported = import_legacy_csv_rows(rows, anchor_date=ANCHOR, tz_name=TZ)
    day1 = imported[1]

    by_name = {task.name: task for task in day1.day_schedule.tasks.tasks.values()}
    assert "Pre-Calc Review" in by_name
    study_session = by_name["Study Session"]
    assert study_session.dependency_ids == [by_name["Pre-Calc Review"].id]
    assert day1.diagnostics == []


def test_import_legacy_csv_rows_reports_ambiguous_duplicate_names():
    rows = read_csv_rows("samples/inputs/duplicate_task_names_edge_case.csv")
    imported = import_legacy_csv_rows(rows, anchor_date=ANCHOR, tz_name=TZ)
    day1 = imported[1]

    assert any(diag.kind == "ambiguous" for diag in day1.diagnostics)
    by_name_ids = day1.task_ids_by_name["Study Session"]
    assert len(by_name_ids) == 2

    follow_up = next(t for t in day1.day_schedule.tasks.tasks.values() if t.name == "Follow Up Task")
    assert follow_up.dependency_ids == []


def test_import_legacy_csv_rows_splits_by_day():
    rows = read_csv_rows("samples/inputs/valid_multi_day_two_days.csv")
    imported = import_legacy_csv_rows(rows, anchor_date=ANCHOR, tz_name=TZ)
    assert set(imported.keys()) == {1, 2}
    assert imported[1].day_schedule.date == ANCHOR
    assert imported[2].day_schedule.date == ANCHOR + timedelta(days=1)


def test_import_legacy_csv_rows_handles_end_of_day_1440_boundary():
    rows = read_csv_rows("samples/inputs/end_of_day_boundary_1440.csv")
    imported = import_legacy_csv_rows(rows, anchor_date=ANCHOR, tz_name=TZ)
    day1 = imported[1]
    # Every fixed block and task converted without raising; end_time=1440
    # rows resolve to the following midnight rather than erroring.
    assert day1.day_schedule.date == ANCHOR


def test_import_legacy_csv_rows_fixed_blocks_do_not_become_tasks():
    rows = read_csv_rows("samples/inputs/valid_single_day_basic.csv")
    imported = import_legacy_csv_rows(rows, anchor_date=ANCHOR, tz_name=TZ)
    day1 = imported[1]
    fixed_labels = {block.label for block in day1.day_schedule.fixed_blocks}
    assert fixed_labels == {"Sleep", "Breakfast", "Lunch", "Dinner"}
    task_names = {task.name for task in day1.day_schedule.tasks.tasks.values()}
    assert fixed_labels.isdisjoint(task_names)


def test_import_legacy_csv_rows_duplicate_names_get_distinct_ids():
    rows = read_csv_rows("samples/inputs/duplicate_task_names_edge_case.csv")
    imported = import_legacy_csv_rows(rows, anchor_date=ANCHOR, tz_name=TZ)
    day1 = imported[1]
    ids = day1.task_ids_by_name["Study Session"]
    assert len(ids) == 2
    assert ids[0] != ids[1]
