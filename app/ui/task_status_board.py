"""
app/ui/task_status_board.py

The Day page's execution board: three rounded columns side by side,

    Uncompleted          Tasks                 Completed
    [ card      → ]      [ × card      → ]     [ × card      ]

drawn from a StatusBoard (app/ui/task_status.py). In Tasks, × marks a task
uncompleted and → completed; in Uncompleted, → brings it back to Tasks; in
Completed, × brings it back. Widgets only: every button calls
on_move(card, target), and the page persists the move and redraws from what
was saved. On a narrow window the columns stack (Uncompleted, Tasks,
Completed, top to bottom) with the same controls.
"""

from __future__ import annotations

from collections.abc import Callable
from collections import deque
import time

import customtkinter as ctk

from app.execution.lifecycle import TaskOutcome
from app.ui import theme
from app.ui.components import AppButton, Card, Notice, SectionTitle, Tooltip, font, focus_target
from app.ui.layout import Coalescer
from app.ui.shell_state import LayoutMode
from app.ui.task_status import COLUMN_TITLES, COLUMNS, StatusBoard, StatusCard

_EMPTY_TEXT = {
    TaskOutcome.UNCOMPLETED: "Nothing marked uncompleted.",
    TaskOutcome.PENDING: "No scheduled tasks waiting.",
    TaskOutcome.COMPLETED: "Nothing completed yet.",
}
#: (button text, target column, tooltip) on the left and right of a card, per column.
_CONTROLS = {
    TaskOutcome.PENDING: (("×", TaskOutcome.UNCOMPLETED, "Mark uncompleted"),
                          ("→", TaskOutcome.COMPLETED, "Mark completed")),
    TaskOutcome.UNCOMPLETED: (None, ("→", TaskOutcome.PENDING, "Back to Tasks")),
    TaskOutcome.COMPLETED: (("×", TaskOutcome.PENDING, "Back to Tasks"), None),
}
_WRAP = {LayoutMode.WIDE: 190, LayoutMode.MEDIUM: 150, LayoutMode.NARROW: 300}


