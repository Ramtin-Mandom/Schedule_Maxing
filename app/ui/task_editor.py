"""
app/ui/task_editor.py

The reusable task form (Milestone 4, Prompt 3) that Day, Week and Month --
and later Projects -- share. Widgets only: it collects a TaskDraft
(app/ui/task_form_model.py) and hands it to its page, which saves it through
the presenter and the services; field errors come back as FormErrors and are
shown next to their fields, with the typed values kept.

Controls:

- Times (preferred window, deadline time, fixed-block start/end) use the
  shared [ Hour ] : [ Minute ] [ AM/PM ] input (app/ui/clock_input.py),
  exact to the minute; an end time of 12:00 AM means the next midnight.
  No minutes-from-midnight and no spinners anywhere.
- The date is not typed or shown: a new task or block belongs to the date
  the page has selected (Day: its date, today unless opened for another;
  Week/Month: the selected day). The page passes it with set_date(). Editing
  keeps the record's own date (an undated imported task stays undated) and
  whether it is pinned to it.
- Category and Project sit side by side. No category chosen saves as
  "other"; the categories are the built-in ones plus those added in Settings
  (app/ui/task_defaults.py). Project starts at "None" and lists the
  workspace's live projects.
- Points: typed, or stepped by 10 with the - / + buttons.
- DurationField: minutes typed as "13", "1 h 13 min" or "1:13", or stepped
  (1 minute, 15 with Shift).
- DateField (the optional deadline date): YYYY-MM-DD, Up/Down move a day.
- TagInput: Enter adds the typed tag as a chip (it never submits the form);
  a chip's button (or Enter/Space on it) removes it; Backspace in the empty
  entry removes the last tag. Tags keep their order. The chips are one row,
  scrolled sideways when they are wider than the form.
- Above the submit button: "Add more options" (window, deadline, type,
  dependencies, repeats) and "Use default values", which fills the name (if
  empty), duration, priority and points from the chosen category's defaults
  (the general ones when no category is chosen).
- DependencyPicker: checkboxes labelled by task name and date, kept by id.
- Repeats (docs/recurrence.md): does not repeat / daily / weekly (weekday
  checkboxes) / monthly (day of month), every N, ending never, on a date or
  after N occurrences. The page's date is the series' start. An edited
  series or occurrence shows what it is (recurrence_note); for an occurrence
  the repeat controls are off -- the page asks, when saving, whether the
  change applies to that occurrence, it and every later one, or the series.

A fixed block (label, category, start, end on the page's date) and a
flexible task are different forms; editing never switches between them.
"""

from __future__ import annotations

import tkinter as tk
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta

import customtkinter as ctk

from app.ui.paint_widgets import AppScrollableFrame

from app.ui import theme
from app.ui.clock_input import ClockInput
from app.ui.components import (
    AppButton,
    Card,
    LabeledEntry,
    LabeledSelect,
    Notice,
    SectionTitle,
    Tooltip,
    font,
    make_keyboard_accessible,
)
from app.planning.models import DEFAULT_TASK_POINTS, MAX_TASK_POINTS, ProjectTaskDefaults
from app.ui.task_defaults import TaskDefaultsStore, resolve_task_default
from app.ui.task_form_model import (
    CATEGORIES,
    PRIORITIES,
    WEEKDAY_NAMES,
    Choice,
    EditorOptions,
    TaskDraft,
    parse_points,
)
from app.ui.time_fields import (
    FieldError,
    format_date,
    format_duration,
    parse_date,
    parse_duration,
)

NO_PROJECT = "None"
#: The category choice that is not a category: the task is saved as UNSET_CATEGORY and uses the general defaults.
NO_CATEGORY = "(none)"
UNSET_CATEGORY = "other"
POINTS_STEP = 10
#: The task-type choices that are not a stored type: keep the task's own type / create one from the name below.
OWN_TYPE = "(its own type)"
NEW_TYPE = "New type..."
#: "Repeats" labels and the draft values they stand for.
REPEAT_LABELS = {"Does not repeat": "", "Daily": "daily", "Weekly": "weekly", "Monthly": "monthly"}
END_LABELS = {"Never": "never", "On a date": "on", "After a number of times": "after"}


def _shift_held(event) -> bool:
    return bool(event is not None and getattr(event, "state", 0) & 0x0001)


