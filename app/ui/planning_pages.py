"""
Native project management; services own all writes.

The Projects page has two views. The overview creates a project (name,
description, start and estimated end date) and lists the projects in two
collapsible sections, ongoing and completed; double-clicking one (or its
Open button) opens its detail view. The detail view has three sections --
Add Task (the shared task form, with a date where the Day page's has the
project choice), Project Tasks and Milestones -- the last two scrolling on
their own. Everything is read and written through ProjectsController.
"""
from __future__ import annotations

import uuid
from datetime import date

import tkinter as tk

import customtkinter as ctk

from app.planning.models import MAX_MILESTONE_SCORE, MIN_MILESTONE_SCORE
from app.ui import theme
from app.ui.background import WRITE_LANE, run_in_background
from app.ui.components import (
    AppButton,
    Card,
    ChoiceDialog,
    LabeledEntry,
    ModalDialog,
    Notice,
    SectionTitle,
    ask_confirm,
    font,
    Tooltip,
    make_keyboard_accessible,
)
from app.ui.pages import PageHeader
from app.ui.paint_widgets import AppOptionMenu, AppScrollableFrame, AppTextbox
from app.ui.projects_controller import MilestoneRow, ProjectRow, ProjectsSnapshot, ProjectTaskRow
from app.ui.shell_state import LayoutMode
from app.ui.task_editor import DateField, TaskEditor
from app.ui.task_form_model import FormErrors, TaskDraft
from app.ui.time_fields import FieldError, format_duration

#: A project task's row: green only once the task is finished (also said in words and by a mark).
TASK_DONE_BG: theme.Color = ("#DCFCE7", "#1C4630")
TASK_DONE_TEXT: theme.Color = ("#14532D", "#DCFCE7")
#: Milestone score band (projects_controller.milestone_score_band) -> (background, text), readable in both modes.
SCORE_COLORS: dict[str, tuple[theme.Color, theme.Color]] = {
    "neutral": (theme.SECONDARY_BG, theme.TEXT_PRIMARY),
    "light": (("#BBF7D0", "#2F6B49"), ("#14532D", "#F0FDF4")),
    "green": (("#16803C", "#1F8A4C"), ("#FFFFFF", "#FFFFFF")),
    "dark": (("#14532D", "#0B3D22"), ("#FFFFFF", "#FFFFFF")),
}
#: Under a project's own date fields: the format, and that the date may be left out.
OPTIONAL_DATE_HINT = f"{DateField.FORMAT_HINT} Optional."
SCORES = [str(score) for score in range(MIN_MILESTONE_SCORE, MAX_MILESTONE_SCORE + 1)]


class DateDialog(ModalDialog):
    """Asks for one required date (YYYY-MM-DD); the result is the date, or None when cancelled."""

    def __init__(self, parent, *, title: str, prompt: str, initial: date | None = None) -> None:
        super().__init__(parent, title, width=360)
        ctk.CTkLabel(self.body, text=prompt, anchor="w", justify="left", wraplength=320, font=font(),
                     text_color=theme.TEXT_PRIMARY).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.date_field = DateField(self.body, "Date", hint=DateField.FORMAT_HINT)
        self.date_field.grid(row=1, column=0, sticky="ew")
        if initial is not None:
            self.date_field.show(initial)
        self.initial_focus = self.date_field.entry
        self.add_buttons("Assign date", self.confirm)
        self.present()

    def confirm(self) -> None:
        if not self.date_field.get().strip():
            self.date_field.set_error("Choose a date.")
            return
        try:
            day = self.date_field.value()
        except FieldError as error:
            self.date_field.set_error(str(error))
            return
        self.close(day)


