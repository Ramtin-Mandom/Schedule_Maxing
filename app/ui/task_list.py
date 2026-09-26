"""
app/ui/task_list.py

The keyboard-operable list of saved tasks and fixed blocks shown by every
schedule page (moved out of app/app.py so the Day page can reuse it). Rows
are keyed by UUID; arrow keys select, Enter edits, Delete removes, and the
Menu key, Shift+F10 or a right-click open the row's actions.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from datetime import date
from tkinter import ttk

import customtkinter as ctk

from app.ui import theme
from app.ui.components import AppButton, ContextMenu, MenuItem, font
from app.ui.schedule_page_controller import RowRef, TaskRow


def _table_frame(parent) -> ctk.CTkFrame:
    frame = ctk.CTkFrame(parent, fg_color=theme.CARD_BG, corner_radius=14, border_color=theme.CARD_BORDER,
                         border_width=1)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame


class AddedTasksPanel(ctk.CTkFrame):
    """
    The saved tasks/fixed blocks on this page's dates, keyed by UUID. It is
    the keyboard way to act on tasks (the canvas is only a picture): arrow
    keys select, Enter edits, Delete removes, and the Menu key, Shift+F10 or
    a right-click open the row's actions.
    """

    def __init__(
        self,
        parent: tk.Widget,
        on_remove_task: Callable[[], None],
        on_edit_task: Callable[[], None],
        on_use_as_dependencies: Callable[[], None] | None = None,
        on_open_date: Callable[[date], None] | None = None,
    ) -> None:
        super().__init__(parent, fg_color="transparent")
        self.item_refs: dict[str, RowRef] = {}
        self._rows: list[TaskRow] = []
        self._on_open_date = on_open_date
        self._on_edit, self._on_remove, self._on_dependencies = on_edit_task, on_remove_task, on_use_as_dependencies
        self.columnconfigure((0, 1), weight=1)
        self.rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self, text="Saved tasks for these dates. Enter edits, Delete removes, Ctrl-click picks dependencies.",
            text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=290, font=font(theme.SIZE_SMALL),
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 10))

        table_frame = _table_frame(self)
        table_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=8)
        self.tree = ttk.Treeview(table_frame, columns=("day", "name", "type", "time"), show="headings",
                                 selectmode="extended", height=6)
        self.tree.heading("day", text="Date")
        self.tree.heading("name", text="Task")
        self.tree.heading("type", text="Type")
        self.tree.heading("time", text="Time / Preference")
        self.tree.column("day", width=74, minwidth=66, anchor="center", stretch=False)
        self.tree.column("name", width=130, minwidth=100, anchor="w", stretch=True)
        self.tree.column("type", width=66, minwidth=60, anchor="center", stretch=False)
        self.tree.column("time", width=190, minwidth=160, anchor="center", stretch=False)
        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 0))
        y_scroll.grid(row=0, column=1, sticky="ns", pady=(10, 0))
        x_scroll.grid(row=1, column=0, sticky="ew", padx=(10, 0), pady=(0, 10))

        self.tree.bind("<Return>", lambda _e: (self._on_edit(), "break")[1], add="+")
        self.tree.bind("<KP_Enter>", lambda _e: (self._on_edit(), "break")[1], add="+")
        self.tree.bind("<Delete>", lambda _e: (self._on_remove(), "break")[1], add="+")
        self.context_menu = ContextMenu(self)
        self.context_menu.attach(self.tree, self._menu_items)

        self.edit_button = AppButton(self, "Edit Selected", on_edit_task, height=40)
        self.edit_button.grid(row=2, column=0, sticky="ew", padx=(8, 4), pady=(12, 6))
        self.remove_button = AppButton(self, "Remove Selected", on_remove_task, variant="danger", height=40)
        self.remove_button.grid(row=2, column=1, sticky="ew", padx=(4, 8), pady=(12, 6))
        ctk.CTkLabel(
            self, text="Changes are saved immediately; removing a task also removes its saved schedule entries.",
            text_color=theme.TEXT_MUTED, font=font(theme.SIZE_SMALL), anchor="w", wraplength=290, justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))

    def _menu_items(self, event) -> list[MenuItem]:
        if event is not None:  # a right-click acts on the row under the pointer
            row = self.tree.identify_row(event.y)
            if row and row not in self.tree.selection():
                self.tree.selection_set(row)
                self.tree.focus(row)
        selected = self.selected_refs()
        single = len(selected) == 1
        items = [
            MenuItem("Edit...", self._on_edit, enabled=single),
            MenuItem("Remove...", self._on_remove, enabled=single, danger=True),
        ]
        if self._on_dependencies is not None:
            items.append(MenuItem("Use as dependencies", self._on_dependencies, enabled=bool(selected)))
        if self._on_open_date is not None:
            day = self.selected_date()
            items.append(MenuItem("Open date in Day Schedule",
                                  (lambda: self._on_open_date(day)) if day is not None else (lambda: None),
                                  enabled=day is not None))
        return items

    def selected_date(self) -> date | None:
        """The date of the one selected row (None: no single dated row is selected)."""
        selected = self.selected_refs()
        if len(selected) != 1:
            return None
        return next((row.date for row in self._rows if row.ref == selected[0]), None)

    def clear(self) -> None:
        self.item_refs.clear()
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

    def refresh(self, rows: list[TaskRow]) -> None:
        selected = set(self.tree.selection())
        focused = self.tree.focus()
        self._rows = list(rows)
        self.clear()
        for row in rows:
            item_id = f"{row.ref.kind}:{row.ref.id}"
            self.item_refs[item_id] = row.ref
            self.tree.insert("", tk.END, iid=item_id, values=(row.day_label, row.name, row.type_label, row.time_text),
                             tags=("fixed" if row.ref.kind == "block" else "flexible",))
        still_there = [item_id for item_id in selected if item_id in self.item_refs]
        if still_there:
            self.tree.selection_set(still_there)
        if focused in self.item_refs:
            self.tree.focus(focused)
        self.retag()

    def retag(self) -> None:
        """Row colors for the current appearance (the Type column says fixed/flexible in words too)."""
        self.tree.tag_configure("fixed", foreground=theme.resolve(theme.TEXT_MUTED))
        self.tree.tag_configure("flexible", foreground=theme.resolve(theme.TEXT_PRIMARY))

    def selected_refs(self) -> list[RowRef]:
        return [self.item_refs[item_id] for item_id in self.tree.selection() if item_id in self.item_refs]
