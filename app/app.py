"""
Modern CustomTkinter desktop UI for the schedule optimizer.

Run from the project root with:

    python -m app.app

Data flow (Milestone 2): every page is backed by the application's SQLite
database. A widget callback calls a Tk-free presenter
(app/ui/schedule_page_controller.py), which goes through PlanningController
-> PlanningService -> repository -> SQLite; the page is then redrawn from a
fresh read of what was committed. There is no widget-owned task collection:
tasks, fixed blocks, and generated placements are loaded from SQLite at
startup and after every change.

- Startup opens the one shared database connection (app/ui/app_services.py).
  If it cannot be opened, the app shows the error instead of a scheduler
  whose edits would never be saved.
- Tasks and fixed blocks are created, edited, and deleted by UUID; the
  dependency picker and row selection use UUIDs, so duplicate names work.
- Each page shows real calendar dates from an explicit, editable start date,
  in the configured timezone (config.settings.DEFAULT_TIMEZONE).
- "Make Schedule" allocates the page's dates and runs the canonical day
  scheduler once per date (app.planning.service.generate_selected_day), then
  saves the whole range in one transaction. Greedy Optimizer v1
  (app.optimizer.optimize_day_schedule) remains the legacy/CLI baseline and
  is unchanged.
- The Execute tab tracks saved placements by task/placement id.
- Closing waits for background work, then closes the database.
"""

from __future__ import annotations

import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

try:
    import customtkinter as ctk
except ImportError as error:  # pragma: no cover - runtime dependency message
    raise ImportError(
        "This redesigned UI uses CustomTkinter. Install it with: "
        "pip install customtkinter"
    ) from error

from app.planning.csv_import import ImportMode
from app.ui.app_services import AppServices, describe_startup_failure, open_app_services
from app.ui.background import ControllerResult, run_in_background
from app.ui.duration_suggestion import DurationSuggestionWidget, SuggestionContext
from app.ui.execution_controller import ExecutionController
from app.ui.execution_panel import ExecutionPanel
from app.ui.productivity_controller import ProductivityController
from app.ui.productivity_page import ProductivityPage
from app.ui.schedule_page_controller import (
    CATEGORY_OPTIONS,
    FIXED_OPTIONS,
    CanvasItem,
    FormState,
    PageSnapshot,
    ResetScope,
    RowRef,
    SchedulePageController,
    ScheduleRun,
    TaskRow,
    UnscheduledRow,
    default_anchor,
    format_window,
)
from config import settings

# -----------------------------------------------------------------------------
# Constants / Theme
# -----------------------------------------------------------------------------

CATEGORY_COLORS = {
    "study": "#7CC8FF",
    "sleep": "#C4A7FF",
    "food": "#FFD18A",
    "exercise": "#9BE7B1",
    "work": "#91A7FF",
    "event": "#FF9DE2",
    "entertainment": "#FFF08A",
    "errand": "#FF9E9E",
    "other": "#CBD5E1",
    "fixed": "#94A3B8",
}

CATEGORY_TEXT_COLORS = {
    "study": "#0B3B56",
    "sleep": "#37205A",
    "food": "#5C3600",
    "exercise": "#0F3D22",
    "work": "#15275E",
    "event": "#5B144A",
    "entertainment": "#4A3E00",
    "errand": "#5B1717",
    "other": "#273449",
    "fixed": "#172033",
}

APP_BG = "#EEF2F7"
CARD_BG = "#FFFFFF"
CARD_BORDER = "#E2E8F0"
TEXT_PRIMARY = "#0F172A"
TEXT_MUTED = "#64748B"
ACCENT = "#2563EB"
ACCENT_HOVER = "#1D4ED8"
DANGER = "#DC2626"
DANGER_HOVER = "#B91C1C"
SUCCESS = "#16A34A"
WARNING = "#F59E0B"
CANVAS_BG = "#F8FAFC"
GRID_LINE = "#E5E7EB"
GRID_LINE_STRONG = "#CBD5E1"

DAY_STRIP_WIDTH = 170
DAY_HEADER_HEIGHT = 44
PIXELS_PER_HOUR = 56
DAY_HEIGHT = 24 * PIXELS_PER_HOUR
MINUTES_PER_DAY = 24 * 60

PAGE_DAYS = {"day": 1, "week": 7, "month": 30}


# -----------------------------------------------------------------------------
# Shared Visual Components
# -----------------------------------------------------------------------------

class Card(ctk.CTkFrame):
    """Soft rounded container used throughout the app."""

    def __init__(self, parent: tk.Widget, **kwargs: object) -> None:
        super().__init__(
            parent,
            fg_color=CARD_BG,
            border_color=CARD_BORDER,
            border_width=1,
            corner_radius=18,
            **kwargs,
        )


class SectionTitle(ctk.CTkFrame):
    """Card header with title and optional subtitle."""

    def __init__(self, parent: tk.Widget, title: str, subtitle: str = "") -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text=title,
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color=TEXT_PRIMARY,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        if subtitle:
            ctk.CTkLabel(
                self,
                text=subtitle,
                font=ctk.CTkFont(size=12),
                text_color=TEXT_MUTED,
                anchor="w",
            ).grid(row=1, column=0, sticky="ew", pady=(2, 0))


class StatPill(ctk.CTkFrame):
    """Small rounded statistic badge."""

    def __init__(self, parent: tk.Widget, label: str, value: str, color: str) -> None:
        super().__init__(parent, fg_color=color, corner_radius=14)
        self.value_label = ctk.CTkLabel(
            self,
            text=value,
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color="white",
        )
        self.value_label.pack(anchor="center", pady=(8, 0), padx=12)
        ctk.CTkLabel(
            self,
            text=label,
            font=ctk.CTkFont(size=11),
            text_color="#EAF2FF",
        ).pack(anchor="center", pady=(0, 8), padx=12)

    def set_value(self, value: str) -> None:
        self.value_label.configure(text=value)


# -----------------------------------------------------------------------------
# Task Input Form
# -----------------------------------------------------------------------------

