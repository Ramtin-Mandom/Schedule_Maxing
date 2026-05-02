"""
Tkinter UI for the schedule optimizer.

Place this file inside your project `app/` folder as:

    app/ui_app.py

Run from the project root with:

    python -m app.ui_app

This file is intentionally UI-only:
- It builds Task, FixedBlock, TimeWindow, and DaySchedule objects.
- It validates user input before accepting tasks.
- It calls the existing optimizer function.
- It displays scheduled and unscheduled tasks.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable

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
from config import settings
from config.settings import (
    DEFAULT_DAY_START,
    DEFAULT_DAY_END,
    TIME_SLOT_MINUTES,
)


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
    "study": "#89CFF0",
    "sleep": "#CDB4DB",
    "food": "#FFD6A5",
    "exercise": "#BDE0BE",
    "work": "#A0C4FF",
    "event": "#FFC6FF",
    "entertainment": "#FDFFB6",
    "errand": "#FFADAD",
    "other": "#D9D9D9",
    "fixed": "#B8B8B8",
}

DAY_STRIP_WIDTH = 150
DAY_HEADER_HEIGHT = 32
PIXELS_PER_HOUR = 48
DAY_HEIGHT = 24 * PIXELS_PER_HOUR
MINUTES_PER_DAY = 24 * 60


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


class TaskForm(ttk.LabelFrame):
    """Left-side form used for day, week, and month modes."""

    def __init__(
        self,
        parent: tk.Widget,
        mode_name: str,
        number_of_days: int,
        on_add_task: Callable[[dict], None],
    ) -> None:
        super().__init__(parent, text="Task Input")

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
        row = 0

        self._add_entry(row, "Name", self.name_var)
        row += 1

        if self.mode_name != "day":
            self._add_entry(row, f"Day (1-{self.number_of_days})", self.day_var)
            row += 1

        self._add_dropdown(row, "Category", self.category_var, CATEGORY_OPTIONS)
        row += 1

        self._add_entry(row, "Tag", self.tag_var)
        row += 1

        fixed_box = self._add_dropdown(row, "Fixed", self.fixed_var, FIXED_OPTIONS)
        fixed_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_fixed_fields())
        row += 1

        self._add_entry(row, "Start time (minutes)", self.start_var)
        row += 1

        self._add_entry(row, "End time (minutes)", self.end_var)
        row += 1

        self.duration_label, self.duration_entry = self._add_entry(
            row,
            "Duration (minutes)",
            self.duration_var,
        )
        row += 1

        self.priority_label, self.priority_entry = self._add_entry(
            row,
            "Priority (1-10)",
            self.priority_var,
        )
        row += 1

        self.dependencies_label, self.dependencies_entry = self._add_entry(
            row,
            "Dependencies (- separated)",
            self.dependencies_var,
        )
        row += 1

        ttk.Button(
            self,
            text="Add Task",
            command=self._submit,
        ).grid(row=row, column=0, columnspan=2, sticky="ew", padx=6, pady=(12, 4))

    def _add_entry(
        self,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> tuple[ttk.Label, ttk.Entry]:
        label_widget = ttk.Label(self, text=label)
        label_widget.grid(row=row, column=0, sticky="w", padx=6, pady=4)

        entry = ttk.Entry(self, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        self.columnconfigure(1, weight=1)
        return label_widget, entry

    def _add_dropdown(
        self,
        row: int,
        label: str,
        variable: tk.StringVar,
        values: list[str],
    ) -> ttk.Combobox:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)

        box = ttk.Combobox(
            self,
            textvariable=variable,
            values=values,
            state="readonly",
        )
        box.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        return box

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


class UnscheduledPanel(ttk.LabelFrame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, text="Unscheduled Tasks")
        self.listbox = tk.Listbox(self, height=12)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)

        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def clear(self) -> None:
        self.listbox.delete(0, tk.END)

    def add_flexible_task(self, day: int, task: Task) -> None:
        self.listbox.insert(
            tk.END,
            f"Day {day}: {task.name} | flexible | {task.duration} min",
        )

    def add_unscheduled_result(self, day: int, task: UnscheduledTask) -> None:
        self.listbox.insert(
            tk.END,
            f"Day {day}: {task.name} | reason: {task.reason}",
        )


class ScheduleCanvas(ttk.Frame):
    """Scrollable visual schedule strips."""

    def __init__(self, parent: tk.Widget, number_of_days: int) -> None:
        super().__init__(parent)
        self.number_of_days = number_of_days

        self.canvas = tk.Canvas(self, background="white", highlightthickness=0)
        self.v_scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.h_scroll = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(
            yscrollcommand=self.v_scroll.set,
            xscrollcommand=self.h_scroll.set,
        )

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.v_scroll.grid(row=0, column=1, sticky="ns")
        self.h_scroll.grid(row=1, column=0, sticky="ew")

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.draw_empty()

    def draw_empty(self) -> None:
        self.canvas.delete("all")

        total_width = self.number_of_days * DAY_STRIP_WIDTH + 80
        total_height = DAY_HEADER_HEIGHT + DAY_HEIGHT + 20
        self.canvas.configure(scrollregion=(0, 0, total_width, total_height))

        for day in range(1, self.number_of_days + 1):
            self._draw_day_strip(day)

    def draw_fixed_blocks(self, state: ScheduleState) -> None:
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
                self._draw_task_box(day, task)

    def draw_outputs(self, outputs: dict[int, DayScheduleOutput]) -> None:
        self.draw_empty()
        for day, output in outputs.items():
            for task in output.scheduled_tasks:
                self._draw_task_box(day, task)

    def _day_x(self, day: int) -> int:
        return 70 + (day - 1) * DAY_STRIP_WIDTH

    def _minute_y(self, minute: int) -> float:
        return DAY_HEADER_HEIGHT + (minute / 60) * PIXELS_PER_HOUR

    def _draw_day_strip(self, day: int) -> None:
        x0 = self._day_x(day)
        x1 = x0 + DAY_STRIP_WIDTH - 16

        self.canvas.create_text(
            (x0 + x1) / 2,
            16,
            text=f"Day {day}",
            font=("Arial", 11, "bold"),
        )

        self.canvas.create_rectangle(
            x0,
            DAY_HEADER_HEIGHT,
            x1,
            DAY_HEADER_HEIGHT + DAY_HEIGHT,
            outline="#999999",
            fill="#FAFAFA",
        )

        for hour in range(25):
            y = DAY_HEADER_HEIGHT + hour * PIXELS_PER_HOUR
            self.canvas.create_line(x0, y, x1, y, fill="#DDDDDD")

            if day == 1:
                label = "24:00" if hour == 24 else f"{hour:02d}:00"
                self.canvas.create_text(
                    35,
                    y,
                    text=label,
                    anchor="e",
                    font=("Arial", 8),
                    fill="#555555",
                )

    def _draw_task_box(self, day: int, task: ScheduledTask) -> None:
        x0 = self._day_x(day) + 4
        x1 = x0 + DAY_STRIP_WIDTH - 24
        y0 = self._minute_y(task.time_window.start_time)
        y1 = self._minute_y(task.time_window.end_time)

        color = CATEGORY_COLORS.get(task.category, CATEGORY_COLORS["other"])

        self.canvas.create_rectangle(
            x0,
            y0,
            x1,
            y1,
            fill=color,
            outline="#555555",
        )

        box_height = max(y1 - y0, 12)
        font_size = 8 if box_height < 30 else 9
        self.canvas.create_text(
            (x0 + x1) / 2,
            (y0 + y1) / 2,
            text=task.name,
            width=DAY_STRIP_WIDTH - 34,
            font=("Arial", font_size, "bold"),
        )


class SchedulePage(ttk.Frame):
    """Reusable page for day, week, and month scheduling."""

    def __init__(self, parent: tk.Widget, mode_name: str, number_of_days: int) -> None:
        super().__init__(parent)
        self.mode_name = mode_name
        self.number_of_days = number_of_days
        self.state = ScheduleState(number_of_days)

        left_panel = ttk.Frame(self)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        left_panel.rowconfigure(1, weight=1)
        left_panel.columnconfigure(0, weight=1, minsize=330)

        self.form = TaskForm(
            left_panel,
            mode_name=mode_name,
            number_of_days=number_of_days,
            on_add_task=self.add_task,
        )
        self.unscheduled_panel = UnscheduledPanel(left_panel)

        controls_frame = ttk.LabelFrame(left_panel, text="Schedule Controls")
        controls_frame.columnconfigure((0, 1), weight=1)
        ttk.Button(
            controls_frame,
            text="Make Schedule",
            command=self.make_schedule,
        ).grid(row=0, column=0, sticky="ew", padx=6, pady=8)
        ttk.Button(
            controls_frame,
            text="Reset",
            command=self.reset,
        ).grid(row=0, column=1, sticky="ew", padx=6, pady=8)

        self.schedule_canvas = ScheduleCanvas(self, number_of_days)

        self.form.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.unscheduled_panel.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        controls_frame.grid(row=2, column=0, sticky="ew")
        self.schedule_canvas.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=0, minsize=360)
        self.columnconfigure(1, weight=1)

    def add_task(self, values: dict) -> None:
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
            self.schedule_canvas.draw_fixed_blocks(self.state)
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
            self.unscheduled_panel.add_flexible_task(day, task)

        self.form.clear_fields()

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

        if not outputs:
            messagebox.showinfo("No Tasks", "There are no tasks to schedule yet.")

    def reset(self) -> None:
        self.state.reset()
        self.form.clear_fields()
        self.unscheduled_panel.clear()
        self.schedule_canvas.draw_empty()

    def _refresh_unscheduled_results(self, outputs: dict[int, DayScheduleOutput]) -> None:
        self.unscheduled_panel.clear()

        for day, day_schedule in self.state.days.items():
            for task in day_schedule.tasks:
                self.unscheduled_panel.add_flexible_task(day, task)

        if outputs:
            self.unscheduled_panel.listbox.insert(tk.END, "--- Optimization Results ---")

        for day, output in outputs.items():
            for task in output.unscheduled_tasks:
                self.unscheduled_panel.add_unscheduled_result(day, task)

    def _validate_values(self, values: dict) -> dict:
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
                raise ValueError(
                    f"Duration must be a multiple of {TIME_SLOT_MINUTES} minutes."
                )

            if duration > MINUTES_PER_DAY:
                raise ValueError("Duration cannot be longer than 24 hours.")

            if not 1 <= priority <= 10:
                raise ValueError("Priority must be between 1 and 10.")

            dependencies = self._parse_dependencies(values["dependencies"])

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
            raise ValueError(
                f"Start and end times must be multiples of {TIME_SLOT_MINUTES} minutes."
            )

    def _validate_no_fixed_overlap(
        self,
        day: int,
        start_time: int,
        end_time: int,
    ) -> None:
        fixed_blocks = self.state.days[day].fixed_blocks

        for block in fixed_blocks:
            if does_overlap(
                start_time,
                end_time,
                block.time_window.start_time,
                block.time_window.end_time,
            ):
                raise ValueError(
                    f"Fixed task overlaps with existing fixed task: {block.name}"
                )

    def _parse_dependencies(self, raw_dependencies: str) -> list[str]:
        if not raw_dependencies.strip():
            return []

        return [
            dependency.strip()
            for dependency in raw_dependencies.split("-")
            if dependency.strip()
        ]


class RewardConfigPage(ttk.Frame):
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
        super().__init__(parent)
        self.vars: dict[str, tk.StringVar] = {}
        self._build()

    def _build(self) -> None:
        title = ttk.Label(
            self,
            text="Reward Config",
            font=("Arial", 16, "bold"),
        )
        title.grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 6))

        note = ttk.Label(
            self,
            text=(
                "These values are applied at runtime only. "
                "They do not rewrite config/settings.py."
            ),
        )
        note.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 12))

        for index, field_name in enumerate(self.REWARD_FIELDS, start=2):
            ttk.Label(self, text=field_name).grid(
                row=index,
                column=0,
                sticky="w",
                padx=12,
                pady=4,
            )
            value = getattr(settings, field_name)
            var = tk.StringVar(value=str(value))
            self.vars[field_name] = var
            ttk.Entry(self, textvariable=var, width=24).grid(
                row=index,
                column=1,
                sticky="w",
                padx=12,
                pady=4,
            )

        button_row = len(self.REWARD_FIELDS) + 3
        ttk.Button(
            self,
            text="Apply Runtime Config",
            command=self.apply_config,
        ).grid(row=button_row, column=0, sticky="w", padx=12, pady=12)

        ttk.Button(
            self,
            text="Reload From settings.py",
            command=self.reload_config,
        ).grid(row=button_row, column=1, sticky="w", padx=12, pady=12)

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

                # The optimizer and reward modules import constants directly.
                # Updating these modules keeps the UI config runtime-only while
                # still using the existing project logic.
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


class ScheduleOptimizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Schedule Optimizer")
        self.geometry("1200x760")
        self.minsize(1000, 650)

        self._configure_style()
        self._build_menu()
        self._build_pages()
        self.show_page("day")

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", padding=6)
        style.configure("TLabel", padding=2)
        style.configure("TLabelframe", padding=8)

    def _build_menu(self) -> None:
        self.menu_frame = ttk.Frame(self)
        self.menu_frame.pack(side="top", fill="x", padx=8, pady=8)

        ttk.Label(
            self.menu_frame,
            text="Menu:",
            font=("Arial", 12, "bold"),
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            self.menu_frame,
            text="Day Schedule",
            command=lambda: self.show_page("day"),
        ).pack(side="left", padx=4)

        ttk.Button(
            self.menu_frame,
            text="Week Schedule",
            command=lambda: self.show_page("week"),
        ).pack(side="left", padx=4)

        ttk.Button(
            self.menu_frame,
            text="Month Schedule",
            command=lambda: self.show_page("month"),
        ).pack(side="left", padx=4)

        ttk.Button(
            self.menu_frame,
            text="Reward Config",
            command=lambda: self.show_page("reward"),
        ).pack(side="left", padx=4)

    def _build_pages(self) -> None:
        self.page_container = ttk.Frame(self)
        self.page_container.pack(fill="both", expand=True)
        self.page_container.rowconfigure(0, weight=1)
        self.page_container.columnconfigure(0, weight=1)

        self.pages: dict[str, ttk.Frame] = {
            "day": SchedulePage(self.page_container, "day", 1),
            "week": SchedulePage(self.page_container, "week", 7),
            "month": SchedulePage(self.page_container, "month", 30),
            "reward": RewardConfigPage(self.page_container),
        }

        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

    def show_page(self, page_name: str) -> None:
        self.pages[page_name].tkraise()


def main() -> None:
    app = ScheduleOptimizerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
