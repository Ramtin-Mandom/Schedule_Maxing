"""
app/ui/calendar_page.py

The desktop Week and Month pages (Milestone 4, Prompt 5): one class, two
modes, drawn by app/ui/calendar_view.py from a CalendarSnapshot of
app/ui/calendar_controller.py. Nothing here schedules, validates or stores
anything itself.

Layout, top to bottom (one scrolling column; the lower part is two columns
on wide and medium windows and one on narrow ones):

- Header: the period's name (for Month, the month and year, prominently),
  Previous / Today / Next, a choice of the current year's months (Month),
  a date field, and the period's status.
- The calendar, then the selected day's exact, ordered details with an
  explicit **Open Day** button (a double-click is only a shortcut).
- The reusable task form, creating on the selected date; **Reset Week /
  Month...** (previewed, confirmed, atomic); the period's task list.

Scheduling is done on the Day page: Open Day shows the date there with a
way back to this page, which keeps its week/month and selected day.

Moving to another week or month loads in a background worker; a result for
a period the page no longer shows, or from before an account switch, is
dropped. Re-reads after edits, imports, resets, sync or returning to the
page (on_show) refresh every date and its freshness.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from datetime import date
from tkinter import messagebox

import customtkinter as ctk

from app.ui import theme
from app.ui.background import ControllerResult, run_in_background
from app.ui.calendar_controller import CalendarController, CalendarSnapshot
from app.ui.calendar_model import Period
from app.ui.calendar_view import CalendarView
from app.ui.components import AppButton, Card, LabeledSelect, Notice, SectionTitle, ask_confirm, font
from app.ui.shell_state import LayoutMode
from app.ui.task_actions import TaskFormActions
from app.ui.task_editor import TaskEditor
from app.ui.task_list import AddedTasksPanel
from app.ui.projects_controller import project_choices

_HEADER_WRAP = {LayoutMode.WIDE: 760, LayoutMode.MEDIUM: 560, LayoutMode.NARROW: 400}


class CalendarPage(TaskFormActions, ctk.CTkFrame):
    def __init__(
        self,
        parent: tk.Widget,
        mode_name: str,
        page_controller: CalendarController,
        productivity_controller=None,
        *,
        on_anchor_changed: Callable[[str, date], None] | None = None,
        on_open_day: Callable[[date], None] | None = None,
    ) -> None:
        super().__init__(parent, fg_color=theme.APP_BG, corner_radius=0)
        self.mode_name = mode_name
        self.page_controller = page_controller
        self.productivity_controller = productivity_controller
        self._on_anchor_changed = on_anchor_changed
        self._on_open_day = on_open_day
        self.snapshot: CalendarSnapshot | None = None
        self._editing = None
        self._busy = False
        self.loading = False
        self._load_token = 0
        self._unfiltered = None
        self._project_choices = {}
        self.layout: LayoutMode | None = None
        self.execution_panel = None  # Execute is on the Day page (Open Day)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build_header()
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color=theme.SECONDARY_HOVER)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        self.schedule_canvas = self.calendar = CalendarView(self.body, mode=mode_name, on_select=self.select_date,
                                                            on_open=self.open_day)
        self.calendar.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_M))
        self._build_details()
        self._build_lower()
        self.set_layout(LayoutMode.WIDE)
        self.reload()
        self._reset_editor()

    # ----------------------------- Building -----------------------------

    def _build_header(self) -> None:
        self.header = header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(theme.SPACE_L, 8))
        header.columnconfigure(0, weight=1)
        kind = "Week" if self.mode_name == "week" else "Month"
        ctk.CTkLabel(header, text=f"{kind} Schedule", font=font(theme.SIZE_SMALL, "bold"), text_color=theme.TEXT_MUTED,
                     anchor="w").grid(row=0, column=0, sticky="w")
        self.title_label = ctk.CTkLabel(header, text="", font=font(theme.SIZE_TITLE, "bold"),
                                        text_color=theme.TEXT_PRIMARY, anchor="w")
        self.title_label.grid(row=1, column=0, sticky="w")
        nav = ctk.CTkFrame(header, fg_color="transparent")
        nav.grid(row=2, column=0, sticky="w", pady=(6, 0))
        unit = "week" if self.mode_name == "week" else "month"
        self.prev_button = AppButton(nav, f"‹ Previous {unit}", lambda: self.shift(-1), variant="secondary",
                                     height=32, width=130)
        self.prev_button.grid(row=0, column=0, padx=(0, 6), sticky="s")
        self.today_button = AppButton(nav, "Today", self.go_today, variant="secondary", width=70, height=32)
        self.today_button.grid(row=0, column=1, padx=(0, 6), sticky="s")
        self.next_button = AppButton(nav, f"Next {unit} ›", lambda: self.shift(1), variant="secondary",
                                     height=32, width=120)
        self.next_button.grid(row=0, column=2, padx=(0, 12), sticky="s")
        column = 3
        self.month_select = None
        if self.mode_name == "month":
            labels = [label for _, _, label in self.page_controller.month_choices()]
            self.month_select = LabeledSelect(nav, "Month", labels, command=self._month_chosen, width=170)
            self.month_select.grid(row=0, column=column, padx=(0, 12), sticky="s")
            column += 1
        self._dates_column = column
        self.dates_row = dates = ctk.CTkFrame(nav, fg_color="transparent")
        dates.grid(row=0, column=column, sticky="s")
        date_label = ctk.CTkLabel(dates, text="Go to date:", font=font(theme.SIZE_BODY), text_color=theme.TEXT_MUTED)
        date_label.pack(side="left")
        self.start_date_var = tk.StringVar(value=self.page_controller.selected_date.isoformat())
        self.start_date_entry = ctk.CTkEntry(dates, textvariable=self.start_date_var, width=110, height=32,
                                             fg_color=theme.INPUT_BG, border_color=theme.CARD_BORDER,
                                             text_color=theme.TEXT_PRIMARY)
        self.start_date_entry.pack(side="left", padx=(6, 4))
        self.start_date_entry.bind("<Return>", lambda _e: self.apply_start_date(), add="+")
        date_label.bind("<Button-1>", lambda _e: self.start_date_entry.focus_set(), add="+")
        self.go_button = AppButton(dates, "Go", self.apply_start_date, width=48, height=32)
        self.go_button.pack(side="left")
        self.status_label = ctk.CTkLabel(header, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
                                         anchor="w", justify="left", wraplength=760)
        self.status_label.grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.project_filter = LabeledSelect(header, "Show project (display only; fixed blocks stay visible)",
                                            ["All projects"], command=self._filter_project)
        self.project_filter.grid(row=4, column=0, sticky="ew")

    def _build_details(self) -> None:
        self.details_card = card = Card(self.body)
        card.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_M))
        card.columnconfigure(0, weight=1)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 4))
        top.columnconfigure(0, weight=1)
        self.details_title = ctk.CTkLabel(top, text="", font=font(theme.SIZE_HEADING, "bold"),
                                          text_color=theme.TEXT_PRIMARY, anchor="w")
        self.details_title.grid(row=0, column=0, sticky="w")
        self.open_day_button = AppButton(top, "Open Day", lambda: self.open_day(self.page_controller.selected_date),
                                         width=130)
        self.open_day_button.grid(row=0, column=1, sticky="e")
        self.details_label = ctk.CTkLabel(card, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_PRIMARY,
                                          anchor="w", justify="left", wraplength=760)
        self.details_label.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L, pady=(0, theme.SPACE_L))

    def _build_lower(self) -> None:
        self.lower = lower = ctk.CTkFrame(self.body, fg_color="transparent")
        lower.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_XL))
        self.form = TaskEditor(lower, on_submit=self.submit_task, on_cancel=self.cancel_edit,
                               productivity_controller=self.productivity_controller)
        self.side = side = ctk.CTkFrame(lower, fg_color="transparent")
        side.columnconfigure(0, weight=1)
        actions = Card(side)
        actions.grid(row=0, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        unit = "week" if self.mode_name == "week" else "month"
        SectionTitle(actions, "Schedule and reset", f"Schedules are made on the Day page: select a day and Open Day. "
                                                    f"Reset clears this {unit}'s own dates after showing what goes.",
                     wraplength=420).grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 8))
        self.reset_button = AppButton(actions, f"Reset {unit.title()}...", self.reset_period, variant="danger")
        self.reset_button.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        self.notice = Notice(actions, wraplength=420)
        self.notice.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, theme.SPACE_L))
        self.notice.hide()
        self.right_card = tasks = Card(side)
        tasks.grid(row=1, column=0, sticky="ew", pady=(theme.SPACE_M, 0))
        tasks.columnconfigure(0, weight=1)
        SectionTitle(tasks, f"Tasks this {unit}", "", wraplength=420).grid(
            row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 0))
        self.added_tasks_panel = AddedTasksPanel(tasks, on_remove_task=self.remove_selected_task,
                                                 on_edit_task=self.edit_selected_task,
                                                 on_use_as_dependencies=self.use_selected_as_dependencies,
                                                 on_open_date=self.open_day)
        self.added_tasks_panel.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_S, pady=(0, theme.SPACE_L))

    # ----------------------------- Layout -----------------------------

    def set_layout(self, mode: LayoutMode) -> None:
        if mode == self.layout:
            return
        self.layout = mode
        self.form.grid_forget()
        self.side.grid_forget()
        for index in range(2):
            self.lower.columnconfigure(index, weight=0, minsize=0)
        pad = theme.SPACE_XL if mode != LayoutMode.NARROW else theme.SPACE_M
        for widget in (self.header, self.calendar, self.details_card, self.lower):
            widget.grid_configure(padx=pad)
        self.status_label.configure(wraplength=_HEADER_WRAP[mode])
        self.details_label.configure(wraplength=_HEADER_WRAP[mode])
        if mode == LayoutMode.NARROW:  # the date field moves under the buttons instead of widening the page
            self.dates_row.grid_configure(row=1, column=0, columnspan=self._dates_column, sticky="w", pady=(6, 0))
        else:
            self.dates_row.grid_configure(row=0, column=self._dates_column, columnspan=1, sticky="s", pady=0)
        if mode == LayoutMode.NARROW:
            self.lower.columnconfigure(0, weight=1)
            self.form.grid(row=0, column=0, sticky="new", pady=(0, theme.SPACE_M))
            self.side.grid(row=1, column=0, sticky="new")
        else:
            self.lower.columnconfigure(0, minsize=340)
            self.lower.columnconfigure(1, weight=1)
            self.form.grid(row=0, column=0, sticky="new", padx=(0, theme.SPACE_M))
            self.side.grid(row=0, column=1, sticky="new")

    def show_panel(self, key: str) -> None:
        if key == "input":
            try:
                self.body._parent_canvas.yview_moveto(max(0.0, self.lower.winfo_y() / max(1, self.body.winfo_height())))
            except (AttributeError, tk.TclError):
                pass

    def on_appearance_changed(self) -> None:
        self.calendar.request_redraw()
        self.added_tasks_panel.retag()

    # ----------------------------- Loading and navigation -----------------------------

    def reload(self) -> None:
        """Re-read the shown period from SQLite (startup, returning to the page, after any change)."""
        self._load_token += 1
        self.loading = False
        result = self.page_controller.load()
        if result.ok:
            self._render(result.value)
        else:
            messagebox.showerror("Could Not Load Saved Data", result.error or "Unknown error.", parent=self)

    def on_show(self) -> None:
        if not self._busy:
            self.reload()

    def _load_async(self) -> None:
        """Load the (new) period in a worker; only the newest request's result is shown."""
        self._load_token += 1
        token, period = self._load_token, self.page_controller.period
        self.loading = True
        self.status_label.configure(text=f"Loading {period.title}...")
        self.calendar.set_selected(period.selected)

        def done(result: ControllerResult[CalendarSnapshot]) -> None:
            self._loaded(token, period, result)

        if not run_in_background(self, lambda: self.page_controller.load_for(period), done):
            self.loading = False

    def _loaded(self, token: int, period: Period, result: ControllerResult[CalendarSnapshot]) -> None:
        if token != self._load_token or period.key != self.page_controller.period.key:
            return  # a newer navigation (or reload) replaced this one
        self.loading = False
        if result.ok:
            self._render(result.value)
        else:
            self.status_label.configure(text=f"Could not load {period.title}: {result.error}")

    def _moved(self, reload_needed: bool) -> None:
        self._remember()
        if reload_needed:
            self._load_async()
        else:
            self._show_selection()
        if not self._editing:
            self.form.date_field.variable.set(self.page_controller.selected_date.isoformat())

    def select_date(self, day: date) -> None:
        self._moved(self.page_controller.select(day))

    def shift(self, delta: int) -> None:
        self._moved(self.page_controller.shift(delta))

    def go_today(self) -> None:
        self._moved(self.page_controller.go_today())

    def _month_chosen(self, label: str) -> None:
        for year, month, text in self.page_controller.month_choices():
            if text == label:
                self._moved(self.page_controller.go_to_month(year, month))
                return

    def apply_start_date(self) -> None:
        try:
            day = date.fromisoformat(self.start_date_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Date", "Type the date as YYYY-MM-DD.", parent=self)
            self.start_date_var.set(self.page_controller.selected_date.isoformat())
            return
        self.page_controller.select(day)
        self._remember()
        self.reload()

    def open_day(self, day: date) -> None:
        """Show `day` on the Day page (Back returns here, to this week/month and selection)."""
        if self.page_controller.period.contains(day):
            self.page_controller.select(day)
            self._show_selection()
        self._remember()
        if self._on_open_day is not None:
            self._on_open_day(day)

    def _remember(self) -> None:
        if self._on_anchor_changed is not None:
            self._on_anchor_changed(self.mode_name, self.page_controller.selected_date)

    # ----------------------------- Reset -----------------------------

    def reset_period(self) -> None:
        if self._refuse_while_busy():
            return
        plan = self.page_controller.reset_plan()
        if not plan.ok:
            self.notice.show("error", plan.error or "The reset could not be prepared.")
            return
        if plan.value.blocked:
            self.notice.show("error", plan.value.message)
            return
        unit = "Week" if self.mode_name == "week" else "Month"
        if not self._confirm(f"Reset {unit}?", plan.value.message, f"Reset {unit}", danger=True):
            self.notice.show("info", "Reset cancelled; nothing was deleted.")
            return
        result = self.page_controller.reset_period(plan.value)
        if result.value is not None:
            self._render(result.value)
        if not result.ok:
            self.notice.show("error", result.error or "The reset failed; nothing was deleted.")
            return
        self._leave_edit_mode()
        self._reset_editor()
        self.notice.show("success", f"{self.page_controller.period.title} was reset. Your default preferences, "
                                    "undated tasks and execution history were kept.")

    # ----------------------------- Rendering -----------------------------

    def _render(self, snapshot: CalendarSnapshot) -> None:
        self._unfiltered = snapshot
        self._project_choices = project_choices(snapshot.projects)
        selected = self.project_filter.get()
        self.project_filter.set_values(["All projects", *self._project_choices],
                                       selected=selected if selected in self._project_choices else "All projects")
        snapshot = snapshot.filtered(self._project_choices.get(self.project_filter.get()))
        self.snapshot = snapshot
        period = snapshot.period
        self.title_label.configure(text=period.title)
        if self.month_select is not None:
            self.month_select.variable.set(period.title)  # shown even when outside the current year's list
        undated = f" {snapshot.undated_count} task(s) have no date; they are offered on every Day page." \
            if snapshot.undated_count else ""
        self.status_label.configure(text=snapshot.status_text + undated)
        self.added_tasks_panel.refresh(snapshot.rows)
        if not self._editing:
            options = self.page_controller.editor_options()
            if options.ok:
                self.form.set_options(options.value)
        self.calendar.draw(snapshot, self.page_controller.selected_date)
        self._show_selection()

    def _filter_project(self, _label: str) -> None:
        if self._unfiltered is not None:
            self._render(self._unfiltered)

    def _show_selection(self) -> None:
        selected = self.page_controller.selected_date
        self.start_date_var.set(selected.isoformat())
        self.calendar.set_selected(selected)
        cell = self.snapshot.day(selected) if self.snapshot is not None else None
        notes = []
        if cell is not None:
            if cell.is_today:
                notes.append("today")
            elif cell.is_past:
                notes.append("past")
            if cell.freshness_label:
                notes.append(cell.freshness_label.lower())
        self.details_title.configure(text=f"{selected:%A}, {selected:%B} {selected.day}, {selected.year}"
                                          + (f" · {', '.join(notes)}" if notes else ""))
        if cell is None or not cell.items:
            text = "Nothing planned for this date yet. Add a task below, or Open Day."
        else:
            text = "\n".join(item.text for item in cell.items)
        self.details_label.configure(text=text)

    # ----------------------------- Busy state -----------------------------

    def _refuse_while_busy(self) -> bool:
        return self._busy

    def _confirm(self, title: str, message: str, confirm_text: str, *, danger: bool = False) -> bool:
        return ask_confirm(self, title=title, message=message, confirm_text=confirm_text, danger=danger)