class TaskForm(Card):
    """
    Left-side task entry form, used both to add and (in edit mode) to change
    a task or fixed block. It only collects raw field values plus the chosen
    dependency task ids; validation and saving happen in the presenter.
    """

    def __init__(
        self,
        parent: tk.Widget,
        mode_name: str,
        number_of_days: int,
        on_add_task: Callable[[dict[str, str]], None],
        on_cancel_edit: Callable[[], None],
        on_pick_dependencies: Callable[[], None],
        productivity_controller: ProductivityController | None = None,
    ) -> None:
        super().__init__(parent)

        self.mode_name = mode_name
        self.number_of_days = number_of_days
        self.on_add_task = on_add_task
        self.on_cancel_edit = on_cancel_edit
        self.on_pick_dependencies = on_pick_dependencies
        self.productivity_controller = productivity_controller

        self.name_var = tk.StringVar()
        self.day_var = tk.StringVar(value="1")
        self.category_var = tk.StringVar(value=CATEGORY_OPTIONS[0])
        self.tag_var = tk.StringVar()
        self.fixed_var = tk.StringVar(value=FIXED_OPTIONS[0])
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.duration_var = tk.StringVar()
        self.priority_var = tk.StringVar()
        self.dependencies_text = tk.StringVar(value="None")
        self.dependency_ids: list = []
        self.editing = False

        self._build_form()
        self._sync_fixed_fields()

    def _build_form(self) -> None:
        self.columnconfigure(0, weight=1)

        self.title = SectionTitle(
            self,
            "Task Input",
            "Add fixed blocks or flexible tasks with preferred windows.",
        )
        self.title.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 10))

        form_body = ctk.CTkFrame(self, fg_color="transparent")
        form_body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        form_body.columnconfigure(0, weight=1)

        row = 0
        self._add_entry(form_body, row, "Task name", self.name_var, "e.g., Study Math")
        row += 1

        if self.mode_name != "day":
            self._add_entry(
                form_body, row, f"Day (1-{self.number_of_days}, 1 = start date)", self.day_var, "1"
            )
            row += 1

        self._add_option_menu(form_body, row, "Category", self.category_var, CATEGORY_OPTIONS)
        row += 1

        self._add_entry(form_body, row, "Tag", self.tag_var, "e.g., math, gym, exam")
        row += 1

        self.fixed_option = self._add_option_menu(
            form_body,
            row,
            "Fixed task?",
            self.fixed_var,
            FIXED_OPTIONS,
            command=lambda _choice: self._sync_fixed_fields(),
        )
        row += 1

        time_grid = ctk.CTkFrame(form_body, fg_color="transparent")
        time_grid.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        time_grid.columnconfigure((0, 1), weight=1)
        self._add_compact_entry(time_grid, 0, 0, "Start min", self.start_var, "480")
        self._add_compact_entry(time_grid, 0, 1, "End min", self.end_var, "600")
        row += 1

        duration_priority_grid = ctk.CTkFrame(form_body, fg_color="transparent")
        duration_priority_grid.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        duration_priority_grid.columnconfigure((0, 1), weight=1)
        self.duration_entry = self._add_compact_entry(
            duration_priority_grid,
            0,
            0,
            "Duration",
            self.duration_var,
            "60",
        )
        self.priority_entry = self._add_compact_entry(
            duration_priority_grid,
            0,
            1,
            "Priority",
            self.priority_var,
            "1-10",
        )
        row += 1

        if self.productivity_controller is not None:
            self.duration_suggestion = DurationSuggestionWidget(
                form_body,
                self.productivity_controller,
                get_context=self._duration_suggestion_context,
                apply_duration=self._apply_suggested_duration,
            )
            self.duration_suggestion.grid(row=row, column=0, sticky="ew", pady=(0, 6))
            row += 1
        else:
            self.duration_suggestion = None

        dependencies = ctk.CTkFrame(form_body, fg_color="transparent")
        dependencies.grid(row=row, column=0, sticky="ew", pady=(3, 5))
        dependencies.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            dependencies,
            text="Dependencies (pick rows in Added Tasks)",
            text_color=TEXT_MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ctk.CTkLabel(
            dependencies,
            textvariable=self.dependencies_text,
            text_color=TEXT_PRIMARY,
            anchor="w",
            justify="left",
            wraplength=260,
        ).grid(row=1, column=0, columnspan=2, sticky="ew")
        self.pick_dependencies_button = ctk.CTkButton(
            dependencies, text="Use selected rows", height=30, corner_radius=10,
            fg_color="#334155", hover_color="#1E293B", command=self.on_pick_dependencies,
        )
        self.pick_dependencies_button.grid(row=2, column=0, sticky="ew", padx=(0, 4), pady=(4, 0))
        self.clear_dependencies_button = ctk.CTkButton(
            dependencies, text="Clear", height=30, corner_radius=10,
            fg_color="#E2E8F0", hover_color="#CBD5E1", text_color=TEXT_PRIMARY,
            command=lambda: self.set_dependencies([], []),
        )
        self.clear_dependencies_button.grid(row=2, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))
        row += 1

        self.submit_button = ctk.CTkButton(
            self,
            text="+ Add Task",
            height=44,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._submit,
        )
        self.submit_button.grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 8))

        self.cancel_edit_button = ctk.CTkButton(
            self,
            text="Cancel Edit",
            height=34,
            corner_radius=14,
            fg_color="#E2E8F0",
            hover_color="#CBD5E1",
            text_color=TEXT_PRIMARY,
            command=self.on_cancel_edit,
        )
        self._bottom_spacer = ctk.CTkFrame(self, fg_color="transparent", height=10)
        self._bottom_spacer.grid(row=3, column=0, pady=(0, 10))

    def _add_label(self, parent: tk.Widget, row: int, text: str) -> None:
        ctk.CTkLabel(
            parent,
            text=text,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=row, column=0, sticky="ew", pady=(8, 4))

    def _add_entry(
        self,
        parent: tk.Widget,
        row: int,
        label: str,
        variable: tk.StringVar,
        placeholder: str,
    ) -> ctk.CTkEntry:
        wrapper = ctk.CTkFrame(parent, fg_color="transparent")
        wrapper.grid(row=row, column=0, sticky="ew", pady=(3, 5))
        wrapper.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            wrapper,
            text=label,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 4))

        entry = ctk.CTkEntry(
            wrapper,
            textvariable=variable,
            placeholder_text=placeholder,
            height=38,
            corner_radius=12,
            border_color=CARD_BORDER,
            fg_color="#F8FAFC",
            text_color=TEXT_PRIMARY,
        )
        entry.grid(row=1, column=0, sticky="ew")
        return entry

    def _add_compact_entry(
        self,
        parent: tk.Widget,
        row: int,
        column: int,
        label: str,
        variable: tk.StringVar,
        placeholder: str,
    ) -> ctk.CTkEntry:
        wrapper = ctk.CTkFrame(parent, fg_color="transparent")
        wrapper.grid(row=row, column=column, sticky="ew", padx=(0, 6) if column == 0 else (6, 0))
        wrapper.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            wrapper,
            text=label,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 4))

        entry = ctk.CTkEntry(
            wrapper,
            textvariable=variable,
            placeholder_text=placeholder,
            height=38,
            corner_radius=12,
            border_color=CARD_BORDER,
            fg_color="#F8FAFC",
            text_color=TEXT_PRIMARY,
        )
        entry.grid(row=1, column=0, sticky="ew")
        return entry

    def _add_option_menu(
        self,
        parent: tk.Widget,
        row: int,
        label: str,
        variable: tk.StringVar,
        values: list[str],
        command: Callable[[str], None] | None = None,
    ) -> ctk.CTkOptionMenu:
        wrapper = ctk.CTkFrame(parent, fg_color="transparent")
        wrapper.grid(row=row, column=0, sticky="ew", pady=(3, 5))
        wrapper.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            wrapper,
            text=label,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 4))

        option = ctk.CTkOptionMenu(
            wrapper,
            variable=variable,
            values=values,
            command=command,
            height=38,
            corner_radius=12,
            fg_color="#F8FAFC",
            button_color="#E2E8F0",
            button_hover_color="#CBD5E1",
            text_color=TEXT_PRIMARY,
            dropdown_fg_color="#FFFFFF",
            dropdown_hover_color="#EFF6FF",
            dropdown_text_color=TEXT_PRIMARY,
        )
        option.grid(row=1, column=0, sticky="ew")
        return option

    def _sync_fixed_fields(self) -> None:
        is_fixed = self.fixed_var.get() == "True"
        state = "disabled" if is_fixed else "normal"

        self.duration_entry.configure(state=state)
        self.priority_entry.configure(state=state)
        self.pick_dependencies_button.configure(state=state)
        self.clear_dependencies_button.configure(state=state)

        if is_fixed:
            self.duration_var.set("")
            self.priority_var.set("")
            self.set_dependencies([], [])

    def set_dependencies(self, dependency_ids: list, labels: list[str]) -> None:
        self.dependency_ids = list(dependency_ids)
        self.dependencies_text.set("; ".join(labels) if labels else "None")

    def values(self) -> dict[str, str]:
        return {
            "name": self.name_var.get().strip(),
            "day": self.day_var.get().strip(),
            "category": self.category_var.get().strip(),
            "tag": self.tag_var.get().strip(),
            "fixed": self.fixed_var.get().strip(),
            "start_time": self.start_var.get().strip(),
            "end_time": self.end_var.get().strip(),
            "duration": self.duration_var.get().strip(),
            "priority": self.priority_var.get().strip(),
        }

    def _submit(self) -> None:
        self.on_add_task(self.values())

    def enter_edit_mode(self, state: FormState, dependency_labels: list[str]) -> None:
        values = state.values
        self.editing = True
        self.fixed_var.set(values["fixed"])
        self._sync_fixed_fields()
        self.name_var.set(values["name"])
        self.day_var.set(values["day"])
        self.category_var.set(values["category"] if values["category"] in CATEGORY_OPTIONS else CATEGORY_OPTIONS[-1])
        self.tag_var.set(values["tag"])
        self.start_var.set(values["start_time"])
        self.end_var.set(values["end_time"])
        self.duration_var.set(values["duration"])
        self.priority_var.set(values["priority"])
        self.set_dependencies(state.dependency_ids, dependency_labels)
        self.fixed_option.configure(state="disabled")  # kind cannot change while editing
        self.submit_button.configure(text="Save Changes")
        self.cancel_edit_button.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 18))

    def exit_edit_mode(self) -> None:
        self.editing = False
        self.fixed_option.configure(state="normal")
        self.submit_button.configure(text="+ Add Task")
        self.cancel_edit_button.grid_remove()

    def clear_fields(self) -> None:
        self.name_var.set("")
        self.day_var.set("1")
        self.category_var.set(CATEGORY_OPTIONS[0])
        self.tag_var.set("")
        self.fixed_var.set(FIXED_OPTIONS[0])
        self.start_var.set("")
        self.end_var.set("")
        self.duration_var.set("")
        self.priority_var.set("")
        self.set_dependencies([], [])
        self._sync_fixed_fields()
        if self.duration_suggestion is not None:
            self.duration_suggestion.reset()

    def _duration_suggestion_context(self) -> SuggestionContext | None:
        """
        Read the form's current category/start-time/duration fields for a
        duration suggestion request. Returns None (and the widget shows a
        hint) if the fields aren't filled in well enough yet -- this never
        triggers automatically, only when the user clicks "Suggest duration".
        """
        category = self.category_var.get().strip()
        if not category:
            return None

        try:
            planned_start = int(self.start_var.get().strip())
        except ValueError:
            return None

        try:
            original_estimate_minutes = float(self.duration_var.get().strip())
        except ValueError:
            original_estimate_minutes = 0.0

        return SuggestionContext(
            category=category,
            planned_start=planned_start,
            original_estimate_minutes=original_estimate_minutes,
        )

    def _apply_suggested_duration(self, minutes: int) -> None:
        # Explicit, user-initiated fill (the widget only calls this from its own
        # "Use suggestion" button) -- the duration field remains a normal, editable entry.
        self.duration_var.set(str(minutes))


