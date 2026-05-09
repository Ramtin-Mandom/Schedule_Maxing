"""
Modern CustomTkinter UI for the schedule optimizer.

Place this file inside your project `app/` folder as:

    app/ui_app.py

Run from the project root with:

    python -m app.ui_app

Install the extra UI dependency first:

    pip install customtkinter

This is still UI-only:
- It builds Task, FixedBlock, TimeWindow, and DaySchedule objects.
- It validates user input before accepting tasks.
- It calls the existing optimizer function.
- It displays scheduled and unscheduled tasks.
- It supports CSV upload and task removal.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from typing import Callable, Literal

try:
    import customtkinter as ctk
except ImportError as error:  # pragma: no cover - runtime dependency message
    raise ImportError(
        "This redesigned UI uses CustomTkinter. Install it with: "
        "pip install customtkinter"
    ) from error

from app.models import (
    Task,
    FixedBlock,
    DaySchedule,
    TimeWindow,
    DayScheduleOutput,
    ScheduledTask,
    UnscheduledTask,
)
from app.constraints import does_overlap
from app.optimizer import combine_fixed_and_optimized_scheduled_tasks
from app.data_processor import load_schedule_from_csv
from config import settings
from config.settings import (
    DEFAULT_DAY_START,
    DEFAULT_DAY_END,
    TIME_SLOT_MINUTES,
)


# -----------------------------------------------------------------------------
# Constants / Theme
# -----------------------------------------------------------------------------

CATEGORY_OPTIONS = [
    "study",
    "sleep",
    "food",
    "exercise",
    "work",
    "event",
    "entertainment",
    "errand",
    "other",
]

FIXED_OPTIONS = ["False", "True"]

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

TaskKind = Literal["fixed", "flexible"]


# -----------------------------------------------------------------------------
# Small Helpers
# -----------------------------------------------------------------------------

def minutes_to_hhmm(minutes: int) -> str:
    minutes = max(0, min(MINUTES_PER_DAY, minutes))
    hour = minutes // 60
    minute = minutes % 60
    if hour == 24:
        return "24:00"
    return f"{hour:02d}:{minute:02d}"


def format_window(start_time: int, end_time: int) -> str:
    return f"{minutes_to_hhmm(start_time)} - {minutes_to_hhmm(end_time)}"


def parse_dependency_string(raw_dependencies: str) -> list[str]:
    if not raw_dependencies.strip():
        return []

    separators = [";", ",", "-"]
    for separator in separators:
        if separator in raw_dependencies:
            return [part.strip() for part in raw_dependencies.split(separator) if part.strip()]

    return [raw_dependencies.strip()]


def get_unscheduled_name(task: UnscheduledTask) -> str:
    return str(getattr(task, "name", str(task)))


def get_unscheduled_reason(task: UnscheduledTask) -> str:
    return str(getattr(task, "reason", "unknown"))


# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------

class ScheduleState:
    """Stores UI-side schedule data before and after optimization."""

    def __init__(self, number_of_days: int) -> None:
        self.number_of_days = number_of_days
        self.days: dict[int, DaySchedule] = {
            day: DaySchedule(
                time_window=TimeWindow(
                    start_time=DEFAULT_DAY_START,
                    end_time=DEFAULT_DAY_END,
                ),
                fixed_blocks=[],
                tasks=[],
            )
            for day in range(1, number_of_days + 1)
        }
        self.outputs: dict[int, DayScheduleOutput] = {}

    def reset(self) -> None:
        self.__init__(self.number_of_days)

    def count_fixed(self) -> int:
        return sum(len(day.fixed_blocks) for day in self.days.values())

    def count_flexible(self) -> int:
        return sum(len(day.tasks) for day in self.days.values())

    def count_all_tasks(self) -> int:
        return self.count_fixed() + self.count_flexible()


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
    """Left-side task entry form."""

    def __init__(
        self,
        parent: tk.Widget,
        mode_name: str,
        number_of_days: int,
        on_add_task: Callable[[dict[str, str]], None],
    ) -> None:
        super().__init__(parent)

        self.mode_name = mode_name
        self.number_of_days = number_of_days
        self.on_add_task = on_add_task

        self.name_var = tk.StringVar()
        self.day_var = tk.StringVar(value="1")
        self.category_var = tk.StringVar(value=CATEGORY_OPTIONS[0])
        self.tag_var = tk.StringVar()
        self.fixed_var = tk.StringVar(value=FIXED_OPTIONS[0])
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.duration_var = tk.StringVar()
        self.priority_var = tk.StringVar()
        self.dependencies_var = tk.StringVar()

        self._build_form()
        self._sync_fixed_fields()

    def _build_form(self) -> None:
        self.columnconfigure(0, weight=1)

        SectionTitle(
            self,
            "Task Input",
            "Add fixed blocks or flexible tasks with preferred windows.",
        ).grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 10))

        form_body = ctk.CTkFrame(self, fg_color="transparent")
        form_body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        form_body.columnconfigure(0, weight=1)

        row = 0
        self._add_entry(form_body, row, "Task name", self.name_var, "e.g., Study Math")
        row += 1

        if self.mode_name != "day":
            self._add_entry(form_body, row, f"Day (1-{self.number_of_days})", self.day_var, "1")
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

        self.dependencies_entry = self._add_entry(
            form_body,
            row,
            "Dependencies",
            self.dependencies_var,
            "Task A - Task B",
        )
        row += 1

        ctk.CTkButton(
            self,
            text="+ Add Task",
            height=44,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._submit,
        ).grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 18))

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
        self.dependencies_entry.configure(state=state)

        if is_fixed:
            self.duration_var.set("")
            self.priority_var.set("")
            self.dependencies_var.set("")

    def _submit(self) -> None:
        values = {
            "name": self.name_var.get().strip(),
            "day": self.day_var.get().strip(),
            "category": self.category_var.get().strip(),
            "tag": self.tag_var.get().strip(),
            "fixed": self.fixed_var.get().strip(),
            "start_time": self.start_var.get().strip(),
            "end_time": self.end_var.get().strip(),
            "duration": self.duration_var.get().strip(),
            "priority": self.priority_var.get().strip(),
            "dependencies": self.dependencies_var.get().strip(),
        }
        self.on_add_task(values)

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
        self.dependencies_var.set("")
        self._sync_fixed_fields()


# -----------------------------------------------------------------------------
# Task Manager Panels
# -----------------------------------------------------------------------------

class AddedTasksPanel(ctk.CTkFrame):
    """Right-side task table."""

    def __init__(self, parent: tk.Widget, on_remove_task: Callable[[], None]) -> None:
        super().__init__(parent, fg_color="transparent")
        self.item_refs: dict[str, tuple[TaskKind, int, int]] = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Tasks currently stored in the schedule state.",
            text_color=TEXT_MUTED,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 10))

        table_frame = ctk.CTkFrame(self, fg_color="#FFFFFF", corner_radius=14, border_color=CARD_BORDER, border_width=1)
        table_frame.grid(row=1, column=0, sticky="nsew", padx=8)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_frame,
            columns=("day", "name", "type", "time"),
            show="headings",
            selectmode="browse",
            height=16,
        )
        self.tree.heading("day", text="Day")
        self.tree.heading("name", text="Task")
        self.tree.heading("type", text="Type")
        self.tree.heading("time", text="Time / Preference")
        self.tree.column("day", width=48, minwidth=44, anchor="center", stretch=False)
        self.tree.column("name", width=150, minwidth=110, anchor="w", stretch=True)
        self.tree.column("type", width=80, minwidth=75, anchor="center", stretch=False)
        self.tree.column("time", width=145, minwidth=125, anchor="center", stretch=False)

        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 0))
        y_scroll.grid(row=0, column=1, sticky="ns", pady=(10, 0))
        x_scroll.grid(row=1, column=0, sticky="ew", padx=(10, 0), pady=(0, 10))

        ctk.CTkButton(
            self,
            text="Remove Selected Task",
            height=40,
            corner_radius=14,
            fg_color=DANGER,
            hover_color=DANGER_HOVER,
            command=on_remove_task,
        ).grid(row=2, column=0, sticky="ew", padx=8, pady=(12, 6))

        ctk.CTkLabel(
            self,
            text="Removing a task also removes it from the schedule strip.",
            text_color=TEXT_MUTED,
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))

    def clear(self) -> None:
        self.item_refs.clear()
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

    def refresh(self, state: ScheduleState) -> None:
        self.clear()

        for day, day_schedule in state.days.items():
            for index, block in enumerate(day_schedule.fixed_blocks):
                item_id = f"fixed-{day}-{index}"
                self.item_refs[item_id] = ("fixed", day, index)
                self.tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    values=(
                        day,
                        block.name,
                        "fixed",
                        format_window(block.time_window.start_time, block.time_window.end_time),
                    ),
                    tags=("fixed",),
                )

            for index, task in enumerate(day_schedule.tasks):
                item_id = f"flexible-{day}-{index}"
                self.item_refs[item_id] = ("flexible", day, index)
                self.tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    values=(
                        day,
                        task.name,
                        "flexible",
                        f"pref {format_window(task.preference_time.start_time, task.preference_time.end_time)}",
                    ),
                    tags=("flexible",),
                )

        self.tree.tag_configure("fixed", foreground="#334155")
        self.tree.tag_configure("flexible", foreground="#1E3A8A")

    def selected_ref(self) -> tuple[TaskKind, int, int] | None:
        selected = self.tree.selection()
        if not selected:
            return None
        return self.item_refs.get(selected[0])


class UnscheduledPanel(ctk.CTkFrame):
    """Right-side optimizer failure table."""

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
        self.tree.heading("day", text="Day")
        self.tree.heading("task", text="Task")
        self.tree.heading("reason", text="Reason")
        self.tree.column("day", width=48, minwidth=44, anchor="center", stretch=False)
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

    def add_unscheduled_result(self, day: int, task: UnscheduledTask) -> None:
        self.tree.insert(
            "",
            tk.END,
            values=(day, get_unscheduled_name(task), get_unscheduled_reason(task)),
        )
        self.empty_text.configure(text="Some tasks could not be placed. Review the reasons below.")


# -----------------------------------------------------------------------------
# Schedule Canvas
# -----------------------------------------------------------------------------

class ScheduleCanvas(Card):
    """Scrollable visual schedule strips."""

    def __init__(self, parent: tk.Widget, number_of_days: int) -> None:
        super().__init__(parent)
        self.number_of_days = number_of_days
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 6))
        header.columnconfigure(0, weight=1)

        SectionTitle(header, "Schedule Strip", "Preview preferred windows, then run the optimizer.").grid(
            row=0, column=0, sticky="ew"
        )

        legend = ctk.CTkFrame(header, fg_color="transparent")
        legend.grid(row=0, column=1, sticky="e")
        self._legend_item(legend, "Fixed", CATEGORY_COLORS["fixed"], 0)
        self._legend_item(legend, "Flexible preview", "#DBEAFE", 1)
        self._legend_item(legend, "Optimized", ACCENT, 2)

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

    def draw_state(self, state: ScheduleState) -> None:
        """Draw fixed tasks and flexible tasks at their preferred windows."""
        self.draw_empty()
        for day, day_schedule in state.days.items():
            for block in day_schedule.fixed_blocks:
                task = ScheduledTask(
                    name=block.name,
                    category=block.category,
                    tag="fixed",
                    time_window=block.time_window,
                    score=0,
                )
                self._draw_task_box(day, task, mode="fixed")

            for task in day_schedule.tasks:
                preview_task = ScheduledTask(
                    name=f"{task.name}  · pref",
                    category=task.category,
                    tag=task.tag,
                    time_window=task.preference_time,
                    score=0,
                )
                self._draw_task_box(day, preview_task, mode="preview")

    def draw_outputs(self, outputs: dict[int, DayScheduleOutput]) -> None:
        self.draw_empty()
        for day, output in outputs.items():
            for task in output.scheduled_tasks:
                self._draw_task_box(day, task, mode="optimized")

    def _day_x(self, day: int) -> int:
        return 78 + (day - 1) * DAY_STRIP_WIDTH

    def _minute_y(self, minute: int) -> float:
        return DAY_HEADER_HEIGHT + (minute / 60) * PIXELS_PER_HOUR

    def _draw_day_strip(self, day: int) -> None:
        x0 = self._day_x(day)
        x1 = x0 + DAY_STRIP_WIDTH - 20
        y0 = DAY_HEADER_HEIGHT
        y1 = DAY_HEADER_HEIGHT + DAY_HEIGHT

        self.canvas.create_text(
            (x0 + x1) / 2,
            20,
            text=f"Day {day}",
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

    def _draw_task_box(
        self,
        day: int,
        task: ScheduledTask,
        mode: Literal["fixed", "preview", "optimized"],
    ) -> None:
        x0 = self._day_x(day) + 10
        x1 = x0 + DAY_STRIP_WIDTH - 40
        y0 = self._minute_y(task.time_window.start_time) + 3
        y1 = self._minute_y(task.time_window.end_time) - 3

        if y1 - y0 < 18:
            y1 = y0 + 18

        category = task.category if task.category in CATEGORY_COLORS else "other"
        fill = CATEGORY_COLORS.get(category, CATEGORY_COLORS["other"])
        text_fill = CATEGORY_TEXT_COLORS.get(category, TEXT_PRIMARY)
        outline = "#FFFFFF"
        dash = None

        if mode == "preview":
            outline = fill
            fill = "#EFF6FF"
            dash = (4, 3)
            text_fill = "#1E3A8A"
        elif mode == "optimized" and task.score > 0:
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
            text=task.name,
            width=DAY_STRIP_WIDTH - 50,
            font=("Segoe UI", name_font_size, "bold"),
            fill=text_fill,
            justify="center",
        )

        if box_height >= 34:
            self.canvas.create_text(
                (x0 + x1) / 2,
                y1 - 9,
                text=format_window(task.time_window.start_time, task.time_window.end_time),
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
# Schedule Page
# -----------------------------------------------------------------------------

class SchedulePage(ctk.CTkFrame):
    """Reusable page for day, week, and month scheduling."""

    def __init__(self, parent: tk.Widget, mode_name: str, number_of_days: int) -> None:
        super().__init__(parent, fg_color=APP_BG)
        self.mode_name = mode_name
        self.number_of_days = number_of_days
        self.state = ScheduleState(number_of_days)

        self.columnconfigure(0, weight=0, minsize=360)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=0, minsize=430)
        self.rowconfigure(1, weight=1)

        self._build_header()
        self._build_left_panel()
        self._build_schedule_canvas()
        self._build_right_panel()
        self.refresh_all()

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

        ctk.CTkLabel(
            header,
            text="Build a schedule, preview task windows, upload CSV data, then optimize.",
            font=ctk.CTkFont(size=13),
            text_color=TEXT_MUTED,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        stats = ctk.CTkFrame(header, fg_color="transparent")
        stats.grid(row=0, column=1, rowspan=2, sticky="e")
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
            on_add_task=self.add_task,
        )
        self.form.grid(row=0, column=0, sticky="ew", pady=(0, 14))

        controls = Card(self.left_panel)
        controls.grid(row=1, column=0, sticky="ew")
        controls.columnconfigure((0, 1), weight=1)
        SectionTitle(controls, "Controls", "Run optimization or load a CSV schedule.").grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(18, 12)
        )

        ctk.CTkButton(
            controls,
            text="Make Schedule",
            height=42,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.make_schedule,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 10))

        ctk.CTkButton(
            controls,
            text="Upload CSV",
            height=38,
            corner_radius=14,
            fg_color="#334155",
            hover_color="#1E293B",
            command=self.upload_csv,
        ).grid(row=2, column=0, sticky="ew", padx=(18, 6), pady=(0, 18))

        ctk.CTkButton(
            controls,
            text="Reset",
            height=38,
            corner_radius=14,
            fg_color="#E2E8F0",
            hover_color="#CBD5E1",
            text_color=TEXT_PRIMARY,
            command=self.reset,
        ).grid(row=2, column=1, sticky="ew", padx=(6, 18), pady=(0, 18))

    def _build_schedule_canvas(self) -> None:
        self.schedule_canvas = ScheduleCanvas(self, self.number_of_days)
        self.schedule_canvas.grid(row=1, column=1, sticky="nsew", padx=8, pady=(0, 20))

    def _build_right_panel(self) -> None:
        right_card = Card(self)
        right_card.grid(row=1, column=2, sticky="nsew", padx=(8, 20), pady=(0, 20))
        right_card.columnconfigure(0, weight=1)
        right_card.rowconfigure(1, weight=1)

        SectionTitle(right_card, "Task Manager", "Review, remove, and inspect optimizer results.").grid(
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

        self.added_tasks_panel = AddedTasksPanel(added_tab, on_remove_task=self.remove_selected_task)
        self.unscheduled_panel = UnscheduledPanel(unscheduled_tab)
        self.added_tasks_panel.grid(row=0, column=0, sticky="nsew")
        self.unscheduled_panel.grid(row=0, column=0, sticky="nsew")

    # ----------------------------- User actions -----------------------------

    def add_task(self, values: dict[str, str]) -> None:
        try:
            validated = self._validate_values(values)
        except ValueError as error:
            messagebox.showerror("Invalid Task", str(error))
            return

        day = validated["day"]
        day_schedule = self.state.days[day]

        if validated["fixed"]:
            fixed_block = FixedBlock(
                name=validated["name"],
                category=validated["category"],
                time_window=TimeWindow(
                    start_time=validated["start_time"],
                    end_time=validated["end_time"],
                ),
            )
            day_schedule.fixed_blocks.append(fixed_block)
        else:
            task = Task(
                name=validated["name"],
                date=day,
                category=validated["category"],
                tag=validated["tag"],
                fixed=False,
                duration=validated["duration"],
                priority=validated["priority"],
                preference_time=TimeWindow(
                    start_time=validated["start_time"],
                    end_time=validated["end_time"],
                ),
                dependencies=validated["dependencies"],
            )
            day_schedule.tasks.append(task)

        self.state.outputs = {}
        self.form.clear_fields()
        self.refresh_all()

    def remove_selected_task(self) -> None:
        selected = self.added_tasks_panel.selected_ref()
        if selected is None:
            messagebox.showinfo("No Selection", "Select a task to remove first.")
            return

        task_type, day, index = selected
        day_schedule = self.state.days[day]

        if task_type == "fixed":
            if index < len(day_schedule.fixed_blocks):
                del day_schedule.fixed_blocks[index]
        else:
            if index < len(day_schedule.tasks):
                del day_schedule.tasks[index]

        self.state.outputs = {}
        self.unscheduled_panel.clear()
        self.refresh_all()

    def upload_csv(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Upload Schedule CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not file_path:
            return

        try:
            schedule_input = load_schedule_from_csv(file_path)
            imported_days = schedule_input.schedules
            self._validate_imported_days(imported_days)
            self._validate_imported_fixed_blocks(imported_days)
        except Exception as error:
            messagebox.showerror("CSV Import Error", str(error))
            return

        self.state.reset()
        for day, day_schedule in imported_days.items():
            if 1 <= day <= self.number_of_days:
                self.state.days[day] = day_schedule

        self.state.outputs = {}
        self.unscheduled_panel.clear()
        self.refresh_all()

    def make_schedule(self) -> None:
        outputs: dict[int, DayScheduleOutput] = {}

        try:
            for day, day_schedule in self.state.days.items():
                if not day_schedule.fixed_blocks and not day_schedule.tasks:
                    continue

                outputs[day] = combine_fixed_and_optimized_scheduled_tasks(
                    date=day,
                    day_schedule=day_schedule,
                )
        except ValueError as error:
            messagebox.showerror("Scheduling Error", str(error))
            return

        self.state.outputs = outputs
        self.schedule_canvas.draw_outputs(outputs)
        self._refresh_unscheduled_results(outputs)
        self._refresh_stats()

        if not outputs:
            messagebox.showinfo("No Tasks", "There are no tasks to schedule yet.")

    def reset(self) -> None:
        self.state.reset()
        self.form.clear_fields()
        self.unscheduled_panel.clear()
        self.refresh_all()

    # ----------------------------- Refresh logic -----------------------------

    def refresh_all(self) -> None:
        self.added_tasks_panel.refresh(self.state)
        self.schedule_canvas.draw_state(self.state)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        self.total_pill.set_value(str(self.state.count_all_tasks()))
        self.fixed_pill.set_value(str(self.state.count_fixed()))
        self.flex_pill.set_value(str(self.state.count_flexible()))

    def _refresh_unscheduled_results(self, outputs: dict[int, DayScheduleOutput]) -> None:
        self.unscheduled_panel.clear()
        for day, output in outputs.items():
            for task in getattr(output, "unscheduled_tasks", []):
                self.unscheduled_panel.add_unscheduled_result(day, task)

    # ------------------------------- Validation ------------------------------

    def _validate_values(self, values: dict[str, str]) -> dict[str, object]:
        name = values["name"]
        category = values["category"]
        tag = values["tag"]
        fixed_text = values["fixed"]

        if not name:
            raise ValueError("Name is required.")

        day = self._parse_int(values["day"], "Day")
        if not 1 <= day <= self.number_of_days:
            raise ValueError(f"Day must be between 1 and {self.number_of_days}.")

        if not category or category not in CATEGORY_OPTIONS:
            raise ValueError("Category must be selected from the category dropdown.")

        if not tag:
            raise ValueError("Tag is required.")

        if fixed_text not in FIXED_OPTIONS:
            raise ValueError("Fixed must be either True or False.")

        fixed = fixed_text == "True"
        start_time = self._parse_int(values["start_time"], "Start time")
        end_time = self._parse_int(values["end_time"], "End time")
        self._validate_time_window(start_time, end_time)

        dependencies: list[str] = []
        duration = 0
        priority = 1

        if fixed:
            self._validate_no_fixed_overlap(day, start_time, end_time)
        else:
            duration = self._parse_int(values["duration"], "Duration")
            priority = self._parse_int(values["priority"], "Priority")

            if duration <= 0:
                raise ValueError("Duration must be greater than 0.")
            if duration % TIME_SLOT_MINUTES != 0:
                raise ValueError(f"Duration must be a multiple of {TIME_SLOT_MINUTES} minutes.")
            if duration > MINUTES_PER_DAY:
                raise ValueError("Duration cannot be longer than 24 hours.")
            if not 1 <= priority <= 10:
                raise ValueError("Priority must be between 1 and 10.")

            dependencies = parse_dependency_string(values["dependencies"])

        return {
            "name": name,
            "day": day,
            "category": category,
            "tag": tag,
            "fixed": fixed,
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "priority": priority,
            "dependencies": dependencies,
        }

    def _parse_int(self, value: str, field_name: str) -> int:
        if value == "":
            raise ValueError(f"{field_name} is required.")
        try:
            return int(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an integer.") from error

    def _validate_time_window(self, start_time: int, end_time: int) -> None:
        if start_time < 0 or end_time > MINUTES_PER_DAY:
            raise ValueError("Times must be between 0 and 1440 minutes.")
        if start_time >= end_time:
            raise ValueError("Start time must be smaller than end time.")
        if start_time % TIME_SLOT_MINUTES != 0 or end_time % TIME_SLOT_MINUTES != 0:
            raise ValueError(f"Start and end times must be multiples of {TIME_SLOT_MINUTES} minutes.")

    def _validate_no_fixed_overlap(self, day: int, start_time: int, end_time: int) -> None:
        fixed_blocks = self.state.days[day].fixed_blocks
        for block in fixed_blocks:
            if does_overlap(
                start_time,
                end_time,
                block.time_window.start_time,
                block.time_window.end_time,
            ):
                raise ValueError(f"Fixed task overlaps with existing fixed task: {block.name}")

    def _validate_imported_days(self, imported_days: dict[int, DaySchedule]) -> None:
        invalid_days = [day for day in imported_days if day < 1 or day > self.number_of_days]
        if invalid_days:
            raise ValueError(
                f"CSV contains day/date values outside 1-{self.number_of_days}: {invalid_days}"
            )

    def _validate_imported_fixed_blocks(self, imported_days: dict[int, DaySchedule]) -> None:
        for day, day_schedule in imported_days.items():
            fixed_blocks = day_schedule.fixed_blocks

            for i in range(len(fixed_blocks)):
                current = fixed_blocks[i]
                self._validate_time_window(current.time_window.start_time, current.time_window.end_time)

                for j in range(i + 1, len(fixed_blocks)):
                    other = fixed_blocks[j]
                    if does_overlap(
                        current.time_window.start_time,
                        current.time_window.end_time,
                        other.time_window.start_time,
                        other.time_window.end_time,
                    ):
                        raise ValueError(
                            f"CSV fixed tasks overlap on day {day}: {current.name} and {other.name}"
                        )

            for task in day_schedule.tasks:
                self._validate_time_window(task.preference_time.start_time, task.preference_time.end_time)
                if task.duration <= 0:
                    raise ValueError(f"CSV task has invalid duration: {task.name}")
                if task.duration % TIME_SLOT_MINUTES != 0:
                    raise ValueError(
                        f"CSV task duration must be a multiple of {TIME_SLOT_MINUTES}: {task.name}"
                    )
                if not 1 <= task.priority <= 10:
                    raise ValueError(f"CSV task has invalid priority: {task.name}")


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
            text="These values are applied at runtime only. They do not rewrite config/settings.py.",
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
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title("Schedule Optimizer")
        self.geometry("1540x900")
        self.minsize(1240, 760)
        self.configure(fg_color=APP_BG)

        self._configure_treeview_style()
        self._build_shell()
        self.show_page("day")

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

        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("day", "Day Schedule"),
            ("week", "Week Schedule"),
            ("month", "Month Schedule"),
            ("reward", "Reward Config"),
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

        footer = ctk.CTkFrame(self.sidebar, fg_color="#111C31", corner_radius=18)
        footer.grid(row=7, column=0, sticky="sew", padx=16, pady=(0, 20))
        ctk.CTkLabel(
            footer,
            text="Tip",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="white",
            anchor="w",
        ).pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            footer,
            text="Flexible tasks are shown as preferred-window previews before optimization.",
            font=ctk.CTkFont(size=11),
            text_color="#94A3B8",
            wraplength=170,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 14))
        self.sidebar.grid_rowconfigure(6, weight=1)

        self.page_container = ctk.CTkFrame(self, fg_color=APP_BG, corner_radius=0)
        self.page_container.grid(row=0, column=1, sticky="nsew")
        self.page_container.grid_rowconfigure(0, weight=1)
        self.page_container.grid_columnconfigure(0, weight=1)

        self.pages: dict[str, ctk.CTkFrame] = {
            "day": SchedulePage(self.page_container, "day", 1),
            "week": SchedulePage(self.page_container, "week", 7),
            "month": SchedulePage(self.page_container, "month", 30),
            "reward": RewardConfigPage(self.page_container),
        }

        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

    def show_page(self, page_name: str) -> None:
        self.pages[page_name].tkraise()
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