class MilestoneDialog(ModalDialog):
    """Asks for a milestone's number, title and description; the result is the three texts, or None."""

    def __init__(self, parent, *, number: str = "") -> None:
        super().__init__(parent, "Add milestone", width=420)
        self.number = LabeledEntry(self.body, "Milestone number", placeholder="e.g. 1", hint="A whole number; "
                                   "milestones are listed in this order.", wraplength=380)
        self.number.variable.set(number)
        self.number.grid(row=0, column=0, sticky="ew", pady=4)
        self.title_field = LabeledEntry(self.body, "Title", placeholder="e.g. First draft", wraplength=380)
        self.title_field.grid(row=1, column=0, sticky="ew", pady=4)
        ctk.CTkLabel(self.body, text="Description", text_color=theme.TEXT_MUTED, font=font(theme.SIZE_SMALL, "bold"),
                     anchor="w").grid(row=2, column=0, sticky="ew", pady=(4, 4))
        self.description = AppTextbox(self.body, height=110, wrap="word", corner_radius=theme.RADIUS_CONTROL,
                                      border_width=1, border_color=theme.CARD_BORDER, fg_color=theme.INPUT_BG,
                                      text_color=theme.TEXT_PRIMARY, font=font())
        self.description.grid(row=3, column=0, sticky="ew")
        self.description_error = ctk.CTkLabel(self.body, text="", text_color=theme.TONES["error"].foreground,
                                              font=font(theme.SIZE_CAPTION), anchor="w")
        self.description_error.grid(row=4, column=0, sticky="ew")
        self.initial_focus = self.number.entry if not number else self.title_field.entry
        self.add_buttons("Add milestone", self.confirm)
        self.present()

    def _on_return(self, event=None):
        if event is not None and getattr(event.widget, "winfo_class", lambda: "")() == "Text":
            return None  # a new line in the description
        return super()._on_return(event)

    def values(self) -> tuple[str, str, str]:
        return self.number.get().strip(), self.title_field.get().strip(), self.description.get("1.0", "end").strip()

    def confirm(self) -> None:
        number, title, description = self.values()
        errors = milestone_errors(number, title, description)
        self.number.set_error(errors.get("number"))
        self.title_field.set_error(errors.get("title"))
        self.description_error.configure(text=errors.get("description", ""))
        if not errors:
            self.close((number, title, description))


def milestone_errors(number: str, title: str, description: str) -> dict[str, str]:
    """Field -> message for what is missing or not a whole number in a new milestone."""
    errors: dict[str, str] = {}
    if not number.strip():
        errors["number"] = "Enter the milestone number."
    else:
        try:
            int(number.strip())
        except ValueError:
            errors["number"] = "The milestone number must be a whole number."
    if not title.strip():
        errors["title"] = "Enter a title."
    if not description.strip():
        errors["description"] = "Describe the milestone."
    return errors


class ProjectDialog(ModalDialog):
    """
    Edit a project's name, description, planned dates and the defaults of its
    new tasks; the result is (name, description, start, end, (default
    duration, priority, points)) as typed, or None (the priority is the stored one: it is not shown).
    """

    def __init__(self, parent, project: ProjectRow) -> None:
        super().__init__(parent, "Edit project", width=420)
        self.name = LabeledEntry(self.body, "Project name", wraplength=380)
        self.name.variable.set(project.name)
        self.description = LabeledEntry(self.body, "Project description", wraplength=380)
        self.description.variable.set(project.description)
        self.start = DateField(self.body, "Start date", hint=OPTIONAL_DATE_HINT)
        self.start.show(project.start_date)
        self.end = DateField(self.body, "Estimated end date", hint=OPTIONAL_DATE_HINT)
        self.end.show(project.estimated_end_date)
        for row, widget in enumerate((self.name, self.description, self.start, self.end)):
            widget.grid(row=row, column=0, sticky="ew", pady=4)
        defaults = project.task_defaults
        SectionTitle(self.body, "Defaults for new tasks", "Used for tasks you add to this project from now on; "
                     "existing tasks keep their values. Leave one empty to use the category's default.",
                     wraplength=380).grid(row=4, column=0, sticky="ew", pady=(10, 2))
        self.default_duration = LabeledEntry(self.body, "Default duration", placeholder="not set",
                                             hint="e.g. 45m or 1h 30m", wraplength=380)
        self.default_duration.variable.set(
            format_duration(defaults.duration_minutes) if defaults.duration_minutes is not None else "")
        self.default_priority = LabeledEntry(self.body, "Default priority (1-10)", placeholder="not set",
                                             wraplength=380)
        self.default_priority.variable.set("" if defaults.priority is None else str(defaults.priority))
        self.default_points = LabeledEntry(self.body, "Default points", placeholder="not set", wraplength=380)
        self.default_points.variable.set("" if defaults.points is None else str(defaults.points))
        # The default priority is not shown (no scheduler reads a priority); a stored one is carried through.
        for row, widget in enumerate((self.default_duration, self.default_points), start=5):
            widget.grid(row=row, column=0, sticky="ew", pady=4)
        self.initial_focus = self.name.entry
        self.add_buttons("Save project", lambda: self.close(
            (self.name.get(), self.description.get(), self.start.get(), self.end.get(),
             (self.default_duration.get(), self.default_priority.get(), self.default_points.get()))))
        self.present()


