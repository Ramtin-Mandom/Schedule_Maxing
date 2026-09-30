"""The opt-in probe records timing metadata, never callback arguments."""
import tkinter as tk

from benchmarks.desktop_responsiveness import Probe, baseline_ui, distribution, reference_behavior, source_hashes


def test_distribution_nearest_rank_and_empty():
    assert distribution([]) == {"n": 0, "p95_ms": 0, "max_ms": 0}
    assert distribution(list(range(1, 101))) == {"n": 100, "p95_ms": 95, "max_ms": 100}


def test_probe_preserves_callback_result_and_restores_hook():
    original = tk.CallWrapper.__call__
    probe = Probe()
    try:
        def callback(secret):
            return secret

        wrapped = tk.CallWrapper(callback, None, None)
        assert wrapped("private payload") == "private payload"
        report = probe.report()
        assert "private payload" not in str(report)
        assert next(iter(report["startup"]["callbacks"].values()))["n"] == 1
    finally:
        probe.close()
    assert tk.CallWrapper.__call__ is original


def test_reference_mode_is_scoped_and_restores_production_methods():
    import customtkinter as ctk
    from app.ui.paint_widgets import AppOptionMenu, AppScrollableFrame
    from app.ui.tk_lifecycle import DesktopCollection
    from app.ui.day_page import DaySchedulePage
    from app.ui.task_editor import DependencyPicker

    methods = [(AppOptionMenu, "_draw"), (AppScrollableFrame, "__init__"),
               (DesktopCollection, "collect"), (DaySchedulePage, "_show_available"),
               (DependencyPicker, "set_choices")]
    originals = [getattr(owner, name) for owner, name in methods]
    with reference_behavior():
        assert AppOptionMenu._draw is ctk.CTkOptionMenu._draw
        assert AppScrollableFrame.__init__ is ctk.CTkScrollableFrame.__init__
        assert DesktopCollection.collect is not originals[2]
    assert [getattr(owner, name) for owner, name in methods] == originals


def test_source_manifest_detects_edits_without_recording_contents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app").mkdir()
    source = tmp_path / "app" / "example.py"
    source.write_text("private content", encoding="utf-8")
    before = source_hashes()
    assert "private content" not in str(before)
    source.write_text("changed", encoding="utf-8")
    assert source_hashes() != before


def test_baseline_snapshot_is_scoped_and_restores_current_classes():
    from pathlib import Path
    import app.app as app_module
    import app.ui.day_page as day_module
    import app.ui.task_status_board as board_module

    day, board = day_module.DaySchedulePage, board_module.TaskStatusBoard
    path = Path(__file__).resolve().parents[2] / "benchmarks" / "round2-baseline-ui.json"
    with baseline_ui(path):
        assert app_module.DaySchedulePage is day_module.DaySchedulePage
        assert day_module.DaySchedulePage is not day
        assert board_module.TaskStatusBoard is not board
    assert app_module.DaySchedulePage is day_module.DaySchedulePage is day
    assert board_module.TaskStatusBoard is board
