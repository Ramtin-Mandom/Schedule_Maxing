"""
app/ui/task_editor.py

The reusable task form (Milestone 4, Prompt 3) that Day, Week and Month --
and later Projects -- share. Widgets only: it collects a TaskDraft
(app/ui/task_form_model.py) and hands it to its page, which saves it through
the presenter and the services; field errors come back as FormErrors and are
shown next to their fields, with the typed values kept.

Controls:

- Task type: Flexible | Fixed | To Do. Choosing one shows only that kind's
  fields at once; a draft is built from the shown kind's fields alone, so a
  value typed for another kind is never saved (app/ui/task_form_model.py).
  Flexible: name, category, project, points, duration, preferred time
  (Early / Mid / Late), required, tags. Fixed: label, category, points,
  start, end. To Do: name, category, points, tags. There are no advanced
  settings: no deadline, task type, dependency or repeat controls. What a
  stored record has of those is kept as it is when the record is edited.
- Times (fixed-block start/end) use the
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
- TagInput: Enter adds the typed tag as a chip (it never submits the form);
  a chip's button (or Enter/Space on it) removes it; Backspace in the empty
  entry removes the last tag. Tags keep their order. The chips are one row,
  scrolled sideways when they are wider than the form.
- Above the submit button: "Use default values", which fills the name (if
  empty), duration and points from the chosen category's defaults (the
  general ones when no category is chosen).
- While a stored record is being edited: "Remove task" (the page removes it
  through its services) and "Cancel edit".
- An edited series or occurrence shows what it is (recurrence_note); for an
  occurrence the page asks, when saving, whether the change applies to that
  occurrence, it and every later one, or the series.

The three kinds are different forms; editing never switches between them.
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
    DEFAULT_PREFERRED_TIME,
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
#: The task-type switch: draft kind -> its button text, and what the kind is called in titles.
KIND_LABELS = {"task": "Flexible", "block": "Fixed", "todo": "To Do"}
KIND_NOUNS = {"task": "task", "block": "fixed block", "todo": "To Do"}
#: "Preferred time" labels and the draft values they stand for (a third of the day's schedulable window).
PREFERRED_LABELS = {"Early": "early", "Mid": "mid", "Late": "late"}


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


class TaskEditor(Card):
    """
    Add or edit one flexible task, fixed block or To Do; `on_submit(draft)` saves it, `on_cancel()` leaves edit
    mode and `on_remove()` (when given) removes the record being edited.
    """

    FIELD_NAMES = ("name", "category", "duration", "points", "start", "end")

    def __init__(self, parent, *, on_submit: Callable[[TaskDraft], None], on_cancel: Callable[[], None],
                 task_defaults: TaskDefaultsStore | None = None, date_selector: bool = False,
                 on_remove: Callable[[], None] | None = None) -> None:
        super().__init__(parent)
        self._on_remove = on_remove
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
        #: The draft the form was last loaded from: what it does not show (see draft()) is carried from it.
        self._loaded = TaskDraft()

        self.title = SectionTitle(self, "Add a task", "Flexible tasks are placed by the scheduler; fixed blocks stay "
                                                       "where you put them; To Dos are a checklist.", wraplength=280)
        self.title.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 8))
        switch = ctk.CTkFrame(self, fg_color="transparent")
        switch.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        switch.columnconfigure((0, 1, 2), weight=1, uniform="kind")
        self.kind_buttons = {
            kind: AppButton(switch, label, lambda kind=kind: self.set_kind(kind), variant="secondary", height=32)
            for kind, label in KIND_LABELS.items()
        }
        for column, button in enumerate(self.kind_buttons.values()):
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 3, 0 if column == 2 else 3))

        body = self.body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        body.columnconfigure((0, 1), weight=1, uniform="editor")
        self.name_field = LabeledEntry(body, "Name", placeholder="e.g. Study math")
        self.name_field.entry.bind("<Return>", lambda _e: (self.submit(), "break")[1], add="+")
        self.category_select = LabeledSelect(body, "Category", [NO_CATEGORY, *CATEGORIES])
        self.project_select = LabeledSelect(body, "Project", [NO_PROJECT])
        if date_selector:
            # Only the Projects page's form has a date to type; every other form takes its page's date.
            self.date_field = DateField(body, "Date", hint=DateField.FORMAT_HINT)
            switch.grid_remove()
        self.name_field.grid(row=0, column=0, columnspan=2, sticky="ew", pady=4)
        # Every kind has points: what completing it is worth (never a scheduling priority).
        self.points_field = PointsField(body)
        self.points_field.variable.set(str(DEFAULT_TASK_POINTS))
        self.points_field.grid(row=2, column=0, columnspan=2, sticky="new", pady=4)

        # Flexible task fields
        self.task_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.task_frame.columnconfigure((0, 1), weight=1, uniform="task")
        self.duration_field = DurationField(self.task_frame)
        self.preferred_select = LabeledSelect(self.task_frame, "Preferred time", list(PREFERRED_LABELS))
        self._set_preferred(DEFAULT_PREFERRED_TIME)
        Tooltip(self.preferred_select, "The third of the day's schedulable hours the scheduler places it in "
                                       "whenever it fits there")
        self.duration_field.grid(row=0, column=0, sticky="new", padx=(0, 6), pady=4)
        self.preferred_select.grid(row=0, column=1, sticky="new", padx=(6, 0), pady=4)
        self.required_var = tk.BooleanVar(value=False)
        self.required_check = ctk.CTkCheckBox(self.task_frame, text="Required (must be scheduled)",
                                              variable=self.required_var, text_color=theme.TEXT_PRIMARY,
                                              fg_color=theme.ACCENT)
        self.required_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
        make_keyboard_accessible(self.required_check, activate=self.required_check.toggle, ring=False)
        # Tags belong to flexible tasks and To Dos (a fixed block has none).
        self.tag_input = TagInput(body)
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
        self.timezone_label.grid(row=6, column=0, columnspan=2, sticky="ew")

        self.notice = Notice(self, wraplength=280)
        self.notice.grid(row=3, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.notice.hide()
        bar = self.options_bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=theme.SPACE_L, pady=(10, 0))
        bar.columnconfigure(0, weight=1)
        self.defaults_button = AppButton(bar, "Use default values", self.use_defaults, variant="secondary", height=32,
                                         font=font(theme.SIZE_SMALL, "bold"))
        self.defaults_button.grid(row=0, column=0, sticky="ew")
        Tooltip(self.defaults_button, "Fill in the defaults of the chosen category (set in Settings)")
        self.submit_button = AppButton(self, "+ Add task", self.submit, height=44)
        self.submit_button.grid(row=5, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, 8))
        # Shown only while a stored record is being edited.
        self.remove_button = AppButton(self, "Remove task", self.remove, variant="danger", height=34)
        self.cancel_edit_button = AppButton(self, "Cancel edit", lambda: self._on_cancel(), variant="secondary",
                                            height=34)
        ctk.CTkFrame(self, fg_color="transparent", height=10).grid(row=8, column=0)
        self.set_kind("task")

    # ------------------------------------------------------------------ fields

    @property
    def fields(self) -> dict[str, LabeledEntry | ClockInput]:
        return {**({"date": self.date_field} if self.date_selector else {}),
                "name": self.name_field, "duration": self.duration_field, "points": self.points_field,
                "start": self.start_field, "end": self.end_field}

    def set_kind(self, kind: str) -> None:
        if self.editing and kind != self.kind:
            return  # a record keeps its kind
        self.kind = kind
        for key, button in self.kind_buttons.items():
            active = key == kind
            button.configure(text=("✓ " if active else "") + KIND_LABELS[key],
                             fg_color=theme.ACCENT if active else theme.SECONDARY_BG,
                             text_color=theme.TEXT_ON_ACCENT if active else theme.TEXT_PRIMARY,
                             state="disabled" if self.editing and not active else "normal")
        # Only the chosen kind's fields are shown (and only they are read into a saved record).
        self.name_field.label.configure(text="Label" if kind == "block" else "Name")
        if kind == "task":
            self.block_frame.grid_remove()
            self.task_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
            self.category_select.grid(row=1, column=0, columnspan=1, sticky="new", padx=(0, 6), pady=4)
            (self.date_field if self.date_selector else self.project_select).grid(
                row=1, column=1, sticky="new", padx=(6, 0), pady=4)
        else:
            self.task_frame.grid_remove()
            self.project_select.grid_remove()  # a fixed block and a To Do belong to no project
            self.category_select.grid(row=1, column=0, columnspan=2, sticky="new", padx=0, pady=4)
            if kind == "block":
                self.block_frame.grid(row=4, column=0, columnspan=2, sticky="ew")
            else:
                self.block_frame.grid_remove()
        if kind == "block":
            self.tag_input.grid_remove()
        else:
            self.tag_input.grid(row=5, column=0, columnspan=2, sticky="ew", pady=4)
        if kind == "todo":
            self.timezone_label.grid_remove()  # a To Do has no time
        else:
            self.timezone_label.grid()
        self._title()

    def _set_preferred(self, value: str) -> None:
        self.preferred_select.variable.set(next(
            (label for label, stored in PREFERRED_LABELS.items() if stored == value), "Mid"))

    def set_date(self, day: date | str | None) -> None:
        """The date a new task/block gets (the page's selected date); an edited record's own date while editing."""
        self.date_text = day.isoformat() if isinstance(day, date) else (day or "").strip()
        if self.date_selector:
            self.date_field.variable.set(self.date_text)

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
        self.timezone_label.configure(text=f"Times are in {options.timezone}.")

    # ------------------------------------------------------------------ draft

    def draft(self) -> TaskDraft:
        """
        The form as a draft. The form has no deadline, task type, dependency or
        repeat controls: for a record being edited those values are carried
        unchanged from the draft it was loaded from (so saving never clears
        what is stored), and a new record has none.
        """
        base = self._loaded if self.editing else TaskDraft()
        return replace(
            base,
            kind=self.kind, name=self.name_field.get(), category=self._category() or UNSET_CATEGORY,
            date=self.date_field.get().strip() if self.date_selector else self.date_text,
            duration=self.duration_field.get(), points=self.points_field.get(),
            required=self.required_var.get(), pin_to_date=self._pin_to_date,
            preferred_time=PREFERRED_LABELS.get(self.preferred_select.get(), DEFAULT_PREFERRED_TIME),
            project_id=self._project_ids.get(self.project_select.get()),
            tags=tuple(self.tag_input.tags), start=self.start_field.get(), end=self.end_field.get(),
            recurrence_role=self.recurrence_role, needs_configuration=self.needs_configuration,
        )

    def load(self, draft: TaskDraft, *, editing: bool) -> None:
        """Fill the form from a draft (editing a stored record, or a blank one for adding)."""
        self._loaded = draft
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
        self._set_preferred(draft.preferred_time)
        self.points_field.variable.set(draft.points)
        self.required_var.set(draft.required)
        self._pin_to_date = draft.pin_to_date
        project = next((label for label, value in self._project_ids.items() if value == draft.project_id), NO_PROJECT)
        self.project_select.variable.set(project)
        self.tag_input.set_tags(list(draft.tags))
        self.start_field.variable.set(draft.start)
        self.end_field.variable.set(draft.end)
        self.recurrence_role = draft.recurrence_role if editing else "task"
        self.needs_configuration = draft.needs_configuration and editing
        note = draft.recurrence_note if editing else ""
        self.recurrence_note.configure(text=note)
        if note:
            self.recurrence_note.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        else:
            self.recurrence_note.grid_remove()
        self.clear_errors()
        if not editing and self.project_defaults is not None:
            # A new task of a project starts from what the project explicitly configured (nothing else is filled:
            # an unset value leaves the form as every other new task's, and typing over any of them wins).
            configured = self.project_defaults
            if configured.duration_minutes is not None and not draft.duration.strip():
                self.duration_field.show(configured.duration_minutes)
            if configured.points is not None:
                self.points_field.variable.set(str(configured.points))
        if editing and self._on_remove is not None:
            self.remove_button.grid(row=6, column=0, sticky="ew", padx=theme.SPACE_L, pady=(0, 8))
        else:
            self.remove_button.grid_remove()
        if editing:
            self.cancel_edit_button.grid(row=7, column=0, sticky="ew", padx=theme.SPACE_L, pady=(0, 8))
        else:
            self.cancel_edit_button.grid_remove()
        self._title()

    def _title(self) -> None:
        noun = KIND_NOUNS[self.kind]
        self.title.title_label.configure(text=f"Edit {noun}" if self.editing else f"Add a {noun}")
        self.submit_button.configure(text="Save changes" if self.editing else f"+ Add {noun}")
        self.remove_button.configure(text=f"Remove {noun}")

    def remove(self) -> None:
        """Remove the record being edited (the page confirms and removes it through its services)."""
        if self.editing and self._on_remove is not None:
            self._on_remove()

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
        self.notice.show("error", message)

    def submit(self) -> None:
        self._on_submit(self.draft())

    def reset_for_next(self, date_text: str) -> None:
        """After a successful add: a blank form of the same kind, on the same date."""
        self.load(replace(TaskDraft(kind=self.kind), date=date_text, category=self._category()),
                  editing=False)