# -----------------------------------------------------------------------------
# Task Manager Panels
# -----------------------------------------------------------------------------

class AddedTasksPanel(ctk.CTkFrame):
    """Right-side table of the saved tasks/fixed blocks on this page's dates, keyed by UUID."""

    def __init__(
        self,
        parent: tk.Widget,
        on_remove_task: Callable[[], None],
        on_edit_task: Callable[[], None],
    ) -> None:
        super().__init__(parent, fg_color="transparent")
        self.item_refs: dict[str, RowRef] = {}
        self.columnconfigure((0, 1), weight=1)
        self.rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Saved tasks for these dates. Ctrl-click rows to pick dependencies.",
            text_color=TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=290,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 10))

        table_frame = ctk.CTkFrame(self, fg_color="#FFFFFF", corner_radius=14, border_color=CARD_BORDER, border_width=1)
        table_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=8)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_frame,
            columns=("day", "name", "type", "time"),
            show="headings",
            selectmode="extended",
            height=16,
        )
        self.tree.heading("day", text="Date")
        self.tree.heading("name", text="Task")
        self.tree.heading("type", text="Type")
        self.tree.heading("time", text="Time / Preference")
        self.tree.column("day", width=74, minwidth=66, anchor="center", stretch=False)
        self.tree.column("name", width=130, minwidth=100, anchor="w", stretch=True)
        self.tree.column("type", width=66, minwidth=60, anchor="center", stretch=False)
        self.tree.column("time", width=145, minwidth=125, anchor="center", stretch=False)

        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 0))
        y_scroll.grid(row=0, column=1, sticky="ns", pady=(10, 0))
        x_scroll.grid(row=1, column=0, sticky="ew", padx=(10, 0), pady=(0, 10))

        ctk.CTkButton(
            self,
            text="Edit Selected",
            height=40,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=on_edit_task,
        ).grid(row=2, column=0, sticky="ew", padx=(8, 4), pady=(12, 6))

        ctk.CTkButton(
            self,
            text="Remove Selected",
            height=40,
            corner_radius=14,
            fg_color=DANGER,
            hover_color=DANGER_HOVER,
            command=on_remove_task,
        ).grid(row=2, column=1, sticky="ew", padx=(4, 8), pady=(12, 6))

        ctk.CTkLabel(
            self,
            text="Changes are saved immediately; removing a task also removes its saved schedule entries.",
            text_color=TEXT_MUTED,
            font=ctk.CTkFont(size=12),
            anchor="w",
            wraplength=290,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))

    def clear(self) -> None:
        self.item_refs.clear()
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

    def refresh(self, rows: list[TaskRow]) -> None:
        selected = set(self.tree.selection())
        self.clear()

        for row in rows:
            item_id = f"{row.ref.kind}:{row.ref.id}"
            self.item_refs[item_id] = row.ref
            self.tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(row.day_label, row.name, row.type_label, row.time_text),
                tags=("fixed" if row.ref.kind == "block" else "flexible",),
            )

        still_there = [item_id for item_id in selected if item_id in self.item_refs]
        if still_there:
            self.tree.selection_set(still_there)

        self.tree.tag_configure("fixed", foreground="#334155")
        self.tree.tag_configure("flexible", foreground="#1E3A8A")

    def selected_refs(self) -> list[RowRef]:
        return [self.item_refs[item_id] for item_id in self.tree.selection() if item_id in self.item_refs]