class _SteppedField(LabeledEntry):
    """A LabeledEntry whose value can also be stepped with keys, the mouse wheel and two small buttons."""

    def __init__(self, parent, label: str, *, placeholder: str = "", hint: str = "", big_step: int = 15) -> None:
        super().__init__(parent, label, placeholder=placeholder, hint=hint, wraplength=130)
        self._big_step = big_step
        self.entry.grid_configure(row=1, column=0)
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=1, column=1, sticky="ns", padx=(4, 0))
        self.down_button = AppButton(buttons, "▼", lambda: self.step(-1), variant="secondary", width=30, height=18,
                                     font=font(9, "bold"))
        self.up_button = AppButton(buttons, "▲", lambda: self.step(1), variant="secondary", width=30, height=18,
                                   font=font(9, "bold"))
        self.up_button.grid(row=0, column=0, pady=(0, 1))
        self.down_button.grid(row=1, column=0, pady=(1, 0))
        Tooltip(self.up_button, f"Later / more (Shift: {big_step})")
        Tooltip(self.down_button, f"Earlier / less (Shift: {big_step})")
        self.entry.bind("<Up>", lambda e: self._key_step(e, 1), add="+")
        self.entry.bind("<Down>", lambda e: self._key_step(e, -1), add="+")
        self.entry.bind("<MouseWheel>", lambda e: self._key_step(e, 1 if e.delta > 0 else -1), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self.normalize(), add="+")
        self.entry.bind("<Return>", lambda _e: (self.normalize(), "break")[1], add="+")

    def _key_step(self, event, direction: int) -> str:
        self.step(direction * (self._big_step if _shift_held(event) else 1))
        return "break"

    # subclasses: value() -> int | None (raises FieldError), show(int), start value, step(delta)
    def normalize(self) -> None:
        """Rewrite what was typed in the display format (or show why it cannot be read)."""
        if not self.get().strip():
            self.set_error(None)
            return
        try:
            self.show(self.value())
            self.set_error(None)
        except FieldError as error:
            self.set_error(str(error))


class DurationField(_SteppedField):
    def __init__(self, parent, label: str = "Duration") -> None:
        super().__init__(parent, label, placeholder="e.g. 45 min, 1 h 13 min")

    def value(self) -> int:
        return parse_duration(self.get())

    def show(self, minutes: int) -> None:
        self.variable.set(format_duration(minutes))

    def step(self, delta: int) -> None:
        try:
            current = self.value()
        except FieldError:
            current = 30 - delta
        self.show(min(1440, max(1, current + delta)))
        self.set_error(None)


class DateField(_SteppedField):
    #: The expected format, for a hint under the field (the Projects page shows it).
    FORMAT_HINT = "Format: YYYY-MM-DD, e.g. 2026-09-23."

    def __init__(self, parent, label: str = "Date", *, optional: bool = False, hint: str | None = None) -> None:
        super().__init__(parent, label, placeholder="YYYY-MM-DD",
                         hint=hint if hint is not None else "Empty: any date." if optional else "", big_step=7)

    def value(self):
        return parse_date(self.get())

    def show(self, value) -> None:
        self.variable.set(format_date(value))

    def step(self, delta: int) -> None:
        try:
            self.show(self.value() + timedelta(days=delta))
            self.set_error(None)
        except FieldError:
            pass


class PointsField(LabeledEntry):
    """Points, typed or stepped by POINTS_STEP with the - / + buttons (kept within 0..MAX_TASK_POINTS)."""

    def __init__(self, parent) -> None:
        super().__init__(parent, "Points", placeholder="e.g. 20",
                         hint=f"What finishing it is worth to you (0-{MAX_TASK_POINTS}).", wraplength=200)
        self.minus_button = AppButton(self, "−", lambda: self.step(-POINTS_STEP), variant="secondary", width=40)
        self.plus_button = AppButton(self, "+", lambda: self.step(POINTS_STEP), variant="secondary", width=40)
        self.minus_button.grid(row=1, column=1, padx=(6, 0))
        self.plus_button.grid(row=1, column=2, padx=(4, 0))
        Tooltip(self.minus_button, f"{POINTS_STEP} points less")
        Tooltip(self.plus_button, f"{POINTS_STEP} points more")

    def step(self, delta: int) -> None:
        try:
            current = parse_points(self.get())
        except FieldError:
            current = 0
        self.variable.set(str(min(MAX_TASK_POINTS, max(0, current + delta))))
        self.set_error(None)