class TaskStatusBoard(Card):
    def __init__(self, parent, *, on_move: Callable[[StatusCard, TaskOutcome], None]) -> None:
        super().__init__(parent)
        self._on_move = on_move
        self.board = StatusBoard()
        self.layout: LayoutMode | None = None
        self._busy = False
        self._render_timer = None
        self._pending_cards = deque()
        self._staged_cards = []
        self._viewport_canvas = None
        self._reserved_rows = {outcome: [] for outcome in COLUMNS}
        self.viewport_updates = Coalescer(self, self._refresh_viewport, delay_ms=20)
        self.bind("<Configure>", lambda event: self.viewport_updates.request(), add="+")
        self.columnconfigure(0, weight=1)
        SectionTitle(self, "Scheduled tasks", "Only tasks on the saved schedule. × marks a task uncompleted, → marks "
                                              "it completed; the arrows bring it back.", wraplength=620).grid(
            row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 6))
        self.notice = Notice(self, wraplength=620)
        self.notice.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        self.notice.hide()
        self.grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.grid_frame.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_M, pady=(6, theme.SPACE_L))
        self.columns: dict[TaskOutcome, ctk.CTkFrame] = {}
        self.count_labels: dict[TaskOutcome, ctk.CTkLabel] = {}
        self.lists: dict[TaskOutcome, ctk.CTkFrame] = {}
        self.empty_labels: dict[TaskOutcome, ctk.CTkLabel] = {}
        for outcome in COLUMNS:
            column = ctk.CTkFrame(self.grid_frame, fg_color=theme.SUBTLE_BG, corner_radius=theme.RADIUS_CARD,
                                  border_width=1, border_color=theme.CARD_BORDER)
            column.columnconfigure(0, weight=1)
            head = ctk.CTkFrame(column, fg_color="transparent")
            head.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_M, pady=(theme.SPACE_M, 6))
            head.columnconfigure(0, weight=1)
            ctk.CTkLabel(head, text=COLUMN_TITLES[outcome], font=font(theme.SIZE_HEADING, "bold"),
                         text_color=theme.TEXT_PRIMARY, anchor="w").grid(row=0, column=0, sticky="w")
            count = ctk.CTkLabel(head, text="0", font=font(theme.SIZE_SMALL, "bold"), corner_radius=10, padx=8,
                                 fg_color=theme.SECONDARY_BG, text_color=theme.TEXT_PRIMARY)
            count.grid(row=0, column=1, sticky="e")
            items = ctk.CTkFrame(column, fg_color="transparent")
            items.grid(row=1, column=0, sticky="nsew", padx=theme.SPACE_S, pady=(0, theme.SPACE_M))
            items.columnconfigure(0, weight=1)
            empty = ctk.CTkLabel(items, text=_EMPTY_TEXT[outcome], font=font(theme.SIZE_SMALL),
                                 text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=_WRAP[LayoutMode.WIDE])
            self.columns[outcome], self.count_labels[outcome] = column, count
            self.lists[outcome], self.empty_labels[outcome] = items, empty
        #: placement id -> the widgets of its card (for tests and focus)
        self.card_widgets: dict = {}
        self.set_layout(LayoutMode.WIDE)
        self.render(StatusBoard())

    # ------------------------------------------------------------------ layout

    def set_layout(self, mode: LayoutMode) -> None:
        if mode == self.layout:
            return
        self.layout = mode
        for index in range(3):
            self.grid_frame.columnconfigure(index, weight=0, uniform="")
        for column in self.columns.values():
            column.grid_forget()
        if mode == LayoutMode.NARROW:
            self.grid_frame.columnconfigure(0, weight=1)
            for row, outcome in enumerate(COLUMNS):
                self.columns[outcome].grid(row=row, column=0, sticky="new", pady=4)
        else:
            for index, outcome in enumerate(COLUMNS):
                self.grid_frame.columnconfigure(index, weight=1, uniform="status")
                self.columns[outcome].grid(row=0, column=index, sticky="nsew", padx=4)
        self.render(self.board)

    # ------------------------------------------------------------------ drawing

    def render(self, board: StatusBoard) -> None:
        self._cancel_render()
        self.board = board
        old = self.card_widgets
        self.card_widgets = {}
        wrap = _WRAP[self.layout or LayoutMode.WIDE]
        for outcome in COLUMNS:
            cards = board.column(outcome)
            self.count_labels[outcome].configure(text=str(len(cards)))
            empty = self.empty_labels[outcome]
            empty.configure(wraplength=wrap)
            if cards:
                empty.grid_remove()
            else:
                if outcome == TaskOutcome.PENDING and not board.cards:
                    empty.configure(text="No scheduled tasks for this date. Add tasks, then Make Schedule.")
                else:
                    empty.configure(text=_EMPTY_TEXT[outcome])
                empty.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
            for row, card in enumerate(cards):
                widgets = old.pop(card.key, None)
                if widgets is not None and self._appearance(widgets["card"]) != self._appearance(card):
                    widgets["frame"].destroy()
                    widgets = None
                if widgets is None:
                    self._pending_cards.append((row, outcome, card, wrap))
                else:
                    # Callbacks read this current card, including its execution version.
                    widgets["card"] = card
                    if widgets["wrap"] != wrap:
                        for label in widgets["labels"]:
                            label.configure(wraplength=wrap)
                        widgets["wrap"] = wrap
                    if widgets["row"] != row:
                        if widgets["placed"]:
                            widgets["frame"].grid_configure(row=row)
                        widgets["row"] = row
                    if not widgets["placed"]:
                        self._staged_cards.append(widgets)
                    self.card_widgets[card.key] = widgets
        for widgets in old.values():
            widgets["frame"].destroy()
        self.set_busy(self._busy)
        self._fill_cards()

    @staticmethod
    def _appearance(card):
        return card.outcome, card.name, card.category, card.detail, card.status_note

    def _cancel_render(self):
        if self._render_timer is not None:
            self.after_cancel(self._render_timer)
            self._render_timer = None
        self._pending_cards.clear()
        self._staged_cards.clear()

    def destroy(self):
        self._cancel_render()
        self.viewport_updates.cancel()
        super().destroy()

    def observe_viewport(self, canvas):
        """Use the Day page's existing scroll viewport, without adding nested scrolling."""
        self._viewport_canvas = canvas
        canvas.bind("<Configure>", lambda event: self.viewport_updates.request(), add="+")
        self.viewport_updates.request()

    def _refresh_viewport(self):
        canvas = self._viewport_canvas
        if canvas is None or canvas.winfo_height() <= 1 or self._pending_cards:
            return
        top = canvas.winfo_rooty() - 100
        bottom = top + canvas.winfo_height() + 200
        virtual = len(self.board.cards) > 12
        padding = 2 * round(3 * self._get_widget_scaling())
        focused = set()
        widget = self.focus_get()
        while widget is not None:
            focused.add(widget)
            widget = getattr(widget, "master", None)
        for outcome in COLUMNS:
            parent = self.lists[outcome]
            offset = parent.winfo_rooty()
            heights = []
            previous = self._reserved_rows[outcome]
            for row, card in enumerate(self.board.column(outcome)):
                widgets = self.card_widgets.get(card.key)
                if widgets is None or not widgets["placed"]:
                    break
                frame = widgets["frame"]
                height = frame.winfo_reqheight() + padding
                # The first native layout determines the wrapped label heights.
                # Keep initial cards mapped until that request has settled.
                ready = frame.winfo_height() > 1
                reserve = height if virtual and ready else 0
                heights.append(reserve)
                if row >= len(previous) or previous[row] != reserve:
                    parent.rowconfigure(row, minsize=reserve)
                visible = (not virtual or not ready or frame in focused
                           or (offset < bottom and offset + height > top))
                if visible and not frame.winfo_manager():
                    frame.grid(row=row, column=0, sticky="ew", pady=3)
                elif not visible and frame.winfo_manager():
                    frame.grid_remove()
                offset += height
            for row in range(len(heights), len(previous)):
                parent.rowconfigure(row, minsize=0)
            self._reserved_rows[outcome] = heights

    def _focus_card(self, card, direction, side):
        cards = self.board.column(card.outcome)
        index = next((i for i, value in enumerate(cards) if value.key == card.key), None)
        if index is None:
            return "break"
        index = max(0, min(len(cards) - 1, index + direction))
        widgets = self.card_widgets.get(cards[index].key)
        canvas = self._viewport_canvas
        if widgets is None or not widgets["placed"] or canvas is None:
            return "break"
        parent = self.lists[card.outcome]
        heights = self._reserved_rows[card.outcome]
        y = (parent.winfo_rooty() + sum(heights[:index]) if len(self.board.cards) > 12
             else widgets["frame"].winfo_rooty())
        extent = canvas.bbox("all")
        if extent and (y < canvas.winfo_rooty() or y > canvas.winfo_rooty() + canvas.winfo_height() - 100):
            canvas.yview_moveto((canvas.canvasy(0) + y - canvas.winfo_rooty()) / max(1, extent[3]))
        widgets["frame"].grid(row=index, column=0, sticky="ew", pady=3)
        focus_target(widgets[side]).focus_set()
        self.viewport_updates.request()
        return "break"

    def _fill_cards(self):
        """A bounded Tk-thread batch; a newer board or destruction cancels the rest."""
        self._render_timer = None
        deadline = time.perf_counter() + .008
        while self._pending_cards:
            row, outcome, card, wrap = self._pending_cards.popleft()
            widgets = self._card(self.lists[outcome], outcome, card, wrap)
            widgets["row"] = row
            widgets["placed"] = False
            self.card_widgets[card.key] = widgets
            self._staged_cards.append(widgets)
            self._card_busy(widgets)
            if len(self.board.cards) > 12 and time.perf_counter() >= deadline:
                break
        if self._pending_cards:
            self._render_timer = self.after(10, self._fill_cards)
        else:
            # Construct off-grid, then let Tk lay out the new cards once. Mapping
            # every batch separately repeatedly redraws the growing, tall board.
            for widgets in self._staged_cards:
                widgets["frame"].grid(row=widgets["row"], column=0, sticky="ew", pady=3)
                widgets["placed"] = True
            self._staged_cards.clear()
            self.viewport_updates.request()

    def _card(self, parent, outcome: TaskOutcome, card: StatusCard, wrap: int) -> dict:
        style = theme.category_style(card.category)
        frame = ctk.CTkFrame(parent, fg_color=style.fill, corner_radius=theme.RADIUS_CONTROL)
        frame.columnconfigure(1, weight=1)
        widgets = {"frame": frame, "card": card, "wrap": wrap, "labels": []}
        left, right = _CONTROLS[outcome]
        for column, control in ((0, left), (2, right)):
            if control is None:
                continue
            text, target, tip = control
            button = AppButton(frame, text, lambda target=target: self._on_move(widgets["card"], target), variant="ghost",
                               width=34, height=30, font=font(theme.SIZE_HEADING, "bold"),
                               style=dict(text_color=style.text, border_color=style.text))
            button.grid(row=0, column=column, padx=6, pady=6, sticky="ns")
            Tooltip(button, f"{tip}: {card.name}")
            widgets["left" if column == 0 else "right"] = button
            for key, direction in (("Up", -1), ("Down", 1)):
                focus_target(button).bind(f"<{key}>", lambda event, direction=direction,
                                          side="left" if column == 0 else "right":
                                          self._focus_card(widgets["card"], direction, side), add="+")
        text = ctk.CTkFrame(frame, fg_color="transparent")
        text.grid(row=0, column=1, sticky="ew", padx=(0 if left else 10, 0 if right else 10), pady=6)
        text.columnconfigure(0, weight=1)
        name = ctk.CTkLabel(text, text=card.name, font=font(theme.SIZE_BODY, "bold"), text_color=style.text, anchor="w",
                          justify="left", wraplength=wrap)
        name.grid(row=0, column=0, sticky="ew")
        note = f" · {card.status_note}" if card.status_note else ""
        detail = ctk.CTkLabel(text, text=card.detail + note, font=font(theme.SIZE_CAPTION), text_color=style.text, anchor="w",
                            justify="left", wraplength=wrap)
        detail.grid(row=1, column=0, sticky="ew")
        widgets["labels"] = [name, detail]
        return widgets

    def set_busy(self, busy: bool) -> None:
        """Disable the arrows while a move (or the page) is saving, so a double click cannot move twice."""
        self._busy = busy
        for widgets in self.card_widgets.values():
            self._card_busy(widgets)

    def _card_busy(self, widgets):
        state = "normal" if not self._busy and widgets["card"].can_move else "disabled"
        for side in ("left", "right"):
            if side in widgets and widgets[side].cget("state") != state:
                widgets[side].configure(state=state)