class UnscheduledPanel(ctk.CTkFrame):
    """Right-side table of what the last Make Schedule run could not place."""

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.empty_text = ctk.CTkLabel(
            self,
            text="No unscheduled tasks yet. Run Make Schedule to see results.",
            text_color=TEXT_MUTED,
            anchor="w",
        )
        self.empty_text.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 10))

        table_frame = ctk.CTkFrame(self, fg_color="#FFFFFF", corner_radius=14, border_color=CARD_BORDER, border_width=1)
        table_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_frame,
            columns=("day", "task", "reason"),
            show="headings",
            height=16,
        )
        self.tree.heading("day", text="Date")
        self.tree.heading("task", text="Task")
        self.tree.heading("reason", text="Reason")
        self.tree.column("day", width=82, minwidth=70, anchor="center", stretch=False)
        self.tree.column("task", width=150, minwidth=110, anchor="w", stretch=True)
        self.tree.column("reason", width=270, minwidth=200, anchor="w", stretch=True)

        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 0))
        y_scroll.grid(row=0, column=1, sticky="ns", pady=(10, 0))
        x_scroll.grid(row=1, column=0, sticky="ew", padx=(10, 0), pady=(0, 10))

    def clear(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self.empty_text.configure(text="No unscheduled tasks yet. Run Make Schedule to see results.")

    def show(self, rows: list[UnscheduledRow]) -> None:
        self.clear()
        for row in rows:
            self.tree.insert("", tk.END, values=(row.day_label, row.name, row.reason))
        if rows:
            self.empty_text.configure(text="Some tasks could not be placed. Review the reasons below.")
        else:
            self.empty_text.configure(text="Every task was placed by the last Make Schedule run.")


# -----------------------------------------------------------------------------
# Schedule Canvas
# -----------------------------------------------------------------------------

class ScheduleCanvas(Card):
    """Scrollable visual schedule strips, one per real date of the page."""

    def __init__(self, parent: tk.Widget, number_of_days: int) -> None:
        super().__init__(parent)
        self.number_of_days = number_of_days
        self.dates: list[date] = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 6))
        header.columnconfigure(0, weight=1)

        SectionTitle(header, "Schedule Strip", "Saved schedule, or preferred-window previews before scheduling.").grid(
            row=0, column=0, sticky="ew"
        )

        legend = ctk.CTkFrame(header, fg_color="transparent")
        legend.grid(row=0, column=1, sticky="e")
        self._legend_item(legend, "Fixed", CATEGORY_COLORS["fixed"], 0)
        self._legend_item(legend, "Preview", "#DBEAFE", 1)
        self._legend_item(legend, "Scheduled", ACCENT, 2)
        self._legend_item(legend, "Out of date", WARNING, 3)

        canvas_shell = ctk.CTkFrame(self, fg_color=CANVAS_BG, corner_radius=16, border_color=CARD_BORDER, border_width=1)
        canvas_shell.grid(row=1, column=0, sticky="nsew", padx=18, pady=(6, 18))
        canvas_shell.rowconfigure(0, weight=1)
        canvas_shell.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            canvas_shell,
            background=CANVAS_BG,
            highlightthickness=0,
            bd=0,
            relief="flat",
        )
        self.v_scroll = ttk.Scrollbar(canvas_shell, orient="vertical", command=self.canvas.yview)
        self.h_scroll = ttk.Scrollbar(canvas_shell, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.v_scroll.set, xscrollcommand=self.h_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        self.v_scroll.grid(row=0, column=1, sticky="ns", pady=(8, 0))
        self.h_scroll.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))

        self.draw_empty()

    def _legend_item(self, parent: tk.Widget, text: str, color: str, column: int) -> None:
        item = ctk.CTkFrame(parent, fg_color="transparent")
        item.grid(row=0, column=column, padx=(8, 0))
        swatch = ctk.CTkFrame(item, width=12, height=12, fg_color=color, corner_radius=4)
        swatch.pack(side="left", padx=(0, 5))
        ctk.CTkLabel(item, text=text, font=ctk.CTkFont(size=11), text_color=TEXT_MUTED).pack(side="left")

    def draw_empty(self) -> None:
        self.canvas.delete("all")
        total_width = self.number_of_days * DAY_STRIP_WIDTH + 92
        total_height = DAY_HEADER_HEIGHT + DAY_HEIGHT + 28
        self.canvas.configure(scrollregion=(0, 0, total_width, total_height))

        for day in range(1, self.number_of_days + 1):
            self._draw_day_strip(day)

    def draw(self, snapshot: PageSnapshot) -> None:
        """Draw fixed blocks, saved placements (current or out of date), and previews."""
        self.dates = list(snapshot.dates)
        self.number_of_days = len(self.dates)
        self.draw_empty()
        order = {"fixed": 0, "preview": 1, "stale": 2, "optimized": 3}
        for item in sorted(snapshot.canvas_items, key=lambda entry: order[entry.mode]):
            self._draw_task_box(item)

    def _day_x(self, day: int) -> int:
        return 78 + (day - 1) * DAY_STRIP_WIDTH

    def _minute_y(self, minute: int) -> float:
        return DAY_HEADER_HEIGHT + (minute / 60) * PIXELS_PER_HOUR

    def _draw_day_strip(self, day: int) -> None:
        x0 = self._day_x(day)
        x1 = x0 + DAY_STRIP_WIDTH - 20
        y0 = DAY_HEADER_HEIGHT
        y1 = DAY_HEADER_HEIGHT + DAY_HEIGHT

        header = f"{self.dates[day - 1]:%a %b} {self.dates[day - 1].day}" if day <= len(self.dates) else f"Day {day}"
        self.canvas.create_text(
            (x0 + x1) / 2,
            20,
            text=header,
            font=("Segoe UI", 11, "bold"),
            fill=TEXT_PRIMARY,
        )

        self._round_rect(x0, y0, x1, y1, radius=18, fill="#FFFFFF", outline=CARD_BORDER, width=1)

        for hour in range(25):
            y = DAY_HEADER_HEIGHT + hour * PIXELS_PER_HOUR
            line_fill = GRID_LINE_STRONG if hour % 6 == 0 else GRID_LINE
            self.canvas.create_line(x0 + 8, y, x1 - 8, y, fill=line_fill)

            if day == 1:
                label = "24:00" if hour == 24 else f"{hour:02d}:00"
                self.canvas.create_text(
                    60,
                    y,
                    text=label,
                    anchor="e",
                    font=("Segoe UI", 8),
                    fill=TEXT_MUTED,
                )

    def _draw_task_box(self, item: CanvasItem) -> None:
        x0 = self._day_x(item.day_index) + 10
        x1 = x0 + DAY_STRIP_WIDTH - 40
        y0 = self._minute_y(item.start_minute) + 3
        y1 = self._minute_y(item.end_minute) - 3

        if y1 - y0 < 18:
            y1 = y0 + 18

        category = item.category if item.category in CATEGORY_COLORS else "other"
        fill = CATEGORY_COLORS.get(category, CATEGORY_COLORS["other"])
        text_fill = CATEGORY_TEXT_COLORS.get(category, TEXT_PRIMARY)
        outline = "#FFFFFF"
        dash = None

        if item.mode == "preview":
            outline = fill
            fill = "#EFF6FF"
            dash = (4, 3)
            text_fill = "#1E3A8A"
        elif item.mode == "stale":
            outline = WARNING
            dash = (6, 3)
        elif item.mode == "optimized" and item.score > 0:
            outline = "#1D4ED8"

        # Soft shadow.
        self._round_rect(x0 + 2, y0 + 3, x1 + 2, y1 + 3, radius=12, fill="#D8E0EA", outline="")
        self._round_rect(x0, y0, x1, y1, radius=12, fill=fill, outline=outline, width=2, dash=dash)

        box_height = y1 - y0
        name_font_size = 8 if box_height < 32 else 9
        time_font_size = 7

        self.canvas.create_text(
            (x0 + x1) / 2,
            y0 + box_height * 0.45,
            text=item.name,
            width=DAY_STRIP_WIDTH - 50,
            font=("Segoe UI", name_font_size, "bold"),
            fill=text_fill,
            justify="center",
        )

        if box_height >= 34:
            self.canvas.create_text(
                (x0 + x1) / 2,
                y1 - 9,
                text=format_window(item.start_minute, item.end_minute),
                width=DAY_STRIP_WIDTH - 50,
                font=("Segoe UI", time_font_size),
                fill=text_fill,
                justify="center",
            )

    def _round_rect(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        radius: int = 12,
        **kwargs: object,
    ) -> int:
        points = [
            x0 + radius, y0,
            x1 - radius, y0,
            x1, y0,
            x1, y0 + radius,
            x1, y1 - radius,
            x1, y1,
            x1 - radius, y1,
            x0 + radius, y1,
            x0, y1,
            x0, y1 - radius,
            x0, y0 + radius,
            x0, y0,
        ]
        return int(self.canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs))