class CollapsibleSection(ctk.CTkFrame):
    """A bar with a dropdown arrow that shows or hides `content`, on its own."""

    def __init__(self, parent, title: str, *, expanded: bool = True) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.title, self.expanded, self.count = title, expanded, 0
        self.bar = AppButton(self, "", self.toggle, variant="secondary", anchor="w")
        self.bar.grid(row=0, column=0, sticky="ew")
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.columnconfigure(0, weight=1)
        self._show()

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._show()

    def set_count(self, count: int) -> None:
        self.count = count
        self._show()

    def _show(self) -> None:
        self.bar.configure(text=f"{'▾' if self.expanded else '▸'}  {self.title} ({self.count})")
        if self.expanded:
            self.content.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        else:
            self.content.grid_remove()

    def clear(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()


class ProjectsPage(ctk.CTkFrame):
    def __init__(self, parent, controller, *, on_open_day, task_defaults=None, on_open_performance=None) -> None:
        super().__init__(parent, fg_color=theme.APP_BG)
        self.controller, self.on_open_day = controller, on_open_day
        #: Opens Performance -> Project for a project id (None: the action is not offered).
        self.on_open_performance = on_open_performance
        self.task_defaults = task_defaults
        self.snapshot: ProjectsSnapshot | None = None
        #: The project whose detail view is open (None: the overview).
        self.selected: uuid.UUID | None = None
        self._busy = False
        self._options_token = 0
        self.layout: LayoutMode | None = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_overview()
        self._build_detail()
        self._show_view()

    # ------------------------------------------------------------------ building

    def _build_overview(self) -> None:
        self.overview = view = AppScrollableFrame(self, fg_color="transparent")
        view.columnconfigure(0, weight=1)
        header = PageHeader(view, "Project Schedule", "Create a project, then double-click it to plan its tasks and "
                                                      "milestones.")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=16)
        for label in header.winfo_children():
            label.configure(wraplength=360)
        create = self.create_card = Card(view)
        create.grid(row=1, column=0, sticky="ew", padx=20)
        create.columnconfigure((0, 1), weight=1, uniform="create")
        SectionTitle(create, "New project").grid(row=0, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L,
                                                 pady=(theme.SPACE_L, 4))
        self.name = LabeledEntry(create, "Project name", placeholder="e.g. Math course")
        self.description = LabeledEntry(create, "Project description")
        self.start_date = DateField(create, "Start date", hint=OPTIONAL_DATE_HINT)
        self.end_date = DateField(create, "Estimated end date", hint=OPTIONAL_DATE_HINT)
        self.name.grid(row=1, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=4)
        self.description.grid(row=2, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=4)
        self.start_date.grid(row=3, column=0, sticky="new", padx=(theme.SPACE_L, 6), pady=4)
        self.end_date.grid(row=3, column=1, sticky="new", padx=(6, theme.SPACE_L), pady=4)
        self.create_button = AppButton(create, "Create Project", self.create)
        self.create_button.grid(row=4, column=0, columnspan=2, sticky="w", padx=theme.SPACE_L,
                                pady=(8, theme.SPACE_L))
        self.overview_notice = Notice(view, wraplength=360)
        self.overview_notice.grid(row=2, column=0, sticky="ew", padx=20, pady=8)
        self.overview_notice.hide()
        self.ongoing_section = CollapsibleSection(view, "Ongoing projects")
        self.ongoing_section.grid(row=3, column=0, sticky="ew", padx=20, pady=(4, 8))
        self.completed_section = CollapsibleSection(view, "Completed projects")
        self.completed_section.grid(row=4, column=0, sticky="ew", padx=20, pady=(4, 20))

    def _build_detail(self) -> None:
        self.detail = view = ctk.CTkFrame(self, fg_color="transparent")
        view.columnconfigure(0, weight=1)
        view.rowconfigure(2, weight=1)
        top = ctk.CTkFrame(view, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 6))
        top.columnconfigure(1, weight=1)
        self.back_button = AppButton(top, "← All projects", self.show_overview, variant="secondary")
        self.back_button.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12))
        self.detail_title = ctk.CTkLabel(top, text="", font=font(theme.SIZE_HEADING, "bold"),
                                         text_color=theme.TEXT_PRIMARY, anchor="w", justify="left", wraplength=420)
        self.detail_title.grid(row=0, column=1, sticky="ew")
        self.detail_subtitle = ctk.CTkLabel(top, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
                                            anchor="w", justify="left", wraplength=420)
        self.detail_subtitle.grid(row=1, column=1, sticky="ew")
        bar = ctk.CTkFrame(top, fg_color="transparent")
        bar.grid(row=0, column=2, rowspan=2, sticky="ne")
        self.complete_button = AppButton(bar, "Mark complete", self.toggle_completed)
        self.complete_button.grid(row=0, column=0, padx=(8, 0))
        self.edit_button = AppButton(bar, "Edit…", self.edit_project, variant="secondary", width=80)
        self.edit_button.grid(row=0, column=1, padx=(8, 0))
        self.delete_button = AppButton(bar, "Delete empty project", self.delete, variant="danger")
        self.delete_button.grid(row=0, column=2, padx=(8, 0))
        self.performance_button = AppButton(bar, "View performance", self.open_performance, variant="secondary")
        if self.on_open_performance is not None:
            self.performance_button.grid(row=1, column=0, columnspan=3, sticky="e", padx=(8, 0), pady=(6, 0))
        self.detail_notice = Notice(view, wraplength=520)
        self.detail_notice.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 6))
        self.detail_notice.hide()

        self.sections = sections = ctk.CTkFrame(view, fg_color="transparent")
        sections.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 16))

        # Add Task: the shared task form, in its own scroll area (it is taller than a small window).
        self.form_area = AppScrollableFrame(sections, fg_color="transparent", width=330)
        self.form_area.columnconfigure(0, weight=1)
        self.form = TaskEditor(self.form_area, on_submit=self.submit_task, on_cancel=lambda: None,
                               task_defaults=self.task_defaults, date_selector=True)
        self.form.title.subtitle_label.configure(
            text="Added to this project on the chosen date, without a time; schedule it from that day.")
        self.form.grid(row=0, column=0, sticky="ew")

        self.tasks_card = Card(sections)
        self.tasks_card.columnconfigure(0, weight=1)
        self.tasks_card.rowconfigure(1, weight=1)
        self.tasks_title = SectionTitle(self.tasks_card, "Project Tasks", "Tick Done to complete a task; click an "
                                                                          "unfinished one to give it a day.", wraplength=300)
        self.tasks_title.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 6))
        self.tasks_list = AppScrollableFrame(self.tasks_card, fg_color="transparent")
        self.tasks_list.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 8))
        self.tasks_list.columnconfigure(0, weight=1)

        self.milestones_card = Card(sections)
        self.milestones_card.columnconfigure(0, weight=1)
        self.milestones_card.rowconfigure(2, weight=1)
        SectionTitle(self.milestones_card, "Milestones").grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L,
                                                              pady=(theme.SPACE_L, 6))
        self.add_milestone_button = AppButton(self.milestones_card, "+ Add Milestone", self.ask_milestone)
        self.add_milestone_button.grid(row=1, column=0, sticky="w", padx=theme.SPACE_L, pady=(0, 8))
        self.milestones_list = AppScrollableFrame(self.milestones_card, fg_color="transparent")
        self.milestones_list.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 8))
        self.milestones_list.columnconfigure(0, weight=1)
        self.set_layout(LayoutMode.WIDE)

    def set_layout(self, mode: LayoutMode) -> None:
        """Three columns side by side; stacked in a narrow window. Each list keeps its own scrolling."""
        if mode == self.layout:
            return
        self.layout = mode
        sections = self.sections
        areas = (self.form_area, self.tasks_card, self.milestones_card)
        for index in range(3):
            sections.columnconfigure(index, weight=0, minsize=0, uniform="")
            sections.rowconfigure(index, weight=0)
        if mode == LayoutMode.NARROW:
            sections.columnconfigure(0, weight=1)
            for row, (area, weight) in enumerate(zip(areas, (3, 2, 2))):
                sections.rowconfigure(row, weight=weight)
                area.grid(row=row, column=0, sticky="nsew", padx=0, pady=(0, 8))
        else:
            sections.rowconfigure(0, weight=1)
            sections.columnconfigure(0, weight=0, minsize=350)
            sections.columnconfigure((1, 2), weight=1, uniform="lists")
            for column, area in enumerate(areas):
                area.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0), pady=0)

    # ------------------------------------------------------------------ plumbing

    @property
    def notice(self) -> Notice:
        return self.detail_notice if self.selected is not None else self.overview_notice

    def work(self, operation, done, failed=None) -> None:
        """Run one controller call in order with other changes; `failed(result)` replaces the error notice."""
        if self._busy:
            return
        self._busy = True
        notice = self.notice
        notice.show("info", "Working…")

        def finish(result):
            self._busy = False
            if result.ok:
                notice.hide()
                done(result.value)
            elif failed is not None:
                notice.hide()
                failed(result)
            else:
                notice.show("error", result.error)

        if not run_in_background(self, operation, finish, serial=WRITE_LANE):  # in order with other changes
            self._busy = False

    def _show_view(self) -> None:
        if self.selected is None:
            self.detail.grid_remove()
            self.overview.grid(row=0, column=0, sticky="nsew")
        else:
            self.overview.grid_remove()
            self.detail.grid(row=0, column=0, sticky="nsew")

    def on_show(self) -> None:
        selected = self.selected
        self.work(lambda: self.controller.load(selected), self.render)

    # ------------------------------------------------------------------ rendering

    def render(self, snapshot: ProjectsSnapshot) -> None:
        self.snapshot = snapshot
        if self.selected is not None and snapshot.selected is None:
            self.selected = None  # the open project was deleted elsewhere
        self._show_view()
        self._render_overview(snapshot)
        if snapshot.selected is not None:
            self._render_detail(snapshot)

    def _render_overview(self, snapshot: ProjectsSnapshot) -> None:
        for section, rows, empty in (
                (self.ongoing_section, snapshot.ongoing, "No ongoing projects. Create one above."),
                (self.completed_section, snapshot.completed, "No completed projects yet.")):
            section.clear()
            section.set_count(len(rows))
            if not rows:
                ctk.CTkLabel(section.content, text=empty, anchor="w", font=font(theme.SIZE_SMALL),
                             text_color=theme.TEXT_MUTED).grid(row=0, column=0, sticky="ew", padx=6, pady=4)
            for index, row in enumerate(rows):
                self._project_row(section.content, row).grid(row=index, column=0, sticky="ew", pady=3)

    def _project_row(self, parent, row: ProjectRow) -> Card:
        card = Card(parent)
        card.columnconfigure(0, weight=1)
        details = " · ".join(part for part in (
            row.dates_text, f"{row.task_count} task(s), {row.scheduled_count} scheduled" if row.task_count else "no tasks",
            "Completed" if row.completed else "") if part)
        name = ctk.CTkLabel(card, text=row.name, anchor="w", justify="left", wraplength=300,
                            font=font(theme.SIZE_BODY, "bold"), text_color=theme.TEXT_PRIMARY)
        name.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 0))
        text = "\n".join(part for part in (row.description, details) if part)
        info = ctk.CTkLabel(card, text=text, anchor="w", justify="left", wraplength=300, font=font(theme.SIZE_SMALL),
                            text_color=theme.TEXT_MUTED)
        info.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        AppButton(card, "Open", lambda: self.open_project(row.id), variant="secondary", width=70, height=30).grid(
            row=0, column=1, rowspan=2, padx=(0, 10))
        for widget in (card, name, info):
            widget.bind("<Double-Button-1>", lambda _event, project_id=row.id: self.open_project(project_id), add="+")
        return card

    def _render_detail(self, snapshot: ProjectsSnapshot) -> None:
        project = snapshot.selected
        self.detail_title.configure(text=project.name + ("  ·  Completed" if project.completed else ""))
        self.detail_subtitle.configure(text="\n".join(part for part in (project.description, project.dates_text) if part))
        self.complete_button.configure(text="Reopen project" if project.completed else "Mark complete")
        if self.form.project_defaults != project.task_defaults:
            # Changed defaults apply to the next task: an untouched form takes them now, typed values stay.
            self.form.project_defaults = project.task_defaults
            if not self.form.name_field.get().strip() and not self.form.duration_field.get().strip():
                self.form.reset_for_next(self.form.date_field.get().strip())
        for child in self.tasks_list.winfo_children():
            child.destroy()
        if not snapshot.tasks:
            ctk.CTkLabel(self.tasks_list, text="No tasks in this project yet. Add one on the left.", anchor="w",
                         justify="left", wraplength=260, font=font(theme.SIZE_SMALL),
                         text_color=theme.TEXT_MUTED).grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        for index, task in enumerate(snapshot.tasks):
            self._task_row(task).grid(row=index, column=0, sticky="ew", padx=2, pady=3)
        for child in self.milestones_list.winfo_children():
            child.destroy()
        if not snapshot.milestones:
            ctk.CTkLabel(self.milestones_list, text="No milestones yet.", anchor="w", font=font(theme.SIZE_SMALL),
                         text_color=theme.TEXT_MUTED).grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        for index, milestone in enumerate(snapshot.milestones):
            self._milestone_card(milestone).grid(row=index, column=0, sticky="ew", padx=2, pady=3)

    def _remove_button(self, parent, command, text_color, tip: str) -> AppButton:
        """The "×" on the left of a task or milestone: its own control, so it never acts as a click on the row."""
        button = AppButton(parent, "×", command, variant="ghost", width=30, height=28,
                           font=font(theme.SIZE_HEADING, "bold"), style=dict(text_color=text_color,
                                                                             border_color=text_color))
        Tooltip(button, tip)
        return button

    def _task_row(self, task: ProjectTaskRow) -> ctk.CTkFrame:
        background, text_color = ((TASK_DONE_BG, TASK_DONE_TEXT) if task.completed
                                  else (theme.SECONDARY_BG, theme.TEXT_PRIMARY))
        row = ctk.CTkFrame(self.tasks_list, fg_color=background, corner_radius=theme.RADIUS_CONTROL)
        row.columnconfigure(1, weight=1)
        row.remove_button = self._remove_button(row, lambda task=task: self.ask_remove_task(task), text_color,
                                                f"Remove: {task.name}")
        row.remove_button.grid(row=0, column=0, rowspan=2, padx=(8, 0), pady=6)
        lines = [f"{task.status_text}  ·  {task.duration_text}  ·  {task.date_text}", task.status]
        if task.deadline_text:
            lines.append(f"Deadline: {task.deadline_text}")
        if task.recurring:
            lines.append("Repeating rule; its occurrences are listed on their days.")
        name = ctk.CTkLabel(row, text=task.name, anchor="w", justify="left", wraplength=190,
                            font=font(theme.SIZE_BODY, "bold"), text_color=text_color)
        name.grid(row=0, column=1, sticky="ew", padx=8, pady=(6, 0))
        info = ctk.CTkLabel(row, text="\n".join(lines), anchor="w", justify="left", wraplength=190,
                            font=font(theme.SIZE_SMALL), text_color=text_color)
        info.grid(row=1, column=1, sticky="ew", padx=8, pady=(0, 6))
        row.done_box = None
        if not task.recurring:
            # Completion right here, scheduled or not (a project's task needs no time slot for it).
            done = tk.BooleanVar(value=task.completed)
            row.done_box = ctk.CTkCheckBox(
                row, text="Done", variable=done, width=64, text_color=text_color, fg_color=theme.SUCCESS,
                command=lambda task=task, done=done: self.set_task_completed(task, done.get()))
            row.done_box.grid(row=0, column=2, rowspan=2, padx=(0, 10))
            make_keyboard_accessible(row.done_box, activate=row.done_box.toggle, ring=False)
        if task.movable:
            for widget in (row, name, info):
                widget.bind("<Button-1>", lambda _event, task=task: self.ask_task_date(task), add="+")
                widget.configure(cursor="hand2")
        return row

    def _milestone_card(self, milestone: MilestoneRow) -> Card:
        background, text = SCORE_COLORS[milestone.band]
        card = Card(self.milestones_list, fg_color=background)  # the whole widget carries its score's color
        card.columnconfigure(1, weight=1)
        card.remove_button = self._remove_button(card, lambda: self.ask_remove_milestone(milestone), text,
                                                 f"Remove milestone {milestone.number}")
        card.remove_button.grid(row=0, column=0, rowspan=2, padx=(8, 0), pady=6)
        ctk.CTkLabel(card, text=f"{milestone.number}. {milestone.title}", anchor="w", justify="left", wraplength=170,
                     font=font(theme.SIZE_BODY, "bold"), text_color=text).grid(
            row=0, column=1, sticky="ew", padx=8, pady=(8, 0))
        ctk.CTkLabel(card, text=milestone.description, anchor="w", justify="left", wraplength=170,
                     font=font(theme.SIZE_SMALL), text_color=text).grid(
            row=1, column=1, sticky="ew", padx=8, pady=(2, 8))
        # The score control stays a plain input surface, readable on every band.
        score = AppOptionMenu(card, values=SCORES, width=64, height=34, corner_radius=theme.RADIUS_CONTROL,
                              fg_color=theme.INPUT_BG, button_color=theme.SECONDARY_HOVER,
                              button_hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT_PRIMARY,
                              font=font(theme.SIZE_BODY, "bold"), dynamic_resizing=False,
                              command=lambda value, milestone_id=milestone.id: self.set_score(milestone_id, int(value)))
        score.set(str(milestone.score))
        score.grid(row=0, column=2, rowspan=2, sticky="e", padx=(0, 10))
        card.score_menu = score
        return card

    # ------------------------------------------------------------------ overview actions

    def create(self) -> None:
        if self._busy:
            return
        values = (self.name.get(), self.description.get(), self.start_date.get(), self.end_date.get())

        def created(_project) -> None:
            for field in (self.name, self.description, self.start_date, self.end_date):
                field.variable.set("")
            self.on_show()

        self.work(lambda: self.controller.create(*values), created)

    def open_project(self, project_id: uuid.UUID) -> None:
        if self._busy:
            return
        self.selected = project_id
        self.detail_notice.hide()
        self._reset_form()
        self.on_show()

    def show_overview(self) -> None:
        if self._busy:
            return
        self.selected = None
        self._show_view()
        self.on_show()

    # ------------------------------------------------------------------ detail actions

    def _project(self) -> ProjectRow | None:
        return self.snapshot.selected if self.snapshot is not None and self.selected is not None else None

    def toggle_completed(self) -> None:
        project = self._project()
        if project is None:
            return
        self.work(lambda: self.controller.set_completed(project.id, not project.completed,
                                                        expected_version=project.version), lambda _: self.on_show())

    def edit_project(self) -> None:
        project = self._project()
        if project is None or self._busy:
            return
        values = ProjectDialog(self, project).wait()
        if values is None:
            return
        name, description, start, end, defaults = values

        def saved(_project) -> None:
            self.on_show()

        self.work(lambda: self.controller.update(project.id, name, description, expected_version=project.version,
                                                 start_date=start, estimated_end_date=end, set_dates=True,
                                                 task_defaults=defaults), saved)

    def open_performance(self) -> None:
        """Performance -> Project, with this project already selected."""
        project = self._project()
        if project is not None and self.on_open_performance is not None:
            self.on_open_performance(project.id)

    def delete(self) -> None:
        project = self._project()
        if self._busy or project is None:
            return
        if ask_confirm(self, title="Delete project?", message=f"Delete {project.name}? Tasks are never deleted with it.",
                       confirm_text="Delete", danger=True):
            def deleted(_):
                self.selected = None
                self.on_show()
            self.work(lambda: self.controller.delete(project.id, expected_version=project.version), deleted)

    # -- Add Task

    def _reset_form(self) -> None:
        """A blank form dated today, with this workspace's current choices."""
        self._options_token += 1
        token = self._options_token
        self.form.load(TaskDraft(kind="task", category="", date=self.controller.today().isoformat()), editing=False)

        def apply(options) -> None:
            if options.ok and token == self._options_token:
                category = self.form.category_select.get()
                self.form.set_options(options.value)
                if category in self.form.category_select.values:
                    self.form.category_select.variable.set(category)

        run_in_background(self, self.controller.editor_options, apply)

    def submit_task(self, draft: TaskDraft) -> None:
        """Add the form's task to this project on its chosen date, unscheduled; a refusal keeps everything typed."""
        project = self._project()
        if project is None or self._busy:
            return

        def added(_snapshot) -> None:
            self.form.reset_for_next(draft.date)
            self.on_show()

        def refused(result) -> None:
            errors = result.cause.errors if isinstance(result.cause, FormErrors) else None
            self.form.show_errors(result.error or "The task could not be saved.", errors)

        self.form.clear_errors()
        self.work(lambda: self.controller.add_task(project.id, draft, draft.date), added, refused)

    # -- Project Tasks

    def set_task_completed(self, task: ProjectTaskRow, completed: bool) -> None:
        def failed(result) -> None:
            self.notice.show("error", result.error or "The task's completion was not changed.")
            self.render(self.snapshot)  # the control goes back to what is stored

        self.work(lambda: self.controller.set_task_completed(task.task_id, completed), lambda _: self.on_show(),
                  failed)

    def ask_remove_task(self, task: ProjectTaskRow) -> None:
        """The "×" of a task: describe what goes with it, confirm (a repeating task: choose what), then remove."""
        if self._busy:
            return
        self.work(lambda: self.controller.task_removal(task.task_id), lambda found: self._confirm_task_removal(task, *found))

    def _confirm_task_removal(self, task: ProjectTaskRow, description: str, choices: list[tuple[str, str]]) -> None:
        scope = self._ask_removal(description, choices)
        if scope is not False:
            self.work(lambda: self.controller.remove_task(task.task_id, expected_version=task.version, scope=scope),
                      lambda _: self.on_show())

    def _ask_removal(self, description: str, choices: list[tuple[str, str]]):
        """The removal scope to use (None: the only one), or False when cancelled."""
        if len(choices) > 1:
            chosen = ChoiceDialog(self, title="Remove a repeating task", prompt=description, options=choices,
                                  danger=True, on_choose=lambda _choice: None).wait()
            return chosen if chosen is not None else False
        if not ask_confirm(self, title="Remove?", message=description, confirm_text="Remove", danger=True):
            return False
        return choices[0][0] if choices else None

    def ask_remove_milestone(self, milestone: MilestoneRow) -> None:
        project = self._project()
        if self._busy or project is None:
            return
        if self._confirm_milestone_removal(milestone):
            self.work(lambda: self.controller.remove_milestone(project.id, milestone.id), lambda _: self.on_show())

    def _confirm_milestone_removal(self, milestone: MilestoneRow) -> bool:
        return ask_confirm(self, title="Remove milestone?", confirm_text="Remove", danger=True,
                           message=f"Remove milestone {milestone.number}, “{milestone.title}”? Its description and "
                                   "score are removed with it.")

    def ask_task_date(self, task: ProjectTaskRow) -> None:
        """Ask for the day an incomplete task goes to; cancelling changes nothing."""
        if self._busy or not task.movable:
            return
        day = self._ask_date(task)
        if day is not None:
            self.assign_task_date(task, day)

    def _ask_date(self, task: ProjectTaskRow) -> date | None:
        return DateDialog(self, title="Choose a day", prompt=f"Which day should “{task.name}” be on? It is put there "
                          "without a time; any time it was scheduled at is removed.",
                          initial=task.dates[0] if task.dates else self.controller.today()).wait()

    def assign_task_date(self, task: ProjectTaskRow, day: date) -> None:
        self.work(lambda: self.controller.assign_date(task.task_id, day, expected_version=task.version),
                  lambda _: self.on_show())

    # -- Milestones

    def ask_milestone(self) -> None:
        if self._busy or self._project() is None:
            return
        values = self._ask_milestone()
        if values is not None:
            self.add_milestone(*values)

    def _ask_milestone(self) -> tuple[str, str, str] | None:
        numbers = [milestone.number for milestone in self.snapshot.milestones]
        return MilestoneDialog(self, number=str(max(numbers) + 1) if numbers else "1").wait()

    def add_milestone(self, number: str, title: str, description: str) -> None:
        project = self._project()
        if project is None:
            return
        self.work(lambda: self.controller.add_milestone(project.id, number, title, description),
                  lambda _: self.on_show())

    def set_score(self, milestone_id: uuid.UUID, score: int) -> None:
        project = self._project()
        if project is None:
            return
        self.work(lambda: self.controller.set_milestone_score(project.id, milestone_id, score),
                  lambda _: self.on_show())
