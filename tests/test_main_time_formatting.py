"""Characterization + regression tests for app/main.py's time-formatting and
CSV export helpers, plus one CSV -> optimizer -> exporter integration test.

minutes_to_time had a confirmed bug: minutes_to_time(1440) printed "12:00 PM"
instead of "12:00 AM" because it used the raw hour (24) rather than hour % 24
when deciding AM/PM. These tests lock in the fix and characterize the
previously-correct cases so they are not regressed by the fix.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

import app.reward as reward_module
from app.data_processor import load_schedule_from_csv
from app.main import export_day_schedule_to_csv, minutes_to_24_hour_time, minutes_to_time
from app.optimizer import combine_fixed_and_optimized_scheduled_tasks


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, "12:00 AM"),  # midnight, start of day
        (540, "9:00 AM"),
        (720, "12:00 PM"),  # noon
        (1439, "11:59 PM"),
        (1440, "12:00 AM"),  # regression: end-of-day must not display as noon
    ],
)
def test_minutes_to_time_boundaries(minutes: int, expected: str) -> None:
    assert minutes_to_time(minutes) == expected


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, "00:00"),
        (870, "14:30"),
        (1439, "23:59"),
        (1440, "24:00"),
    ],
)
def test_minutes_to_24_hour_time_is_unaffected(minutes: int, expected: str) -> None:
    """Characterization test: this function was not touched by the bug fix."""
    assert minutes_to_24_hour_time(minutes) == expected


class _FakeTimeWindow:
    def __init__(self, start_time: int, end_time: int) -> None:
        self.start_time = start_time
        self.end_time = end_time


class _FakeTask:
    def __init__(self, name: str, start_time: int, end_time: int) -> None:
        self.name = name
        self.time_window = _FakeTimeWindow(start_time, end_time)


class _FakeDayOutput:
    def __init__(self, scheduled_tasks: list[_FakeTask]) -> None:
        self.scheduled_tasks = scheduled_tasks


def test_export_day_schedule_to_csv_characterization(tmp_path: Path) -> None:
    """Characterization test for the export helper, which this change does not modify."""
    day_output = _FakeDayOutput(
        [
            _FakeTask("Sleep", 0, 480),
            _FakeTask("Study Math", 540, 660),
        ]
    )
    output_path = tmp_path / "export.csv"

    export_day_schedule_to_csv(day_output, output_path)

    with output_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))

    assert rows[0] == ["time", "task"]
    assert len(rows) == 1 + 48  # header + 48 half-hour blocks

    by_time = dict(rows[1:])
    assert by_time["00:00"] == "Sleep"
    assert by_time["07:30"] == "Sleep"
    assert by_time["08:00"] == "-"
    assert by_time["09:00"] == "Study Math"
    assert by_time["10:30"] == "Study Math"
    assert by_time["11:00"] == "-"


def test_csv_to_optimizer_to_export_pipeline(tmp_path: Path, monkeypatch) -> None:
    """Integration test for the real CLI path: a CSV file on disk, through
    load_schedule_from_csv and the optimizer, into export_day_schedule_to_csv.

    The input CSV schema (date,name,category,tag,fixed,start_time,end_time,
    duration,priority,dependencies) and the output CSV schema (time,task) are
    intentionally different -- this test does not assume or check any
    round-trip between them, only that each stage of the real pipeline
    produces what the next stage expects.
    """
    original_resolve = reward_module._resolve_config_path
    monkeypatch.setattr(
        reward_module,
        "_resolve_config_path",
        lambda config_path=None: None if config_path is None else original_resolve(config_path),
    )

    input_csv = tmp_path / "input.csv"
    input_csv.write_text(
        "date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
        "1,Sleep,sleep,fixed,True,0,480,480,1,\n"
        "1,Study Math,study,math,False,540,600,60,8,\n",
        encoding="utf-8",
    )

    schedule_input = load_schedule_from_csv(str(input_csv))
    day_schedule = schedule_input.schedules[1]

    output = combine_fixed_and_optimized_scheduled_tasks(date=1, day_schedule=day_schedule)

    by_name = {task.name: task for task in output.scheduled_tasks}
    assert by_name["Sleep"].time_window.start_time == 0
    assert by_name["Sleep"].time_window.end_time == 480
    assert by_name["Study Math"].time_window.start_time == 540
    assert by_name["Study Math"].time_window.end_time == 600

    output_csv = tmp_path / "output.csv"
    export_day_schedule_to_csv(output, output_csv)

    with output_csv.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))

    assert rows[0] == ["time", "task"]
    assert len(rows) == 1 + 48  # header + 48 half-hour blocks

    by_time = dict(rows[1:])
    assert by_time["00:00"] == "Sleep"
    assert by_time["07:30"] == "Sleep"
    assert by_time["08:00"] == "-"  # exclusive end boundary: Sleep ends at 480 (08:00)
    assert by_time["09:00"] == "Study Math"
    assert by_time["09:30"] == "Study Math"
    assert by_time["10:00"] == "-"  # exclusive end boundary: Study Math ends at 600 (10:00)