# -----------------------------------------------------------------------------
# Reset Dialog
# -----------------------------------------------------------------------------

class ChoiceDialog(ctk.CTkToplevel):
    """A small modal choice (radio buttons + Continue/Cancel); the page then asks for a final confirmation."""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        title: str,
        prompt: str,
        options: list[tuple[str, str]],
        note: str,
        on_choose: Callable[[str], None],
        danger: bool = False,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self._on_choose = on_choose
        self.choice_var = tk.StringVar(value=options[0][0])

        ctk.CTkLabel(self, text=prompt, anchor="w").pack(anchor="w", padx=18, pady=(18, 8))
        for value, label in options:
            ctk.CTkRadioButton(self, text=label, variable=self.choice_var, value=value).pack(anchor="w", padx=18, pady=4)
        ctk.CTkLabel(self, text=note, text_color=TEXT_MUTED, anchor="w", justify="left", wraplength=420).pack(
            anchor="w", padx=18, pady=(8, 4)
        )

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(8, 18))
        ctk.CTkButton(buttons, text="Cancel", fg_color="#E2E8F0", hover_color="#CBD5E1",
                      text_color=TEXT_PRIMARY, command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            buttons, text="Continue...",
            fg_color=DANGER if danger else ACCENT, hover_color=DANGER_HOVER if danger else ACCENT_HOVER,
            command=self._choose,
        ).pack(side="right")

        self.transient(parent.winfo_toplevel())
        self.after(50, self.grab_set)

    def _choose(self) -> None:
        choice = self.choice_var.get()
        self.destroy()
        self._on_choose(choice)


# -----------------------------------------------------------------------------
# Schedule Page
# -----------------------------------------------------------------------------

