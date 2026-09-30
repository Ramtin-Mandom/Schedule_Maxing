"""Native viewport, stale-render, and card-version regressions."""
from dataclasses import replace
import time
import uuid

from app.ui.shell_state import LayoutMode
from app.ui.task_status import StatusBoard
from tests.ui.test_desktop_app import open_app, close_app, pump
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401


def test_large_available_list_scrolls_to_every_task_and_keeps_versions(tmp_path, monkeypatch):
    import customtkinter as ctk
    from app.ui.day_controller import UnplacedTask
    from app.ui.schedule_page_controller import RowRef

    app = open_app(tmp_path / "viewport.db", tmp_path)
    try:
        app.deiconify()
        day = app.pages["day"]
        tasks = [UnplacedTask(RowRef("task", uuid.uuid4(), 1), f"Task {i}", "study", 15, False, "Any date")
                 for i in range(200)]
        day._show_available(replace(day.snapshot, unplaced=tasks))
        pump(app)
        assert 0 < len(day.chips) < 25
        canvas = day.chip_area._parent_canvas
        seen = set()
        for fraction in (i / 20 for i in range(21)):
            canvas.yview_moveto(fraction)
            pump(app)
            seen.update(chip.task.ref for chip in day.chips)
        assert seen == {task.ref for task in tasks}
        last = day.chips[-1]
        assert last.task == tasks[-1]
        before = canvas.yview()[0]
        tasks[-1] = replace(tasks[-1], ref=replace(tasks[-1].ref, version=2))
        day._show_available(replace(day.snapshot, unplaced=tasks))
        pump(app)
        assert day.chips[-1] is last
        assert abs(canvas.yview()[0] - before) < .01
        selected = []
        monkeypatch.setattr(day, "edit_ref", selected.append)
        last.invoke()
        assert selected[-1].version == 2
        day.set_layout(LayoutMode.NARROW)
        pump(app)
        canvas.yview_moveto(1)
        pump(app)
        assert day.chips[-1].task == tasks[-1]
        assert len(day.chips) < 10
        canvas.yview_moveto(0)
        pump(app)
        ref = day.chips[-1].task.ref
        assert day._focus_available(ref, 1) == "break"
        pump(app)
        index = next(i for i, task in enumerate(tasks) if task.ref == ref)
        assert any(chip.task.ref == tasks[index + 1].ref for chip in day.chips)
        old_scale = day._chip_scale
        ctk.set_widget_scaling(1.25)
        pump(app)
        canvas.yview_moveto(1)
        pump(app)
        assert day.chips[-1].task == tasks[-1]
        assert abs(day._chip_scale / old_scale - 1.25) < .01
        day._show_available(replace(day.snapshot, unplaced=[]))
        pump(app)
        assert not day.chips and not last.winfo_exists()
        day._chip_refresh.request()
        pending = day._chip_refresh._pending
        assert pending is not None
        day.destroy()  # account replacement can destroy a page before its idle refresh
        assert pending not in app.tk.call("after", "info")
    finally:
        close_app(app)
        ctk.set_widget_scaling(1)


def test_card_batches_yield_reuse_and_cancel_stale_results(tmp_path):
    from tests.ui.test_day_status_board import scheduled_day

    app, day = scheduled_day(tmp_path / "cards.db", tmp_path, ("One",))
    try:
        board = day.status_board
        original = board.board.cards[0]
        cards = [replace(original, task=original.task.model_copy(update={"name": "Wrapped task name " * (1 + i % 4)}),
                         placement=original.placement.model_copy(update={"id": uuid.uuid4()}))
                 for i in range(40)]
        board.render(StatusBoard(day=board.board.day, cards=cards))
        assert board._render_timer is not None and len(board.card_widgets) < 40
        assert all(not value["frame"].winfo_manager() for value in board.card_widgets.values())
        yielded = []
        app.after(0, lambda: yielded.append(len(board.card_widgets)))
        deadline = time.monotonic() + 10
        while board._render_timer is not None:
            app.update()
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert yielded and yielded[0] < 40
        assert len(board.card_widgets) == 40
        app.deiconify()
        pump(app, until=lambda: not board.viewport_updates.pending)
        canvas = day.body._parent_canvas
        extent = canvas.bbox("all")[3]
        canvas.yview_moveto(1)
        pump(app, until=lambda: not board.viewport_updates.pending)
        assert 0 < sum(bool(w["frame"].winfo_manager()) for w in board.card_widgets.values()) < 40
        assert board.card_widgets[cards[-1].key]["frame"].winfo_manager() == "grid"
        assert not board.card_widgets[cards[0].key]["frame"].winfo_manager()
        assert abs(canvas.bbox("all")[3] - extent) <= 2
        board._focus_card(cards[1], -1, "right")
        pump(app, until=lambda: not board.viewport_updates.pending)
        assert board.card_widgets[cards[0].key]["frame"].winfo_manager() == "grid"
        widgets = dict(board.card_widgets)
        board.render(StatusBoard(day=board.board.day, cards=cards))
        assert all(board.card_widgets[key] is value for key, value in widgets.items())
        board.set_layout(LayoutMode.NARROW)
        assert all(board.card_widgets[key] is value for key, value in widgets.items())
        current = replace(cards[0], task=cards[0].task.model_copy(update={"version": 7}))
        selected = []
        board._on_move = lambda card, target: selected.append(card)
        board.render(StatusBoard(cards=[current]))
        board.card_widgets[current.key]["right"].invoke()
        assert selected[-1].task.version == 7
        board.render(StatusBoard(cards=cards))
        board.render(StatusBoard())  # account/date replacement before the next batch
        pump(app)
        assert not board.card_widgets and board._render_timer is None
        board.render(StatusBoard(cards=cards))
        board.destroy()
        assert board._render_timer is None
        pump(app)
    finally:
        close_app(app)
