"""Integration tests for app/main.py's Task 6 canonical CLI pipeline:
explicit anchor/timezone requirements, multi-day allocate-then-select-one
behavior, and the exact-interval JSON export preserving short tasks and
non-grid boundaries that the legacy 30-minute CSV export cannot represent.
"""

from __future__ import annotations

import json

import pytest

from app.main import main

SAMPLE_CSV = "samples/inputs/valid_single_day_basic.csv"
MULTI_DAY_CSV = "samples/inputs/valid_multi_day_two_days.csv"


def test_default_invocation_runs_the_sample_fixture(tmp_path):
    legacy_out = tmp_path / "legacy.csv"
    exact_out = tmp_path / "exact.json"

    main(["--legacy-csv-out", str(legacy_out), "--exact-json-out", str(exact_out)])

    assert legacy_out.exists()
    assert exact_out.exists()


def test_non_sample_csv_requires_explicit_anchor_date(tmp_path):
    with pytest.raises(ValueError, match="anchor-date is required"):
        main(["--csv", MULTI_DAY_CSV, "--legacy-csv-out", str(tmp_path / "l.csv"), "--exact-json-out", str(tmp_path / "e.json")])


def test_multi_day_csv_generates_only_the_selected_date(tmp_path):
    exact_out = tmp_path / "exact.json"
    main([
        "--csv", MULTI_DAY_CSV, "--anchor-date", "2026-01-05", "--timezone", "UTC",
        "--legacy-csv-out", str(tmp_path / "legacy.csv"), "--exact-json-out", str(exact_out),
    ])

    data = json.loads(exact_out.read_text())
    assert data["date"] == "2026-01-05"

    task_names = {task["name"] for task in data["tasks"]["tasks"].values()}
    # Day 2's tasks (Edit Draft / Submit Assignment) must not appear in day 1's output.
    assert "Edit Draft" not in task_names
    assert "Submit Assignment" not in task_names


def test_explicit_select_date_picks_the_other_day(tmp_path):
    exact_out = tmp_path / "exact.json"
    main([
        "--csv", MULTI_DAY_CSV, "--anchor-date", "2026-01-05", "--timezone", "UTC",
        "--select-date", "2026-01-06",
        "--legacy-csv-out", str(tmp_path / "legacy.csv"), "--exact-json-out", str(exact_out),
    ])

    data = json.loads(exact_out.read_text())
    assert data["date"] == "2026-01-06"


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
    exact_out = tmp_path / "exact.json"

    main([
        "--csv", str(csv_path), "--anchor-date", "2026-01-05", "--timezone", "UTC",
        "--legacy-csv-out", str(tmp_path / "legacy.csv"), "--exact-json-out", str(exact_out),
    ])

    data = json.loads(exact_out.read_text())
    placements = data["placements"]
    assert len(placements) == 1
    placement = placements[0]
    assert placement["planned_start"] == "2026-01-05T10:13:00Z"
    assert placement["planned_end"] == "2026-01-05T10:16:00Z"


def test_midnight_boundary_preserved_exactly():
    """samples/inputs/end_of_day_boundary_1440.csv's fixed block ending
    exactly at 24:00 must round-trip to the following date at 00:00, not
    be silently misreported as noon or otherwise mangled."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        exact_out = tmp_path / "exact.json"
        main([
            "--csv", "samples/inputs/end_of_day_boundary_1440.csv", "--anchor-date", "2026-01-05", "--timezone", "UTC",
            "--legacy-csv-out", str(tmp_path / "legacy.csv"), "--exact-json-out", str(exact_out),
        ])

        data = json.loads(exact_out.read_text())
        fixed_blocks = {block["label"]: block for block in data["fixed_blocks"]}
        assert fixed_blocks["Late Night Work"]["planned_end"] == "2026-01-06T00:00:00Z"