class SchedulePage(ctk.CTkFrame):
    """
    Reusable page for day, week, and month scheduling. Every callback
    delegates to its SchedulePageController and redraws from the snapshot
    it returns (always a fresh read of committed SQLite state).
    """

    def __init__(
        self,
        parent: tk.Widget,
        mode_name: str,
        page_controller: SchedulePageController,
        execution_controller: ExecutionController | None = None,
        productivity_controller: ProductivityController | None = None,
    ) -> None:
        super().__init__(parent, fg_color=APP_BG)
        self.mode_name = mode_name
        self.page_controller = page_controller
        self.number_of_days = page_controller.number_of_days
        self.execution_controller = execution_controller
        self.productivity_controller = productivity_controller
        self.snapshot: PageSnapshot | None = None
        self._editing: RowRef | None = None
        self._busy = False

        self.columnconfigure(0, weight=0, minsize=360)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=0, minsize=430)
        self.rowconfigure(1, weight=1)

        self._build_header()
        self._build_left_panel()
        self._build_schedule_canvas()
        self._build_right_panel()
        self.reload()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", padx=20, pady=(18, 10))
        header.columnconfigure(0, weight=1)

        title = "Day Schedule" if self.mode_name == "day" else f"{self.mode_name.title()} Schedule"
        ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color=TEXT_PRIMARY,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        range_bar = ctk.CTkFrame(header, fg_color="transparent")
        range_bar.grid(row=1, column=0, sticky="w", pady=(3, 0))
        ctk.CTkLabel(
            range_bar,
            text="Date:" if self.mode_name == "day" else "Start date:",
            font=ctk.CTkFont(size=13),
            text_color=TEXT_MUTED,
        ).pack(side="left")
        self.start_date_var = tk.StringVar(value=self.page_controller.anchor_date.isoformat())
        ctk.CTkEntry(range_bar, textvariable=self.start_date_var, width=110, height=30).pack(side="left", padx=(6, 4))
        ctk.CTkButton(range_bar, text="Go", width=44, height=30, command=self.apply_start_date).pack(side="left")
        self.range_label = ctk.CTkLabel(range_bar, text="", font=ctk.CTkFont(size=13), text_color=TEXT_MUTED)
        self.range_label.pack(side="left", padx=(10, 0))

        self.status_label = ctk.CTkLabel(
            header, text="", font=ctk.CTkFont(size=12), text_color=TEXT_MUTED, anchor="w"
        )
        self.status_label.grid(row=2, column=0, sticky="w", pady=(2, 0))

        stats = ctk.CTkFrame(header, fg_color="transparent")
        stats.grid(row=0, column=1, rowspan=3, sticky="e")
        self.total_pill = StatPill(stats, "Total", "0", ACCENT)
        self.fixed_pill = StatPill(stats, "Fixed", "0", "#475569")
        self.flex_pill = StatPill(stats, "Flexible", "0", SUCCESS)
        self.total_pill.grid(row=0, column=0, padx=(0, 8))
        self.fixed_pill.grid(row=0, column=1, padx=(0, 8))
        self.flex_pill.grid(row=0, column=2)

    def _build_left_panel(self) -> None:
        self.left_panel = ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color="#CBD5E1")
        self.left_panel.grid(row=1, column=0, sticky="nsew", padx=(20, 8), pady=(0, 20))
        self.left_panel.columnconfigure(0, weight=1)

        self.form = TaskForm(
            self.left_panel,
            mode_name=self.mode_name,
            number_of_days=self.number_of_days,
            on_add_task=self.submit_task,
            on_cancel_edit=self.cancel_edit,
            on_pick_dependencies=self.use_selected_as_dependencies,
            productivity_controller=self.productivity_controller,
        )
        self.form.grid(row=0, column=0, sticky="ew", pady=(0, 14))

        controls = Card(self.left_panel)
        controls.grid(row=1, column=0, sticky="ew")
        controls.columnconfigure((0, 1), weight=1)
        SectionTitle(controls, "Controls", "Schedule, import, export, or clear these dates.").grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(18, 12)
        )

        self.make_schedule_button = ctk.CTkButton(
            controls,
            text="Make Schedule",
            height=42,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.make_schedule,
        )
        self.make_schedule_button.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 10))

        self.upload_button = ctk.CTkButton(
            controls,
            text="Upload CSV...",
            height=38,
            corner_radius=14,
            fg_color="#334155",
            hover_color="#1E293B",
            command=self.upload_csv,
        )
        self.upload_button.grid(row=2, column=0, sticky="ew", padx=(18, 6), pady=(0, 10))

        ctk.CTkButton(
            controls,
            text="Export CSV...",
            height=38,
            corner_radius=14,
            fg_color="#334155",
            hover_color="#1E293B",
            command=self.export_csv,
        ).grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 18))

        ctk.CTkButton(
            controls,
            text="Reset...",
            height=38,
            corner_radius=14,
            fg_color="#E2E8F0",
            hover_color="#CBD5E1",
            text_color=TEXT_PRIMARY,
            command=self.reset,
        ).grid(row=2, column=1, sticky="ew", padx=(6, 18), pady=(0, 10))

    def _build_schedule_canvas(self) -> None:
        self.schedule_canvas = ScheduleCanvas(self, self.number_of_days)
        self.schedule_canvas.grid(row=1, column=1, sticky="nsew", padx=8, pady=(0, 20))

    def _build_right_panel(self) -> None:
        right_card = Card(self)
        right_card.grid(row=1, column=2, sticky="nsew", padx=(8, 20), pady=(0, 20))
        right_card.columnconfigure(0, weight=1)
        right_card.rowconfigure(1, weight=1)

        SectionTitle(right_card, "Task Manager", "Review, edit, remove, and inspect optimizer results.").grid(
            row=0, column=0, sticky="ew", padx=18, pady=(18, 10)
        )

        self.right_tabs = ctk.CTkTabview(
            right_card,
            fg_color="#F8FAFC",
            segmented_button_fg_color="#E2E8F0",
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color="#E2E8F0",
            segmented_button_unselected_hover_color="#CBD5E1",
            text_color=TEXT_PRIMARY,
            corner_radius=16,
        )
        self.right_tabs.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))

        added_tab = self.right_tabs.add("Added Tasks")
        unscheduled_tab = self.right_tabs.add("Unscheduled")
        added_tab.columnconfigure(0, weight=1)
        added_tab.rowconfigure(0, weight=1)
        unscheduled_tab.columnconfigure(0, weight=1)
        unscheduled_tab.rowconfigure(0, weight=1)

        self.added_tasks_panel = AddedTasksPanel(
            added_tab, on_remove_task=self.remove_selected_task, on_edit_task=self.edit_selected_task
        )
        self.unscheduled_panel = UnscheduledPanel(unscheduled_tab)
        self.added_tasks_panel.grid(row=0, column=0, sticky="nsew")
        self.unscheduled_panel.grid(row=0, column=0, sticky="nsew")

        self.execution_panel = None
        if self.execution_controller is not None:
            execute_tab = self.right_tabs.add("Execute")
            execute_tab.columnconfigure(0, weight=1)
            execute_tab.rowconfigure(0, weight=1)
            self.execution_panel = ExecutionPanel(execute_tab, self.execution_controller)
            self.execution_panel.grid(row=0, column=0, sticky="nsew")

    # ----------------------------- User actions -----------------------------

    def reload(self) -> None:
        """Re-read this page from SQLite (startup, page switch, after any failure)."""
        result = self.page_controller.load()
        if result.ok:
            self._render(result.value)
        else:
            messagebox.showerror("Could Not Load Saved Data", result.error or "Unknown error.", parent=self)

    def on_show(self) -> None:
        """Called when the page is raised: other pages may have changed shared data."""
        if not self._busy:
            self.reload()

    def apply_start_date(self) -> None:
        if self._refuse_while_busy():
            return
        result = self.page_controller.set_anchor_date(self.start_date_var.get())
        if not result.ok:
            messagebox.showerror("Invalid Date", result.error or "Unknown error.", parent=self)
            self.start_date_var.set(self.page_controller.anchor_date.isoformat())
            return
        self._leave_edit_mode()
        self.unscheduled_panel.clear()
        self._render(result.value)

    def submit_task(self, values: dict[str, str]) -> None:
        """Add (or, in edit mode, save) the form's task/fixed block through the service."""
        if self._refuse_while_busy():
            return
        result = self.page_controller.submit_task_form(
            values, dependency_ids=list(self.form.dependency_ids), editing=self._editing
        )
        self._render_result(result, "Could Not Save Task")
        if result.ok:
            self._leave_edit_mode()
            self.form.clear_fields()

    # Kept for callers of the previous API name.
    add_task = submit_task

    def edit_selected_task(self) -> None:
        if self._refuse_while_busy():
            return
        refs = self.added_tasks_panel.selected_refs()
        if len(refs) != 1:
            messagebox.showinfo("Edit Task", "Select exactly one task or fixed block to edit.", parent=self)
            return
        state = self.page_controller.form_state_for(refs[0])
        if not state.ok:
            messagebox.showerror("Could Not Edit", state.error or "Unknown error.", parent=self)
            self.reload()
            return
        labels = self.page_controller.describe_tasks(state.value.dependency_ids)
        self._editing = refs[0]
        self.form.enter_edit_mode(state.value, labels.value if labels.ok else [])

    def cancel_edit(self) -> None:
        self._leave_edit_mode()
        self.form.clear_fields()

    def use_selected_as_dependencies(self) -> None:
        refs = self.added_tasks_panel.selected_refs()
        task_ids = [ref.id for ref in refs if ref.kind == "task"]
        if len(task_ids) != len(refs):
            messagebox.showinfo("Dependencies", "Fixed blocks cannot be dependencies; they were ignored.", parent=self)
        labels = self.page_controller.describe_tasks(task_ids)
        if not labels.ok:
            messagebox.showerror("Dependencies", labels.error or "Unknown error.", parent=self)
            return
        self.form.set_dependencies(task_ids, labels.value)

    def remove_selected_task(self) -> None:
        if self._refuse_while_busy():
            return
        refs = self.added_tasks_panel.selected_refs()
        if not refs:
            messagebox.showinfo("No Selection", "Select a task to remove first.", parent=self)
            return
        if len(refs) > 1:
            messagebox.showinfo("Remove Task", "Remove one task or fixed block at a time.", parent=self)
            return
        result = self.page_controller.delete(refs[0])
        self._render_result(result, "Could Not Remove Task")
        if result.ok and self._editing == refs[0]:
            self.cancel_edit()

    def upload_csv(self) -> None:
        """Pick a legacy CSV, choose Append or Replace, confirm, then import it in one transaction."""
        if self._refuse_while_busy():
            return
        path = filedialog.askopenfilename(
            title="Upload Schedule CSV", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")], parent=self
        )
        if not path:
            return
        ChoiceDialog(
            self,
            title="Import CSV",
            prompt=f"How should {Path(path).name} be imported?",
            options=[
                (ImportMode.APPEND.value, "Append: add its tasks and fixed blocks"),
                (ImportMode.REPLACE.value, "Replace: clear the dates it covers first, then add it"),
            ],
            note=f"Day 1 of the file is this page's start date ({self.page_controller.anchor_date.isoformat()}).",
            on_choose=lambda mode: self.confirm_import(path, ImportMode(mode)),
        )

    def confirm_import(self, path: str, mode: ImportMode) -> None:
        confirmed = messagebox.askyesno(
            "Confirm Import",
            self.page_controller.import_description(mode) + "\n\nContinue?",
            icon="warning" if mode == ImportMode.REPLACE else "question",
            parent=self,
        )
        if not confirmed:
            return
        result = self.page_controller.import_csv(path, mode)
        if not result.ok:
            if result.value is not None:
                self._render(result.value)
            messagebox.showerror("CSV Import Error", result.error or "Unknown error.", parent=self)
            return
        self._leave_edit_mode()
        self.unscheduled_panel.clear()
        self._render(result.value.snapshot)
        messagebox.showinfo("CSV Imported", result.value.summary, parent=self)

    def export_csv(self) -> None:
        """Export the saved tasks, fixed blocks, and schedule of these dates (read from SQLite)."""
        path = filedialog.asksaveasfilename(
            title="Export Saved Planning Data",
            defaultextension=".csv",
            initialfile=f"planning_{self.page_controller.anchor_date.isoformat()}.csv",
            filetypes=[("CSV files", "*.csv")],
            parent=self,
        )
        if not path:
            return
        result = self.page_controller.export_csv(path)
        if not result.ok:
            messagebox.showerror("Export Error", result.error or "Unknown error.", parent=self)
            return
        exported = result.value
        messagebox.showinfo(
            "Export Complete",
            f"Exported {exported.tasks} task(s), {exported.fixed_blocks} fixed block(s), and "
            f"{exported.placements} scheduled entr(ies) to {exported.path}.",
            parent=self,
        )

    def make_schedule(self) -> None:
        if self._refuse_while_busy():
            return
        self._set_busy(True)
        if not run_in_background(self, self.page_controller.make_schedule, self._on_schedule_done):
            self._set_busy(False)

    def _on_schedule_done(self, result: ControllerResult[ScheduleRun]) -> None:
        self._set_busy(False)
        if not result.ok:
            messagebox.showerror("Scheduling Error", result.error or "Unknown error.", parent=self)
            self.reload()
            return

        run = result.value
        self._render(run.snapshot)
        self.unscheduled_panel.show(run.unscheduled)
        if run.snapshot.total_count == 0:
            messagebox.showinfo("No Tasks", "There are no tasks to schedule on these dates yet.", parent=self)

    def reset(self) -> None:
        if self._refuse_while_busy():
            return
        span = f"{self.page_controller.anchor_date.isoformat()} to {self.page_controller.end_date.isoformat()}"
        ChoiceDialog(
            self,
            title="Reset",
            prompt=f"What should be cleared for {span}?",
            options=[
                (ResetScope.SCHEDULE.value, "Only the saved schedule (generated placements)"),
                (ResetScope.PLANNING_DATA.value, "Schedule, fixed blocks, and tasks planned on these dates"),
            ],
            note="Execution history is never deleted here (see the Productivity page).",
            on_choose=lambda scope: self.confirm_reset(ResetScope(scope)),
            danger=True,
        )

    def confirm_reset(self, scope: ResetScope) -> None:
        confirmed = messagebox.askyesno(
            "Confirm Reset",
            self.page_controller.reset_description(scope) + "\n\nContinue?",
            icon="warning",
            parent=self,
        )
        if not confirmed:
            return
        result = self.page_controller.reset(scope)
        self._render_result(result, "Reset Failed")
        if result.ok:
            self._leave_edit_mode()
            self.form.clear_fields()
            self.unscheduled_panel.clear()

    # ----------------------------- Rendering -----------------------------

    def _render_result(self, result: ControllerResult[PageSnapshot], error_title: str) -> None:
        """Redraw from the (re-read) snapshot; on failure show the error, then the committed state."""
        if result.value is not None:
            self._render(result.value)
        if not result.ok:
            messagebox.showerror(error_title, result.error or "Unknown error.", parent=self)
            if result.value is None:
                self.reload()

    def _render(self, snapshot: PageSnapshot) -> None:
        self.snapshot = snapshot
        self.start_date_var.set(snapshot.start_date.isoformat())
        if snapshot.start_date == snapshot.end_date:
            span = f"{snapshot.start_date:%A}"
        else:
            span = f"to {snapshot.end_date.isoformat()}"
        self.range_label.configure(text=f"{span}   ·   times in {snapshot.timezone}")
        self.status_label.configure(text=snapshot.status_text)
        self.added_tasks_panel.refresh(snapshot.rows)
        self.schedule_canvas.draw(snapshot)
        self.total_pill.set_value(str(snapshot.total_count))
        self.fixed_pill.set_value(str(snapshot.fixed_count))
        self.flex_pill.set_value(str(snapshot.flexible_count))
        if self.execution_panel is not None:
            self.execution_panel.set_scheduled_tasks(snapshot.executables)

    def _leave_edit_mode(self) -> None:
        self._editing = None
        self.form.exit_edit_mode()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.make_schedule_button.configure(
            state="disabled" if busy else "normal", text="Scheduling..." if busy else "Make Schedule"
        )

    def _refuse_while_busy(self) -> bool:
        if self._busy:
            messagebox.showinfo("Please Wait", "Scheduling is still running for this page.", parent=self)
        return self._busy


