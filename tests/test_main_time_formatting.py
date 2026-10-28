"""Characterization + regression tests for app/main.py's time-formatting and
CSV export helpers.

minutes_to_time had a confirmed bug: minutes_to_time(1440) printed "12:00 PM"
instead of "12:00 AM" because it used the raw hour (24) rather than hour % 24
when deciding AM/PM. These tests lock in the fix and characterize the
previously-correct cases so they are not regressed by the fix.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.main import export_day_schedule_to_csv, minutes_to_24_hour_time, minutes_to_time


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
