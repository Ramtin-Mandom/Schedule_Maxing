"""Regression coverage for nested paint dispatch and idle full-heap scans."""
import threading
import tkinter as tk

import pytest

from app.ui import tk_lifecycle


class Timers:
    def __init__(self):
        self.pending = {}
        self.next = 0

    def after(self, delay, callback):
        self.next += 1
        self.pending[self.next] = (delay, callback)
        return self.next

    def after_cancel(self, timer):
        self.pending.pop(timer, None)

    def tick(self):
        timer = next(iter(self.pending))
        _, callback = self.pending.pop(timer)
        callback()


@pytest.fixture
def collection_gc(monkeypatch):
    calls = []
    monkeypatch.setattr(tk_lifecycle, "_owners", 0)
    monkeypatch.setattr(tk_lifecycle, "_restore_automatic", False)
    monkeypatch.setattr(tk_lifecycle.gc, "isenabled", lambda: True)
    monkeypatch.setattr(tk_lifecycle.gc, "disable", lambda: calls.append("disable"))
    monkeypatch.setattr(tk_lifecycle.gc, "enable", lambda: calls.append("enable"))
    monkeypatch.setattr(tk_lifecycle.gc, "get_threshold", lambda: (700, 10, 10))
    monkeypatch.setattr(tk_lifecycle.gc, "collect", lambda generation: calls.append((generation, threading.get_ident())))
    return calls


@pytest.mark.parametrize("counts, generation", [((0, 0, 0), None), ((699, 50, 50), None),
                                                   ((701, 0, 0), 0), ((701, 11, 0), 1), ((701, 11, 11), 2)])
def test_collection_is_allocation_driven_and_on_owner_thread(monkeypatch, collection_gc, counts, generation):
    monkeypatch.setattr(tk_lifecycle.gc, "get_count", lambda: counts)
    root = Timers()
    collection = tk_lifecycle.DesktopCollection(root)
    root.tick()
    assert len(root.pending) == 1
    expected = [] if generation is None else [(generation, threading.get_ident())]
    assert collection_gc == ["disable", *expected]
    collection.close()
    collection.close()
    collection.collect()  # stale callback after shutdown does not collect or reschedule
    assert root.pending == {}
    assert collection_gc == ["disable", *expected, "enable"]


def test_collection_restores_gc_only_after_last_window(collection_gc):
    one, two = tk_lifecycle.DesktopCollection(Timers()), tk_lifecycle.DesktopCollection(Timers())
    one.close()
    assert collection_gc == ["disable"]
    two.close()
    assert collection_gc == ["disable", "enable"]


def test_collection_preserves_originally_disabled_gc(monkeypatch, collection_gc):
    monkeypatch.setattr(tk_lifecycle.gc, "isenabled", lambda: False)
    collection = tk_lifecycle.DesktopCollection(Timers())
    collection.close()
    assert collection_gc == ["disable"]


def test_paint_does_not_dispatch_pending_idle_callbacks():
    import customtkinter as ctk
    from app.ui.paint_widgets import AppOptionMenu, AppScrollableFrame, AppTextbox

    try:
        root = ctk.CTk()
    except tk.TclError:
        pytest.skip("no display available for Tk")
    collection = tk_lifecycle.DesktopCollection(root)
    try:
        root.geometry("600x400")
        frame = AppScrollableFrame(root, height=150)
        frame.pack(fill="both", expand=True)
        for index in range(20):
            ctk.CTkLabel(frame, text=f"Synthetic {index}").pack()
        menu = AppOptionMenu(root, values=["One", "Two"])
        menu.pack()
        text = AppTextbox(root, height=40)
        text.pack()
        text.insert("1.0", "Synthetic text\n" * 10)
        root.update()
        dispatched = []
        root.after_idle(lambda: dispatched.append(threading.get_ident()))
        menu.configure(width=250, fg_color="blue")
        frame._scrollbar.set(.2, .4)
        text._x_scrollbar.set(.2, .4)
        text._y_scrollbar.set(.2, .4)
        assert dispatched == []  # old CTk drawing drains this callback synchronously
        root.update_idletasks()  # explicit caller flush remains fully functional
        assert dispatched == [threading.get_ident()]
        menu.set("Two")
        assert menu.get() == "Two"
        frame._parent_canvas.yview_moveto(.5)
        root.update_idletasks()
        assert frame._parent_canvas.yview()[0] > 0
        root.geometry("800x500")
        root.update()
        assert frame.winfo_width() > 400
        menu.destroy()
        frame.destroy()
        root.update()  # adapters own no deferred callbacks that could hit destroyed widgets
    finally:
        root.destroy()
        tk_lifecycle.release_resources(root)
        collection.close()


def test_unchanged_dependency_choices_do_not_fire_variable_traces():
    import uuid
    import customtkinter as ctk
    from app.ui.task_editor import DependencyPicker
    from app.ui.task_form_model import Choice

    try:
        root = ctk.CTk()
    except tk.TclError:
        pytest.skip("no display available for Tk")
    collection = tk_lifecycle.DesktopCollection(root)
    try:
        picker = DependencyPicker(root)
        choices = [Choice(uuid.uuid4(), "Synthetic")]
        picker.set_choices(choices)
        writes = []
        var = picker.vars[choices[0].id]
        var.trace_add("write", lambda *_: writes.append(True))
        picker.set_choices(choices)
        assert writes == []
        picker.set_choices(choices, (choices[0].id,))
        assert writes == [True] and picker.selected() == (choices[0].id,)
        picker.set_choices(choices)
        assert writes == [True]  # a reload preserves the actual selection
    finally:
        root.destroy()
        tk_lifecycle.release_resources(root)
        collection.close()


def test_available_chips_reuse_widgets_but_refresh_versions_and_remove_deleted_tasks(tmp_path, monkeypatch):
    from app.planning.models import Task
    from tests.ui.test_desktop_app import open_app, close_app, pump, WEDNESDAY, _display_available

    if not _display_available():
        pytest.skip("no display available for Tk")
    app = open_app(tmp_path / "chips.db", tmp_path)
    try:
        planning, day = app.services.planning_controller, app.pages["day"]
        task = planning.add_or_update_task(Task(name="Synthetic", category="study", priority=5,
                                               estimated_duration_minutes=15, preferred_dates=[WEDNESDAY])).value
        day.reload()
        pump(app)
        chip = day.chips[0]
        day.reload()
        pump(app)
        assert day.chips[0] is chip
        changed = planning.add_or_update_task(task.model_copy(update={"priority": 6}), expected_version=task.version)
        assert changed.ok
        day.reload()
        pump(app)
        assert day.chips[0] is chip
        selected = []
        monkeypatch.setattr(day, "edit_ref", selected.append)
        chip.invoke()
        pump(app)
        assert selected[0].version == changed.value.version
        assert planning.remove_task(task.id, expected_version=changed.value.version).ok
        day.reload()
        pump(app)
        assert day.chips == [] and not chip.winfo_exists()
    finally:
        close_app(app)