# -----------------------------------------------------------------------------
# Reward Config Page
# -----------------------------------------------------------------------------

class RewardConfigPage(ctk.CTkFrame):
    """Runtime reward configuration panel."""

    REWARD_FIELDS = [
        "WEIGHT_PRIORITY",
        "WEIGHT_PREFERENCE_TIME",
        "WEIGHT_TAG_RELATION",
        "WEIGHT_SPACING",
        "WEIGHT_NO_BREAK_PENALTY",
        "PREFERENCE_TIME_DISTANCE_SCALE",
        "TAG_RELATION_MAX_GAP",
        "MIN_GOOD_BREAK",
        "MAX_GOOD_BREAK",
        "BACK_TO_BACK_GAP",
        "INITIAL_TEMPERATURE",
        "MIN_TEMPERATURE",
        "COOLING_RATE",
        "MAX_ITERATIONS",
        "NO_IMPROVEMENT_LIMIT",
    ]

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, fg_color=APP_BG)
        self.vars: dict[str, tk.StringVar] = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build()

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 12))
        ctk.CTkLabel(
            header,
            text="Reward Config",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color=TEXT_PRIMARY,
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text=(
                "These values are applied at runtime only and affect the legacy Greedy Optimizer v1 "
                "(CLI baseline). The desktop scheduler reads config/task_preference.yaml instead."
            ),
            font=ctk.CTkFont(size=13),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", pady=(3, 0))

        body = Card(self)
        body.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 24))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(body, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        scroll.columnconfigure((0, 1), weight=1)

        for index, field_name in enumerate(self.REWARD_FIELDS):
            value = getattr(settings, field_name)
            var = tk.StringVar(value=str(value))
            self.vars[field_name] = var

            row = index // 2
            column = index % 2
            item = ctk.CTkFrame(scroll, fg_color="#F8FAFC", corner_radius=14, border_color=CARD_BORDER, border_width=1)
            item.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
            item.columnconfigure(0, weight=1)
            ctk.CTkLabel(
                item,
                text=field_name,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=TEXT_MUTED,
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
            ctk.CTkEntry(
                item,
                textvariable=var,
                height=36,
                corner_radius=12,
                fg_color="#FFFFFF",
                border_color=CARD_BORDER,
            ).grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))

        button_bar = ctk.CTkFrame(body, fg_color="transparent")
        button_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        button_bar.columnconfigure((0, 1), weight=1)

        ctk.CTkButton(
            button_bar,
            text="Apply Runtime Config",
            height=42,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self.apply_config,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            button_bar,
            text="Reload From settings.py",
            height=42,
            corner_radius=14,
            fg_color="#E2E8F0",
            hover_color="#CBD5E1",
            text_color=TEXT_PRIMARY,
            command=self.reload_config,
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def apply_config(self) -> None:
        try:
            for field_name, var in self.vars.items():
                current_value = getattr(settings, field_name)
                raw_value = var.get().strip()

                if isinstance(current_value, int) and not isinstance(current_value, bool):
                    new_value = int(raw_value)
                elif isinstance(current_value, float):
                    new_value = float(raw_value)
                else:
                    new_value = raw_value

                setattr(settings, field_name, new_value)
                self._set_if_exists("app.reward", field_name, new_value)
                self._set_if_exists("app.optimizer", field_name, new_value)

        except ValueError as error:
            messagebox.showerror("Invalid Config", str(error))
            return

        messagebox.showinfo("Config Applied", "Reward config updated for this app session.")

    def reload_config(self) -> None:
        for field_name, var in self.vars.items():
            var.set(str(getattr(settings, field_name)))

    def _set_if_exists(self, module_name: str, field_name: str, value: object) -> None:
        module = __import__(module_name, fromlist=[field_name])
        if hasattr(module, field_name):
            setattr(module, field_name, value)


# -----------------------------------------------------------------------------
# Application Shell
# -----------------------------------------------------------------------------

class ScheduleOptimizerApp(ctk.CTk):
    def __init__(
        self,
        *,
        db_path: str | None = None,
        timezone: str | None = None,
        project_root: str | None = None,
        today: date | None = None,
    ) -> None:
        super().__init__()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title("Schedule Optimizer")
        self.geometry("1540x900")
        self.minsize(1240, 760)
        self.configure(fg_color=APP_BG)

        self.services: AppServices | None = None
        self.startup_error: str | None = None
        self.pages: dict[str, ctk.CTkFrame] = {}
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self._today = today or date.today()

        self._configure_treeview_style()
        try:
            self.services = open_app_services(db_path, timezone=timezone, project_root=project_root)
        except Exception as error:  # noqa: BLE001 - reported to the user; no scheduler without storage
            self.startup_error = describe_startup_failure(error, db_path)

        if self.services is None:
            self._build_startup_error()
            messagebox.showerror("Database Unavailable", self.startup_error, parent=self)
        else:
            self._build_shell()
            self.show_page("day")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    @property
    def execution_controller(self) -> ExecutionController | None:
        return self.services.execution_controller if self.services is not None else None

    @property
    def productivity_controller(self) -> ProductivityController | None:
        return self.services.productivity_controller if self.services is not None else None

    def close_services(self) -> None:
        """Wait for background work, then close the database (idempotent)."""
        if self.services is not None:
            self.services.close()

    def _on_close(self) -> None:
        self.close_services()
        self.destroy()

    def _configure_treeview_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background="#FFFFFF",
            foreground=TEXT_PRIMARY,
            fieldbackground="#FFFFFF",
            borderwidth=0,
            rowheight=32,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#F1F5F9",
            foreground=TEXT_MUTED,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padding=(8, 8),
        )
        style.map(
            "Treeview",
            background=[("selected", "#DBEAFE")],
            foreground=[("selected", TEXT_PRIMARY)],
        )

    def _build_startup_error(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        panel = Card(self)
        panel.grid(row=0, column=0, padx=80, pady=80, sticky="nsew")
        ctk.CTkLabel(
            panel, text="Database Unavailable", font=ctk.CTkFont(size=24, weight="bold"),
            text_color=DANGER, anchor="w",
        ).pack(anchor="w", padx=28, pady=(28, 12))
        ctk.CTkLabel(
            panel, text=self.startup_error, font=ctk.CTkFont(size=13), text_color=TEXT_PRIMARY,
            anchor="w", justify="left", wraplength=1100,
        ).pack(anchor="w", padx=28, pady=(0, 28))

    def _build_shell(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=230, corner_radius=0, fg_color="#0F172A")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self.sidebar,
            text="Schedule\nOptimizer",
            font=ctk.CTkFont(size=24, weight="bold"),
            text_color="white",
            justify="left",
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(28, 8))

        ctk.CTkLabel(
            self.sidebar,
            text="Optimization workspace",
            font=ctk.CTkFont(size=12),
            text_color="#94A3B8",
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 26))

        nav_items = [
            ("day", "Day Schedule"),
            ("week", "Week Schedule"),
            ("month", "Month Schedule"),
            ("reward", "Reward Config"),
            ("productivity", "Productivity"),
        ]

        for row, (page_name, label) in enumerate(nav_items, start=2):
            button = ctk.CTkButton(
                self.sidebar,
                text=label,
                height=44,
                corner_radius=14,
                anchor="w",
                fg_color="transparent",
                hover_color="#1E293B",
                text_color="#CBD5E1",
                font=ctk.CTkFont(size=14, weight="bold"),
                command=lambda name=page_name: self.show_page(name),
            )
            button.grid(row=row, column=0, sticky="ew", padx=16, pady=5)
            self.nav_buttons[page_name] = button

        spacer_row = 2 + len(nav_items)
        footer = ctk.CTkFrame(self.sidebar, fg_color="#111C31", corner_radius=18)
        footer.grid(row=spacer_row + 1, column=0, sticky="sew", padx=16, pady=(0, 20))
        ctk.CTkLabel(
            footer,
            text="Saved locally",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="white",
            anchor="w",
        ).pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            footer,
            text=f"Every change is saved to {self.services.db_path.name} in your data folder.",
            font=ctk.CTkFont(size=11),
            text_color="#94A3B8",
            wraplength=170,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 14))
        self.sidebar.grid_rowconfigure(spacer_row, weight=1)

        self.page_container = ctk.CTkFrame(self, fg_color=APP_BG, corner_radius=0)
        self.page_container.grid(row=0, column=1, sticky="nsew")
        self.page_container.grid_rowconfigure(0, weight=1)
        self.page_container.grid_columnconfigure(0, weight=1)

        services = self.services
        for mode_name, days in PAGE_DAYS.items():
            page_controller = SchedulePageController(
                services.planning_controller,
                number_of_days=days,
                anchor_date=default_anchor(mode_name, self._today),
                timezone=services.timezone,
            )
            self.pages[mode_name] = SchedulePage(
                self.page_container, mode_name, page_controller,
                services.execution_controller, services.productivity_controller,
            )
        self.pages["reward"] = RewardConfigPage(self.page_container)
        self.pages["productivity"] = ProductivityPage(self.page_container, services.productivity_controller)

        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

    def show_page(self, page_name: str) -> None:
        page = self.pages[page_name]
        page.tkraise()
        if isinstance(page, SchedulePage):
            page.on_show()
        for name, button in self.nav_buttons.items():
            if name == page_name:
                button.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="white")
            else:
                button.configure(fg_color="transparent", hover_color="#1E293B", text_color="#CBD5E1")


def main() -> None:
    app = ScheduleOptimizerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