class TagInput(ctk.CTkFrame):
    """Ordered tags as removable chips in one row; Enter adds the typed tag without submitting the form."""

    def __init__(self, parent, on_change: Callable[[], None] | None = None) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.tags: list[str] = []
        self._on_change = on_change
        self.field = LabeledEntry(self, "Tags", placeholder="Type a tag, then Enter")
        self.field.grid(row=0, column=0, sticky="ew")
        self.field.entry.bind("<Return>", self._add_typed, add="+")
        self.field.entry.bind("<KP_Enter>", self._add_typed, add="+")
        self.field.entry.bind("<BackSpace>", self._backspace, add="+")
        # One chip high; the scrollbar is shown only while the chips are wider than the form.
        self.chips = AppScrollableFrame(self, orientation="horizontal", height=32, fg_color="transparent")
        self.overflowing = False
        self.chips.bind("<Configure>", self._show_scrollbar, add="+")
        self.chips._parent_canvas.bind("<Configure>", self._show_scrollbar, add="+")
        self.chip_buttons: dict[str, AppButton] = {}

    def _show_scrollbar(self, _event=None) -> None:
        self.overflowing = self.chips.winfo_reqwidth() > self.chips._parent_canvas.winfo_width()
        if self.overflowing:
            self.chips._scrollbar.grid()
        else:
            self.chips._scrollbar.grid_remove()

    def _add_typed(self, _event=None) -> str:
        self.add(self.field.get())
        return "break"  # Enter in the tag field never submits the task

    def _backspace(self, _event=None) -> str | None:
        if not self.field.get() and self.tags:
            self.remove(self.tags[-1])
            return "break"
        return None

    def add(self, text: str) -> None:
        try:
            draft = TaskDraft(tags=tuple(self.tags)).with_tag(text)
        except FieldError as error:
            self.field.set_error(str(error))
            return
        self.field.set_error(None)
        self.field.variable.set("")
        self.set_tags(list(draft.tags))

    def remove(self, tag: str) -> None:
        self.set_tags([existing for existing in self.tags if existing != tag])
        self.field.entry.focus_set()

    def set_tags(self, tags: list[str]) -> None:
        self.tags = list(tags)
        for button in self.chip_buttons.values():
            button.destroy()
        self.chip_buttons = {}
        for index, tag in enumerate(self.tags):
            chip = AppButton(self.chips, f"{tag}  ✕", lambda tag=tag: self.remove(tag), variant="ghost", height=28,
                             font=font(theme.SIZE_SMALL))
            chip.grid(row=0, column=index, sticky="w", padx=(0, 6), pady=2)
            Tooltip(chip, f"Remove the tag “{tag}”")
            self.chip_buttons[tag] = chip
        if self.tags:
            self.chips.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        else:
            self.chips.grid_remove()
        if self._on_change is not None:
            self._on_change()


class DependencyPicker(ctk.CTkFrame):
    """Choose dependencies by name; the selection is kept by task id."""

    def __init__(self, parent) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text="Depends on", text_color=theme.TEXT_MUTED, font=font(theme.SIZE_SMALL, "bold"),
                     anchor="w").grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.list = AppScrollableFrame(self, height=110, fg_color=theme.INPUT_BG, corner_radius=theme.RADIUS_CONTROL)
        self.list.grid(row=1, column=0, sticky="ew")
        self.list.columnconfigure(0, weight=1)
        self.empty = ctk.CTkLabel(self.list, text="No other tasks yet.", text_color=theme.TEXT_MUTED,
                                  font=font(theme.SIZE_SMALL), anchor="w")
        self.choices: list[Choice] = []
        self.vars: dict[uuid.UUID, tk.BooleanVar] = {}
        self.boxes: dict[uuid.UUID, ctk.CTkCheckBox] = {}

    def set_choices(self, choices: list[Choice], selected: tuple[uuid.UUID, ...] | None = None) -> None:
        keep = set(selected if selected is not None else self.selected())
        if [(c.id, c.label) for c in choices] != [(c.id, c.label) for c in self.choices]:
            for box in self.boxes.values():
                box.destroy()
            self.vars, self.boxes = {}, {}
            for row, choice in enumerate(choices):
                var = tk.BooleanVar(value=False)
                box = ctk.CTkCheckBox(self.list, text=choice.label, variable=var, text_color=theme.TEXT_PRIMARY,
                                      fg_color=theme.ACCENT)
                box.grid(row=row, column=0, sticky="w", padx=8, pady=2)
                make_keyboard_accessible(box, activate=box.toggle, ring=False)
                self.vars[choice.id], self.boxes[choice.id] = var, box
            self.choices = list(choices)
        if choices:
            self.empty.grid_remove()
        else:
            self.empty.grid(row=0, column=0, sticky="w", padx=8, pady=4)
        for choice_id, var in self.vars.items():
            selected_now = choice_id in keep
            if var.get() != selected_now:
                var.set(selected_now)
        self._missing = [choice_id for choice_id in keep if choice_id not in self.vars]

    def selected(self) -> tuple[uuid.UUID, ...]:
        chosen = tuple(choice.id for choice in self.choices if self.vars[choice.id].get())
        # A stored dependency that is not offered (e.g. deleted since) is kept, never silently dropped.
        return chosen + tuple(getattr(self, "_missing", []))


