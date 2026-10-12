"""
Input bounds and export safety that do not need a server: CSV imports are refused when oversized
(before any row is interpreted), reward settings must be small files of finite numbers, and the
spreadsheet-facing exports never write a cell a spreadsheet would run.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from app import reward
from app.csv_safety import safe_row, spreadsheet_safe
from app.planning import csv_import
from app.planning.csv_canonical import parse_canonical_csv
from app.planning.csv_import import CsvImportError, parse_legacy_csv, read_csv_text


def legacy(text: str):
    return parse_legacy_csv(text, anchor_date=date(2026, 3, 2), timezone="UTC")


@pytest.mark.parametrize("parse", [legacy, parse_canonical_csv])
def test_oversized_imports_are_refused_before_anything_is_interpreted(parse, monkeypatch) -> None:
    monkeypatch.setattr(csv_import, "MAX_IMPORT_ROWS", 50)
    monkeypatch.setattr(csv_import, "MAX_FIELD_CHARS", 200)
    monkeypatch.setattr(csv_import, "MAX_IMPORT_BYTES", 20_000)
    too_many_rows = "name,category\n" + "x,y\n" * 60
    long_cell = "name,category\n" + "x" * 300 + ",y\n"
    too_big = "name,category\n" + ("x" * 150 + ",y\n") * 200
    huge_field = "name,category\n\"" + "x" * 200_000 + "\",y\n"  # beyond the csv module's own field limit
    for text, expected in ((too_many_rows, "rows"), (long_cell, "longer than"), (too_big, "larger than"),
                           (huge_field, "larger than")):
        with pytest.raises(CsvImportError) as refused:
            parse(text)
        assert expected in str(refused.value), expected
        assert "xxxxxxxxxx" not in str(refused.value)  # the content is not echoed back


def test_a_file_too_large_to_be_a_planning_file_is_not_even_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(csv_import, "MAX_IMPORT_BYTES", 1000)
    big = tmp_path / "big.csv"
    big.write_text("name\n" + "x" * 5000, encoding="utf-8")
    with pytest.raises(CsvImportError, match="larger than"):
        read_csv_text(big)
    small = tmp_path / "small.csv"
    small.write_text("name\nok\n", encoding="utf-8")
    assert read_csv_text(small) == "name\nok\n"


@pytest.mark.parametrize("cell", ["=1+1", "+SUM(A1:A9)", "-2+3", "@cmd", '=HYPERLINK("http://evil.example","x")',
                                  "\t=1+1", "\r=1+1"])
def test_cells_a_spreadsheet_would_run_are_made_inert(cell: str) -> None:
    assert spreadsheet_safe(cell) == "'" + cell


def test_ordinary_values_are_untouched_and_reports_use_the_protection(tmp_path: Path) -> None:
    for value in ("Study", "a=b", "3-4pm", "", "name@example.com", 5, -5, -1.5, None, True):
        assert spreadsheet_safe(value) == value and type(spreadsheet_safe(value)) is type(value)
    assert safe_row({"name": "=1", "minutes": -5}) == {"name": "'=1", "minutes": -5}
    assert safe_row(["09:00", "@x"]) == ["09:00", "'@x"]
    # The reports opened in spreadsheets go through it; the lossless planning export deliberately does not.
    import app.execution.exporters as execution_exporters
    import app.planning.csv_export as planning_export
    import app.productivity.exporters as productivity_exporters

    assert execution_exporters.safe_row is safe_row and productivity_exporters.safe_row is safe_row
    assert not hasattr(planning_export, "safe_row")
    target = tmp_path / "out.csv"
    with target.open("w", newline="", encoding="utf-8") as file:
        csv.writer(file).writerow(safe_row(["=cmd|' /C calc'!A0", "plain"]))
    assert target.read_text(encoding="utf-8").startswith("'=cmd")


def test_reward_settings_must_be_finite_and_the_file_small(tmp_path: Path) -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            reward.RewardSettings(weight_importance=bad)
        with pytest.raises(ValueError, match="finite"):
            reward.RewardSettings(category_weights={"study": bad})
    assert reward.RewardSettings(weight_importance=2.5).weight_importance == 2.5

    config = tmp_path / "task_preference.yaml"
    config.write_text("weights:\n  importance: .nan\n", encoding="utf-8")
    with pytest.raises(ValueError, match="finite"):
        reward.load_reward_settings(config)
    config.write_text("weights:\n  importance: 1.5\n# " + "x" * (reward.MAX_CONFIG_BYTES + 10) + "\n",
                      encoding="utf-8")
    with pytest.raises(ValueError, match="larger than"):
        reward.load_reward_settings(config)
    config.write_text("weights:\n  importance: 1.5\n", encoding="utf-8")
    assert reward.load_reward_settings(config).weight_importance == 1.5
    # Plain data only: a YAML tag that would build a Python object is refused by the safe loader.
    config.write_text("weights:\n  importance: !!python/object/apply:os.system ['echo hi']\n", encoding="utf-8")
    with pytest.raises(Exception) as refused:
        reward.load_reward_settings(config)
    assert "python" in str(refused.value).lower() or "constructor" in type(refused.value).__name__.lower()
