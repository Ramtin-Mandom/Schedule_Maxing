"""Tests for app/data_processor.py: CSV parsing and conversion into domain models.

Dependency parsing currently splits a flexible task's dependency string on a
plain hyphen (see load_schedule_from_csv). AGENTS.md flags this as a known
issue (task names may themselves contain hyphens) but asks that this
treatment not be silently changed, so these tests characterize the current
behavior rather than assert it is "correct".
"""

from __future__ import annotations

import csv

import pytest
from pydantic import ValidationError

from app.data_processor import load_schedule_from_csv
from app.models import FixedBlock, Task

FIELDNAMES = [
    "date",
    "name",
    "category",
    "tag",
    "fixed",
    "start_time",
    "end_time",
    "duration",
    "priority",
    "dependencies",
]


def write_csv(path, rows: list[dict]) -> str:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(path)


def base_row(**overrides) -> dict:
    row = dict(
        date="1",
        name="Task",
        category="study",
        tag="math",
        fixed="False",
        start_time="540",
        end_time="600",
        duration="60",
        priority="5",
        dependencies="",
    )
    row.update(overrides)
    return row


# -----------------------------
# Fixed and flexible task loading
# -----------------------------


def test_loads_fixed_block_with_actual_scheduled_time(tmp_path) -> None:
    csv_path = write_csv(
        tmp_path / "schedule.csv",
        [base_row(name="Sleep", fixed="True", start_time="0", end_time="480", duration="480")],
    )

    result = load_schedule_from_csv(csv_path)

    day = result.schedules[1]
    assert len(day.fixed_blocks) == 1
    assert day.tasks == []

    block = day.fixed_blocks[0]
    assert isinstance(block, FixedBlock)
    assert block.name == "Sleep"
    assert block.time_window.start_time == 0
    assert block.time_window.end_time == 480


def test_loads_flexible_task_with_preferred_time_window(tmp_path) -> None:
    csv_path = write_csv(
        tmp_path / "schedule.csv",
        [base_row(name="Study Math", start_time="540", end_time="720", duration="120", priority="8")],
    )

    result = load_schedule_from_csv(csv_path)

    day = result.schedules[1]
    assert day.fixed_blocks == []
    assert len(day.tasks) == 1

    task = day.tasks[0]
    assert isinstance(task, Task)
    assert task.fixed is False
    assert task.duration == 120
    assert task.priority == 8
    # For flexible tasks, start/end are the preferred window, not the placement.
    assert task.preference_time.start_time == 540
    assert task.preference_time.end_time == 720


def test_groups_rows_by_date_into_separate_day_schedules(tmp_path) -> None:
    csv_path = write_csv(
        tmp_path / "schedule.csv",
        [
            base_row(date="1", name="Day 1 Task"),
            base_row(date="2", name="Day 2 Task"),
        ],
    )

    result = load_schedule_from_csv(csv_path)

    assert set(result.schedules.keys()) == {1, 2}
    assert result.schedules[1].tasks[0].name == "Day 1 Task"
    assert result.schedules[2].tasks[0].name == "Day 2 Task"


def test_is_fixed_is_case_insensitive_and_trims_whitespace(tmp_path) -> None:
    csv_path = write_csv(
        tmp_path / "schedule.csv",
        [base_row(name="Sleep", fixed=" TRUE ", start_time="0", end_time="480", duration="480")],
    )

    result = load_schedule_from_csv(csv_path)

    assert len(result.schedules[1].fixed_blocks) == 1


# -----------------------------
# Dependency parsing
# -----------------------------


def test_dependencies_empty_string_becomes_empty_list(tmp_path) -> None:
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(dependencies="")])

    result = load_schedule_from_csv(csv_path)

    assert result.schedules[1].tasks[0].dependencies == []


def test_single_dependency_without_hyphen_is_kept_whole(tmp_path) -> None:
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(dependencies="Study Math")])

    result = load_schedule_from_csv(csv_path)

    assert result.schedules[1].tasks[0].dependencies == ["Study Math"]


def test_dependencies_are_split_on_hyphen(tmp_path) -> None:
    """Characterizes current behavior: '-' is the delimiter between multiple names."""
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(dependencies="Study Math-Read Chapter")])

    result = load_schedule_from_csv(csv_path)

    assert result.schedules[1].tasks[0].dependencies == ["Study Math", "Read Chapter"]


def test_dependency_name_containing_a_hyphen_is_split_incorrectly(tmp_path) -> None:
    """Characterizes the known limitation from AGENTS.md: a dependency name that
    itself contains a hyphen gets split into unintended pieces. This is not
    the desired behavior, but it must not change silently -- see AGENTS.md's
    'Known areas requiring care'.
    """
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(dependencies="Pre-Calc Review")])

    result = load_schedule_from_csv(csv_path)

    assert result.schedules[1].tasks[0].dependencies == ["Pre", "Calc Review"]


def test_missing_dependency_reference_is_preserved_for_pert_to_ignore(tmp_path) -> None:
    """data_processor does not validate dependency names against other tasks;
    it just parses them. app.pert is responsible for ignoring missing ones.
    """
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(dependencies="Nonexistent Task")])

    result = load_schedule_from_csv(csv_path)

    assert result.schedules[1].tasks[0].dependencies == ["Nonexistent Task"]


# -----------------------------
# Malformed or missing required values
# -----------------------------


def test_missing_required_column_raises_key_error(tmp_path) -> None:
    path = tmp_path / "schedule.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file, fieldnames=[f for f in FIELDNAMES if f != "priority"]
        )
        writer.writeheader()
        row = base_row()
        del row["priority"]
        writer.writerow(row)

    with pytest.raises(KeyError):
        load_schedule_from_csv(str(path))


def test_non_numeric_duration_raises_value_error(tmp_path) -> None:
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(duration="not-a-number")])

    with pytest.raises(ValueError):
        load_schedule_from_csv(csv_path)


def test_empty_priority_value_raises_value_error(tmp_path) -> None:
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(priority="")])

    with pytest.raises(ValueError):
        load_schedule_from_csv(csv_path)


def test_zero_duration_is_rejected_by_task_model(tmp_path) -> None:
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(duration="0")])

    with pytest.raises(ValidationError):
        load_schedule_from_csv(csv_path)


def test_priority_out_of_range_is_rejected_by_task_model(tmp_path) -> None:
    csv_path = write_csv(tmp_path / "schedule.csv", [base_row(priority="11")])

    with pytest.raises(ValidationError):
        load_schedule_from_csv(csv_path)