class TaskEditor(Card):
    """Add or edit one flexible task or fixed block; `on_submit(draft)` saves it, `on_cancel()` leaves edit mode."""

    FIELD_NAMES = ("name", "category", "duration", "priority", "points", "window_start", "window_end",
                   "deadline_date", "deadline_time", "start", "end", "repeat_interval", "repeat_day_of_month",
                   "repeat_until", "repeat_count")
    REPEAT_FIELDS = ("repeat", "repeat_interval", "repeat_day_of_month", "repeat_until", "repeat_count", "repeat_end")

    def __init__(self, parent, *, on_submit: Callable[[TaskDraft], None], on_cancel: Callable[[], None],
                 task_defaults: TaskDefaultsStore | None = None, date_selector: bool = False) -> None:
        super().__init__(parent)
        #: The Projects page's form: a date to choose where the project choice is (the page supplies the
        #: project), and flexible tasks only -- a fixed block belongs to no project.
        self.date_selector = date_selector
        #: The open project's configured task defaults (the Projects page sets it): they outrank the category's.
        self.project_defaults: ProjectTaskDefaults | None = None
        #: The default values and added categories (Settings); in memory only when the page gave none.
        self.task_defaults = task_defaults or TaskDefaultsStore()
        #: "Pinned to its date" of the loaded draft (an edited record): not shown, kept as stored.
        self._pin_to_date = False
        self.columnconfigure(0, weight=1)
        self._on_submit, self._on_cancel = on_submit, on_cancel
        self.kind = "task"
        self.editing = False
        #: The ISO date the task/block belongs to: the page's selected date, or an edited record's own ("" = any date).
        self.date_text = ""
        self.options = EditorOptions(timezone="UTC")
        self._project_ids: dict[str, uuid.UUID | None] = {NO_PROJECT: None}
        self._type_ids: dict[str, uuid.UUID | None] = {OWN_TYPE: None}

        self.title = SectionTitle(self, "Add a task", "Flexible tasks are placed by the scheduler; fixed blocks stay "
                                                       "where you put them.", wraplength=280)
        self.title.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 8))
        switch = ctk.CTkFrame(self, fg_color="transparent")
        switch.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        switch.columnconfigure((0, 1), weight=1, uniform="kind")
        self.kind_buttons = {
            "task": AppButton(switch, "Flexible task", lambda: self.set_kind("task"), variant="secondary", height=32),
            "block": AppButton(switch, "Fixed block", lambda: self.set_kind("block"), variant="secondary", height=32),
        }
        self.kind_buttons["task"].grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.kind_buttons["block"].grid(row=0, column=1, sticky="ew", padx=(4, 0))

        body = self.body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        body.columnconfigure((0, 1), weight=1, uniform="editor")
        self.name_field = LabeledEntry(body, "Name", placeholder="e.g. Study math")
        self.name_field.entry.bind("<Return>", lambda _e: (self.submit(), "break")[1], add="+")
        self.category_select = LabeledSelect(body, "Category", [NO_CATEGORY, *CATEGORIES])
        self.project_select = LabeledSelect(body, "Project", [NO_PROJECT])
        self.date_field = DateField(body, "Date", hint=DateField.FORMAT_HINT)
        if date_selector:
            switch.grid_remove()
        self.name_field.grid(row=0, column=0, columnspan=2, sticky="ew", pady=4)

        # Flexible task fields
        self.task_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.task_frame.columnconfigure((0, 1), weight=1, uniform="task")
        self.duration_field = DurationField(self.task_frame)
        self.priority_select = LabeledSelect(self.task_frame, "Priority (1-10)", PRIORITIES)
        self.priority_select.variable.set("5")
        self.duration_field.grid(row=0, column=0, sticky="new", padx=(0, 6), pady=4)
        self.priority_select.grid(row=0, column=1, sticky="new", padx=(6, 0), pady=4)
        self.points_field = PointsField(self.task_frame)
        self.points_field.variable.set(str(DEFAULT_TASK_POINTS))
        self.points_field.grid(row=1, column=0, columnspan=2, sticky="new", pady=4)
        self.required_var = tk.BooleanVar(value=False)
        self.required_check = ctk.CTkCheckBox(self.task_frame, text="Required (must be scheduled)",
                                              variable=self.required_var, text_color=theme.TEXT_PRIMARY,
                                              fg_color=theme.ACCENT)
        self.required_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
        make_keyboard_accessible(self.required_check, activate=self.required_check.toggle, ring=False)
        self.tag_input = TagInput(self.task_frame)
        self.tag_input.grid(row=4, column=0, columnspan=2, sticky="ew", pady=4)
        self.more_frame = ctk.CTkFrame(self.task_frame, fg_color="transparent")
        self.more_frame.columnconfigure((0, 1), weight=1, uniform="more")
        # One time input per row: [hh]:[mm][AM/PM] stays whole even in the narrow form column.
        self.window_start = ClockInput(self.more_frame, "Preferred from")
        self.window_end = ClockInput(self.more_frame, "Preferred until", end_of_interval=True)
        self.window_start.grid(row=0, column=0, columnspan=2, sticky="new", pady=4)
        self.window_end.grid(row=1, column=0, columnspan=2, sticky="new", pady=4)
        self.deadline_date = DateField(self.more_frame, "Deadline date", optional=True)
        self.deadline_time = ClockInput(self.more_frame, "Deadline time")
        self.deadline_date.grid(row=2, column=0, columnspan=2, sticky="new", pady=4)
        self.deadline_time.grid(row=3, column=0, columnspan=2, sticky="new", pady=4)
        # The reusable task type: independent of category and tags; several tasks may share one.
        self.type_select = LabeledSelect(self.more_frame, "Task type", [OWN_TYPE, NEW_TYPE],
                                         command=lambda _value: self._show_new_type())
        self.type_select.grid(row=5, column=0, columnspan=2, sticky="ew", pady=4)
        self.new_type_field = LabeledEntry(self.more_frame, "New type name", placeholder="e.g. Reading")
        self.dependency_picker = DependencyPicker(self.more_frame)
        self.dependency_picker.grid(row=7, column=0, columnspan=2, sticky="ew", pady=4)
        self._build_repeat(self.more_frame)
        self.more_open = False
        #: What the record being edited is ("task", "series", "occurrence"); a new record is a "task".
        self.recurrence_role = "task"
        self.needs_configuration = False
        self.recurrence_note = ctk.CTkLabel(self.task_frame, text="", text_color=theme.TEXT_MUTED, anchor="w",
                                            justify="left", font=font(theme.SIZE_CAPTION), wraplength=280)

        # Fixed block fields
        self.block_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.block_frame.columnconfigure((0, 1), weight=1, uniform="block")
        self.start_field = ClockInput(self.block_frame, "Start")
        self.end_field = ClockInput(self.block_frame, "End", end_of_interval=True)
        self.start_field.grid(row=0, column=0, columnspan=2, sticky="new", pady=4)
        self.end_field.grid(row=1, column=0, columnspan=2, sticky="new", pady=4)

        self.timezone_label = ctk.CTkLabel(body, text="", text_color=theme.TEXT_MUTED, font=font(theme.SIZE_CAPTION),
                                           anchor="w")
        self.timezone_label.grid(row=4, column=0, columnspan=2, sticky="ew")

        self.notice = Notice(self, wraplength=280)
        self.notice.grid(row=3, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.notice.hide()
        bar = self.options_bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=theme.SPACE_L, pady=(10, 0))
        bar.columnconfigure((0, 1), weight=1, uniform="options")
        self.more_button = AppButton(bar, "Add more options ▸", self.toggle_more, variant="ghost", height=32,
                                     font=font(theme.SIZE_SMALL, "bold"))
        self.more_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.defaults_button = AppButton(bar, "Use default values", self.use_defaults, variant="secondary", height=32,
                                         font=font(theme.SIZE_SMALL, "bold"))
        self.defaults_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        Tooltip(self.defaults_button, "Fill in the defaults of the chosen category (set in Settings)")
        self.submit_button = AppButton(self, "+ Add task", self.submit, height=44)
        self.submit_button.grid(row=5, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, 8))
        self.cancel_edit_button = AppButton(self, "Cancel edit", lambda: self._on_cancel(), variant="secondary",
                                            height=34)
        ctk.CTkFrame(self, fg_color="transparent", height=10).grid(row=7, column=0)
        self.set_kind("task")

    def _build_repeat(self, parent) -> None:
        """The "Repeats" controls (a series; docs/recurrence.md)."""
        frame = self.repeat_frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        frame.columnconfigure((0, 1), weight=1, uniform="repeat")
        self.repeat_select = LabeledSelect(frame, "Repeats", list(REPEAT_LABELS),
                                           command=lambda _value: self._show_repeat())
        self.repeat_select.grid(row=0, column=0, columnspan=2, sticky="ew", pady=4)
        self.repeat_interval = LabeledEntry(frame, "Every", placeholder="1", hint="days / weeks / months",
                                            wraplength=130)
        self.repeat_interval.variable.set("1")
        self.repeat_day_of_month = LabeledEntry(frame, "Day of month", placeholder="start date's",
                                                hint="Months without it are skipped.", wraplength=130)
        self.weekday_frame = ctk.CTkFrame(frame, fg_color="transparent")
        self.weekday_vars = [tk.BooleanVar(value=False) for _ in WEEKDAY_NAMES]
        for index, (name, variable) in enumerate(zip(WEEKDAY_NAMES, self.weekday_vars)):
            box = ctk.CTkCheckBox(self.weekday_frame, text=name, variable=variable, width=60,
                                  text_color=theme.TEXT_PRIMARY, fg_color=theme.ACCENT)
            box.grid(row=index // 4, column=index % 4, sticky="w", pady=2)
            make_keyboard_accessible(box, activate=box.toggle, ring=False)
        self.repeat_end = LabeledSelect(frame, "Ends", list(END_LABELS), command=lambda _value: self._show_repeat())
        self.repeat_until = DateField(frame, "Last date", optional=True)
        self.repeat_count = LabeledEntry(frame, "Occurrences", placeholder="e.g. 10", wraplength=130)

    def _show_repeat(self) -> None:
        """Show only the repeat controls that apply to the chosen frequency and end."""
        repeat = REPEAT_LABELS.get(self.repeat_select.get(), "")
        end = END_LABELS.get(self.repeat_end.get(), "never")
        for widget in (self.repeat_interval, self.repeat_day_of_month, self.weekday_frame, self.repeat_end,
                       self.repeat_until, self.repeat_count):
            widget.grid_remove()
        if not repeat:
            return
        self.repeat_interval.grid(row=1, column=0, sticky="new", padx=(0, 6), pady=4)
        if repeat == "monthly":
            self.repeat_day_of_month.grid(row=1, column=1, sticky="new", padx=(6, 0), pady=4)
        if repeat == "weekly":
            self.weekday_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)
        self.repeat_end.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        if end == "on":
            self.repeat_until.grid(row=4, column=0, columnspan=2, sticky="new", pady=4)
        elif end == "after":
            self.repeat_count.grid(row=4, column=0, columnspan=2, sticky="new", pady=4)

    # ------------------------------------------------------------------ fields

    @property
    def fields(self) -> dict[str, LabeledEntry | ClockInput]:
        return {**({"date": self.date_field} if self.date_selector else {}),
                "name": self.name_field, "duration": self.duration_field, "points": self.points_field,
                "window_start": self.window_start, "window_end": self.window_end,
                "deadline_date": self.deadline_date, "deadline_time": self.deadline_time,
                "start": self.start_field, "end": self.end_field, "repeat_interval": self.repeat_interval,
                "repeat_day_of_month": self.repeat_day_of_month, "repeat_until": self.repeat_until,
                "repeat_count": self.repeat_count}

    def set_kind(self, kind: str) -> None:
        if self.editing and kind != self.kind:
            return  # a record keeps its kind
        self.kind = kind
        for key, button in self.kind_buttons.items():
            active = key == kind
            button.configure(text=("✓ " if active else "") + ("Flexible task" if key == "task" else "Fixed block"),
                             fg_color=theme.ACCENT if active else theme.SECONDARY_BG,
                             text_color=theme.TEXT_ON_ACCENT if active else theme.TEXT_PRIMARY,
                             state="disabled" if self.editing and not active else "normal")
        if kind == "task":
            self.block_frame.grid_remove()
            self.task_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
            self.name_field.label.configure(text="Name")
            self.category_select.grid(row=1, column=0, columnspan=1, sticky="new", padx=(0, 6), pady=4)
            (self.date_field if self.date_selector else self.project_select).grid(
                row=1, column=1, sticky="new", padx=(6, 0), pady=4)
        else:
            self.task_frame.grid_remove()
            self.block_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
            self.name_field.label.configure(text="Label")
            self.project_select.grid_remove()  # a fixed block has no project
            self.category_select.grid(row=1, column=0, columnspan=2, sticky="new", padx=0, pady=4)
        self.more_button.configure(state="normal" if kind == "task" else "disabled")
        self._title()

    def set_date(self, day: date | str | None) -> None:
        """The date a new task/block gets (the page's selected date); an edited record's own date while editing."""
        self.date_text = day.isoformat() if isinstance(day, date) else (day or "").strip()
        if self.date_selector:
            self.date_field.variable.set(self.date_text)

    def toggle_more(self) -> None:
        self.more_open = not self.more_open
        if self.more_open:
            self.more_frame.grid(row=6, column=0, columnspan=2, sticky="ew")
        else:
            self.more_frame.grid_remove()
        self.more_button.configure(text="Fewer options ▾" if self.more_open else "Add more options ▸")

    def _category(self) -> str:
        """The chosen category ("" while none is chosen)."""
        chosen = self.category_select.get()
        return "" if chosen == NO_CATEGORY else chosen

    def use_defaults(self) -> None:
        """Fill the chosen category's default values (the general ones without a category); a typed name stays."""
        default = resolve_task_default(self.task_defaults.value.for_category(self._category()),
                                       self.project_defaults)
        if not self.name_field.get().strip():
            self.name_field.variable.set(default.name)
        if self.kind == "task":
            self.duration_field.show(default.duration)
            self.priority_select.variable.set(str(default.priority))
            self.points_field.variable.set(str(default.points))
        self.clear_errors()

    def set_options(self, options: EditorOptions) -> None:
        self.options = options
        added = [name for name in self.task_defaults.value.custom_categories if name not in options.categories]
        self.category_select.set_values([NO_CATEGORY, *options.categories, *added])
        self._project_ids = {NO_PROJECT: None}
        for choice in options.projects:
            label = choice.label
            # Imported names and generated labels may collide, including with the
            # unassigned option. Keep every ID selectable without replacing it.
            while label in self._project_ids:
                label += f" ({choice.id})"
            self._project_ids[label] = choice.id
        self.project_select.set_values(list(self._project_ids))
        self._type_ids = {OWN_TYPE: None}
        for choice in options.task_types:
            label = choice.label
            while label in self._type_ids or label == NEW_TYPE:
                label += f" ({choice.id})"
            self._type_ids[label] = choice.id
        self.type_select.set_values([*self._type_ids, NEW_TYPE])
        self.dependency_picker.set_choices(options.dependencies)
        self.timezone_label.configure(text=f"Times are in {options.timezone}.")

    def _show_new_type(self) -> None:
        """The name field appears only while "New type..." is chosen."""
        if self.type_select.get() == NEW_TYPE:
            self.new_type_field.grid(row=6, column=0, columnspan=2, sticky="ew", pady=4)
        else:
            self.new_type_field.grid_remove()

    def set_dependencies(self, dependency_ids: list[uuid.UUID]) -> None:
        self.dependency_picker.set_choices(self.options.dependencies, tuple(dependency_ids))
        if dependency_ids and not self.more_open:
            self.toggle_more()

    # ------------------------------------------------------------------ draft

    def draft(self) -> TaskDraft:
        project_label = self.project_select.get()
        type_label = self.type_select.get()
        return TaskDraft(
            task_type_id=self._type_ids.get(type_label),
            new_type_label=self.new_type_field.get().strip() if type_label == NEW_TYPE else "",
            kind=self.kind, name=self.name_field.get(), category=self._category() or UNSET_CATEGORY,
            date=self.date_field.get().strip() if self.date_selector else self.date_text,
            duration=self.duration_field.get(), priority=self.priority_select.get(), points=self.points_field.get(),
            required=self.required_var.get(),
            pin_to_date=self._pin_to_date, window_start=self.window_start.get(), window_end=self.window_end.get(),
            deadline_date=self.deadline_date.get(), deadline_time=self.deadline_time.get(),
            dependency_ids=self.dependency_picker.selected(), project_id=self._project_ids.get(project_label),
            tags=tuple(self.tag_input.tags), start=self.start_field.get(), end=self.end_field.get(),
            repeat=REPEAT_LABELS.get(self.repeat_select.get(), ""), repeat_interval=self.repeat_interval.get(),
            repeat_weekdays=tuple(day for day, variable in enumerate(self.weekday_vars) if variable.get()),
            repeat_day_of_month=self.repeat_day_of_month.get(),
            repeat_end=END_LABELS.get(self.repeat_end.get(), "never"), repeat_until=self.repeat_until.get(),
            repeat_count=self.repeat_count.get(), recurrence_role=self.recurrence_role,
            needs_configuration=self.needs_configuration,
        )

    def load(self, draft: TaskDraft, *, editing: bool) -> None:
        """Fill the form from a draft (editing a stored record, or a blank one for adding)."""
        self.editing = False  # allow the kind to change while loading
        self.set_kind(draft.kind)
        self.editing = editing
        self.set_kind(draft.kind)
        self.name_field.variable.set(draft.name)
        category = draft.category or NO_CATEGORY
        if category not in self.category_select.values:
            self.category_select.set_values([*self.category_select.values, category])
        self.category_select.variable.set(category)
        # Editing shows the record's own date; a new blank draft without one keeps the page's selected day.
        self.set_date(draft.date if editing or draft.date else self.date_text)
        self.duration_field.variable.set(draft.duration)
        self.priority_select.variable.set(draft.priority if draft.priority in PRIORITIES else "5")
        self.points_field.variable.set(draft.points)
        self.required_var.set(draft.required)
        self._pin_to_date = draft.pin_to_date
        self.window_start.variable.set(draft.window_start)
        self.window_end.variable.set(draft.window_end)
        self.deadline_date.variable.set(draft.deadline_date)
        self.deadline_time.variable.set(draft.deadline_time)
        project = next((label for label, value in self._project_ids.items() if value == draft.project_id), NO_PROJECT)
        self.project_select.variable.set(project)
        chosen_type = next((label for label, value in self._type_ids.items()
                            if value is not None and value == draft.task_type_id), OWN_TYPE)
        self.type_select.variable.set(chosen_type)
        self.new_type_field.variable.set("")
        self._show_new_type()
        self.dependency_picker.set_choices(self.options.dependencies, draft.dependency_ids)
        self.tag_input.set_tags(list(draft.tags))
        self.start_field.variable.set(draft.start)
        self.end_field.variable.set(draft.end)
        self.recurrence_role = draft.recurrence_role if editing else "task"
        self.needs_configuration = draft.needs_configuration and editing
        self.repeat_select.variable.set(next(label for label, value in REPEAT_LABELS.items() if value == draft.repeat))
        self.repeat_interval.variable.set(draft.repeat_interval or "1")
        for day, variable in enumerate(self.weekday_vars):
            variable.set(day in draft.repeat_weekdays)
        self.repeat_day_of_month.variable.set(draft.repeat_day_of_month)
        self.repeat_end.variable.set(next(label for label, value in END_LABELS.items() if value == draft.repeat_end))
        self.repeat_until.variable.set(draft.repeat_until)
        self.repeat_count.variable.set(draft.repeat_count)
        self._show_repeat()
        if self.recurrence_role == "occurrence":
            self.repeat_frame.grid_remove()  # one occurrence does not repeat; its series does
        else:
            self.repeat_frame.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        note = draft.recurrence_note if editing else ""
        self.recurrence_note.configure(text=note)
        if note:
            self.recurrence_note.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        else:
            self.recurrence_note.grid_remove()
        self.clear_errors()
        has_more = any((draft.window_start, draft.deadline_date, draft.dependency_ids, draft.repeat))
        if has_more != self.more_open and draft.kind == "task":
            self.toggle_more()
        if not editing and self.project_defaults is not None:
            # A new task of a project starts from what the project explicitly configured (nothing else is filled:
            # an unset value leaves the form as every other new task's, and typing over any of them wins).
            configured = self.project_defaults
            if configured.duration_minutes is not None and not draft.duration.strip():
                self.duration_field.show(configured.duration_minutes)
            if configured.priority is not None:
                self.priority_select.variable.set(str(configured.priority))
            if configured.points is not None:
                self.points_field.variable.set(str(configured.points))
        if editing:
            self.cancel_edit_button.grid(row=6, column=0, sticky="ew", padx=theme.SPACE_L, pady=(0, 8))
        else:
            self.cancel_edit_button.grid_remove()
        self._title()

    def _title(self) -> None:
        noun = "task" if self.kind == "task" else "fixed block"
        self.title.title_label.configure(text=f"Edit {noun}" if self.editing else f"Add a {noun}")
        self.submit_button.configure(text="Save changes" if self.editing else f"+ Add {noun}")

    def clear_errors(self) -> None:
        for field in self.fields.values():
            field.set_error(None)
        self.notice.hide()

    def show_errors(self, message: str, errors: dict[str, str] | None = None) -> None:
        """Show a refusal: next to each named field and as a notice; everything typed stays."""
        self.clear_errors()
        for name, text in (errors or {}).items():
            if name in self.fields:
                self.fields[name].set_error(text)
                if (name in ("window_start", "window_end", "deadline_date", "deadline_time", *self.REPEAT_FIELDS)
                        and not self.more_open):
                    self.toggle_more()
        self.notice.show("error", message)

    def submit(self) -> None:
        self._on_submit(self.draft())

    def reset_for_next(self, date_text: str) -> None:
        """After a successful add: a blank form of the same kind, on the same date."""
        self.load(replace(TaskDraft(kind=self.kind), date=date_text, category=self._category()),
                  editing=False)
