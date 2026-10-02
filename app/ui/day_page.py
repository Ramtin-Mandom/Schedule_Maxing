"""
app/ui/day_page.py

The desktop Day Schedule (Milestone 4, Prompt 4). Every callback is a thin
call into DayScheduleController (app/ui/day_controller.py) followed by a
redraw from the DaySnapshot it returns -- a fresh read of committed SQLite
state -- so nothing here schedules, validates or stores anything itself.

Layout, top to bottom (one scrolling column; the lower part is two columns
on wide and medium windows, one on narrow ones):

- Header: date navigation (previous / date / Today / next), the planning
  timezone, "Back to Week/Month" when the date was opened from there, and
  the freshness of the saved schedule (Current / Out of date and why).
  The Day page shows today (the computer's date) when it is opened from the
  navigation; another date only when opened for it (Week, Month, Projects,
  Allocation) or moved to with the date controls.
- The Day Window (app/ui/day_window_bar.py): the date's start and end of
  the usable day -- the Settings default, or the date's own override.
- The horizontal timeline (app/ui/day_timeline.py): fixed blocks in their
  category color, scheduled work at its exact minutes, free gaps.
- Available tasks: the date's tasks (and undated ones) that are not on the
  schedule, as buttons that open them for editing, with genuine reasons.
- Add Task (the reusable app/ui/task_editor.TaskEditor), and the actions:
  the Engine choice right beside Make Schedule, Regenerate, Day Preferences,
  Import CSV, Export CSV and Reset Day.
- The scheduled-task board (app/ui/task_status_board.py): Uncompleted |
  Tasks | Completed, for the tasks the saved schedule placed on the date.
  Each move is the placement's TaskExecution changing state
  (app/ui/task_status.py), so it persists and synchronizes; tasks the
  scheduler could not place stay in Available tasks, never Uncompleted.

Generation and engine saves run in background workers; while one runs the
controls that could change its inputs are disabled, and a result that
arrives after the date (or the account workspace) changed is not shown as
this date's result.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from app.ui.paint_widgets import AppScrollableFrame

from app.planning.csv_import import ImportMode
from app.planning.workflow import Freshness
from app.ui import theme
from app.ui.background import ControllerResult, run_in_background
from app.ui.components import AppButton, Card, ChoiceDialog, LabeledSelect, Notice, SectionTitle, ask_confirm, font, focus_target
from app.ui.day_controller import DayRun, DayScheduleController, DaySnapshot, TimelineItem, engine_label
from app.ui.day_timeline import DayTimeline
from app.ui.day_window import DayWindowController
from app.ui.day_window_bar import DayWindowActions, DayWindowBar
from app.ui.layout import Coalescer
from app.ui.preferences_editor import DayPreferencesDialog
from app.ui.schedule_page_controller import day_label
from app.ui.shell_state import LayoutMode
from app.ui.task_actions import TaskFormActions
from app.ui.task_editor import TaskEditor
from app.ui.task_status import TaskStatusController
from app.ui.task_status_board import TaskStatusBoard

_FRESHNESS_TONE = {Freshness.CURRENT: "success", Freshness.STALE: "warning", Freshness.NONE: "info"}
_CHIP_COLUMNS = {LayoutMode.WIDE: 3, LayoutMode.MEDIUM: 2, LayoutMode.NARROW: 1}
_RETURN_NAMES = {"week": "Week", "month": "Month"}
#: The header's text width per layout (a long status must never widen a narrow page).
_HEADER_WRAP = {LayoutMode.WIDE: 760, LayoutMode.MEDIUM: 560, LayoutMode.NARROW: 400}


class DaySchedulePage(TaskFormActions, DayWindowActions, ctk.CTkFrame):
    mode_name = "day"

    def __init__(
        self,
        parent: tk.Widget,
        page_controller: DayScheduleController,
        execution_controller=None,
        productivity_controller=None,
        *,
        on_anchor_changed: Callable[[str, date], None] | None = None,
        on_return: Callable[[str], None] | None = None,
        return_context: tuple[str, date] | None = None,
        background_io: bool = False,
    ) -> None:
        super().__init__(parent, fg_color=theme.APP_BG, corner_radius=0)
        self.page_controller = page_controller
        self.background_io = background_io
        self.number_of_days = 1
        self.execution_controller = execution_controller
        self.productivity_controller = productivity_controller
        self._on_anchor_changed = on_anchor_changed
        self._on_return = on_return
        self.return_context = return_context
        self.snapshot: DaySnapshot | None = None
        self.last_run: DayRun | None = None
        self._editing = None
        self._busy = False
        self.layout: LayoutMode | None = None
        self._engine_labels = {option.label: option for option in page_controller.engine_options()}
        self.chips: list[AppButton] = []
        self.day_window = DayWindowController(page_controller.planning)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build_header()
        self.body = AppScrollableFrame(self, fg_color="transparent", scrollbar_button_color=theme.SECONDARY_HOVER)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        self.window_bar = DayWindowBar(self.body, on_apply=self.apply_day_window,
                                       on_default=self.use_default_day_window)
        self.window_bar.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_M))
        self.schedule_canvas = DayTimeline(self.body, on_edit=lambda item: self.edit_ref(item.ref),
                                           on_remove=lambda item: self.remove_item(item),
                                           on_release=lambda item: self.release_item(item))
        self.schedule_canvas.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_M))
        self._build_available()
        self._build_lower()
        self.set_layout(LayoutMode.WIDE)
        self.reload()
        self._reset_editor()

    def destroy(self) -> None:
        refresh = getattr(self, "_chip_refresh", None)
        if refresh is not None:
            refresh.cancel()
        super().destroy()

    # ----------------------------- Building -----------------------------

    def _build_header(self) -> None:
        self.header = header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(theme.SPACE_L, 8))
        header.columnconfigure(0, weight=1)
        top = ctk.CTkFrame(header, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="Day Schedule", font=font(theme.SIZE_TITLE, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, sticky="w")
        self.freshness_badge = ctk.CTkLabel(top, text="", font=font(theme.SIZE_SMALL, "bold"), corner_radius=10,
                                            padx=10)
        self.freshness_badge.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.back_button = AppButton(top, "Back", self.go_back, variant="ghost", height=30)
        self.back_button.grid(row=0, column=2, sticky="e")
        self.back_button.grid_remove()

        self.nav = nav = ctk.CTkFrame(header, fg_color="transparent")
        nav.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.prev_button = AppButton(nav, "‹ Previous day", lambda: self.shift_date(-1), variant="secondary",
                                     height=32, width=120)
        self.prev_button.grid(row=0, column=0, padx=(0, 6))
        date_label = ctk.CTkLabel(nav, text="Date:", font=font(theme.SIZE_BODY), text_color=theme.TEXT_MUTED)
        date_label.grid(row=0, column=1)
        self.start_date_var = tk.StringVar(value=self.page_controller.anchor_date.isoformat())
        self.start_date_entry = ctk.CTkEntry(nav, textvariable=self.start_date_var, width=110, height=32,
                                             fg_color=theme.INPUT_BG, border_color=theme.CARD_BORDER,
                                             text_color=theme.TEXT_PRIMARY)
        self.start_date_entry.grid(row=0, column=2, padx=(6, 4))
        self.start_date_entry.bind("<Return>", lambda _e: self.apply_start_date(), add="+")
        date_label.bind("<Button-1>", lambda _e: self.start_date_entry.focus_set(), add="+")
        self.go_button = AppButton(nav, "Go", self.apply_start_date, width=48, height=32)
        self.go_button.grid(row=0, column=3)
        self.today_button = AppButton(nav, "Today", self.go_today, variant="secondary", width=70, height=32)
        self.today_button.grid(row=0, column=4, padx=(6, 0))
        self.next_button = AppButton(nav, "Next day ›", lambda: self.shift_date(1), variant="secondary",
                                     height=32, width=100)
        self.next_button.grid(row=0, column=5, padx=(6, 0))
        self.range_label = ctk.CTkLabel(header, text="", font=font(theme.SIZE_BODY), text_color=theme.TEXT_MUTED,
                                        anchor="w")
        self.range_label.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.status_label = ctk.CTkLabel(header, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
                                         anchor="w", justify="left", wraplength=760)
        self.status_label.grid(row=3, column=0, sticky="w", pady=(2, 0))

    def _build_available(self) -> None:
        self.available_card = card = Card(self.body)
        card.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_M))
        card.columnconfigure(0, weight=1)
        SectionTitle(card, "Available tasks", "Tasks for this date (or any date) that are not on the schedule. "
                                              "Select one to edit it.", wraplength=620).grid(
            row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 6))
        self.available_note = ctk.CTkLabel(card, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
                                           anchor="w", justify="left", wraplength=760)
        self.available_note.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        self.chip_area = AppScrollableFrame(card, fg_color="transparent", height=110)
        self.chip_area.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_S, pady=(4, theme.SPACE_M))
        self.chip_area.grid_remove()  # shown only when there is something to list
        self._available_tasks = []
        self._chip_rows = 0
        self._chip_scale = self.chip_area._get_widget_scaling()
        self._chip_refresh = Coalescer(self.chip_area, self._render_available)
        canvas = self.chip_area._parent_canvas

        def scrolled(first, last):
            self.chip_area._scrollbar.set(first, last)
            self._chip_refresh.request()

        canvas.configure(yscrollcommand=scrolled)
        canvas.bind("<Configure>", lambda event: self._chip_refresh.request(), add="+")

    def _build_lower(self) -> None:
        self.lower = lower = ctk.CTkFrame(self.body, fg_color="transparent")
        lower.grid(row=3, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_XL))
        self.form = TaskEditor(lower, on_submit=self.submit_task, on_cancel=self.cancel_edit,
                               productivity_controller=self.productivity_controller)
        self.side = side = ctk.CTkFrame(lower, fg_color="transparent")
        side.columnconfigure(0, weight=1)
        self._build_actions(side)
        self._build_status_board(lower)

    def _build_actions(self, parent) -> None:
        self.actions_card = card = Card(parent)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        SectionTitle(card, "Make the schedule", "Places this date's flexible tasks around its fixed blocks.",
                     wraplength=420).grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 8))
        run_row = ctk.CTkFrame(card, fg_color="transparent")
        run_row.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        run_row.columnconfigure(1, weight=1)
        labels = list(self._engine_labels)
        self.engine_select = LabeledSelect(run_row, "Engine", labels, command=self._engine_chosen, width=160)
        self.engine_select.grid(row=0, column=0, sticky="sw", padx=(0, 8))
        self.make_schedule_button = AppButton(run_row, "Make Schedule", self.make_schedule, height=theme.CONTROL_HEIGHT)
        self.make_schedule_button.grid(row=0, column=1, sticky="sew")
        self.engine_note = ctk.CTkLabel(card, text="", font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED,
                                        anchor="w", justify="left", wraplength=420)
        self.engine_note.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_L, pady=(4, 0))
        engine_row = ctk.CTkFrame(card, fg_color="transparent")
        engine_row.grid(row=3, column=0, sticky="ew", padx=theme.SPACE_L, pady=(4, 0))
        self.use_default_engine_button = AppButton(engine_row, "Use default engine", self.reset_engine,
                                                   variant="ghost", height=30)
        self.use_default_engine_button.grid(row=0, column=0, padx=(0, 6))
        self.regenerate_button = AppButton(engine_row, "Regenerate...", self.regenerate, variant="secondary",
                                           height=30)
        self.regenerate_button.grid(row=0, column=1)

        self.notice = Notice(card, wraplength=420)
        self.notice.grid(row=4, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.notice.hide()
        self.reasons_label = ctk.CTkLabel(card, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_PRIMARY,
                                          anchor="w", justify="left", wraplength=420)
        self.reasons_label.grid(row=5, column=0, sticky="ew", padx=theme.SPACE_L, pady=(4, 0))

        ctk.CTkFrame(card, height=1, fg_color=theme.CARD_BORDER).grid(row=6, column=0, sticky="ew",
                                                                       padx=theme.SPACE_L, pady=10)
        data = ctk.CTkFrame(card, fg_color="transparent")
        data.grid(row=7, column=0, sticky="ew", padx=theme.SPACE_L, pady=(0, theme.SPACE_L))
        data.columnconfigure((0, 1), weight=1, uniform="data")
        self.preferences_button = AppButton(data, "Day Preferences...", self.open_preferences, variant="neutral")
        self.preferences_button.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.import_button = AppButton(data, "Import CSV...", self.import_csv, variant="neutral")
        self.import_button.grid(row=1, column=0, sticky="ew", padx=(0, 3))
        self.export_button = AppButton(data, "Export CSV...", self.export_csv, variant="neutral")
        self.export_button.grid(row=1, column=1, sticky="ew", padx=(3, 0))
        self.reset_button = AppButton(data, "Reset Day...", self.reset_day, variant="danger")
        self.reset_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.upload_button = self.import_button  # the previous name of the import action

    def _build_status_board(self, parent) -> None:
        self.status_controller = (TaskStatusController(self.execution_controller)
                                  if self.execution_controller is not None else None)
        self.status_board = TaskStatusBoard(parent, on_move=self.move_task)
        self.status_board.observe_viewport(self.body._parent_canvas)

        def scrolled(first, last):
            self.body._scrollbar.set(first, last)
            self.status_board.viewport_updates.request()

        self.body._parent_canvas.configure(yscrollcommand=scrolled)
        self._board_token = 0

    # ----------------------------- Layout -----------------------------

    def set_layout(self, mode: LayoutMode) -> None:
        """Re-grid the lower workspace for `mode` (called by the shell only when the mode changes)."""
        if mode == self.layout:
            return
        self.layout = mode
        lower = self.lower
        self.form.grid_forget()
        self.side.grid_forget()
        self.status_board.grid_forget()
        for index in range(2):
            lower.columnconfigure(index, weight=0, minsize=0)
        pad = theme.SPACE_XL if mode != LayoutMode.NARROW else theme.SPACE_M
        for widget in (self.header, self.window_bar, self.schedule_canvas, self.available_card, self.lower):
            widget.grid_configure(padx=pad)
        self.window_bar.set_layout(mode)
        self.status_label.configure(wraplength=_HEADER_WRAP[mode])
        self.status_board.set_layout(mode)
        if mode == LayoutMode.NARROW:
            lower.columnconfigure(0, weight=1)
            self.form.grid(row=0, column=0, sticky="new", pady=(0, theme.SPACE_M))
            self.side.grid(row=1, column=0, sticky="new")
            self.status_board.grid(in_=lower, row=2, column=0, sticky="new", pady=(theme.SPACE_M, 0))
        else:
            lower.columnconfigure(0, minsize=340)
            lower.columnconfigure(1, weight=1)
            self.form.grid(row=0, column=0, sticky="new", padx=(0, theme.SPACE_M))
            self.side.grid(row=0, column=1, sticky="new")
            if mode == LayoutMode.WIDE:  # the three columns sit right under the actions, beside the form
                self.status_board.grid(in_=self.side, row=1, column=0, sticky="new", pady=(theme.SPACE_M, 0))
            else:  # medium: full width below, so each column keeps a readable width
                self.status_board.grid(in_=lower, row=1, column=0, columnspan=2, sticky="new",
                                       pady=(theme.SPACE_M, 0))
        self._place_chips()

    def show_panel(self, key: str) -> None:
        """Bring the task form into view (edits start there); the Day page has no panel switcher."""
        if key == "input":
            try:
                self.body._parent_canvas.yview_moveto(max(0.0, self.lower.winfo_y() / max(1, self.body.winfo_height())))
            except (AttributeError, tk.TclError):
                pass

    def on_appearance_changed(self) -> None:
        self.schedule_canvas.request_redraw()
        if self.snapshot is not None:
            self._show_freshness(self.snapshot)

    # ----------------------------- Loading and dates -----------------------------

    def reload(self) -> None:
        """Re-read this date from storage (startup, page switch, after any failure)."""
        token = self._next_load()

        def done(result) -> None:
            if token != self._load_token:
                return  # a newer load (another date) was started meanwhile
            if result.ok:
                self._render(result.value)
            else:
                self._load_failed(result)

        self._io(self.page_controller.load, done, blocking=False)

    def on_show(self) -> None:
        """Called when the page is shown: other pages, imports or a sync may have changed shared data."""
        if not self._busy:
            self.reload()

    def apply_start_date(self) -> None:
        self._go_to(self.start_date_var.get())

    def shift_date(self, days: int) -> None:
        self._go_to(self.page_controller.anchor_date + timedelta(days=days))

    def go_today(self) -> None:
        self._go_to(self.page_controller.today())

    def show_today(self) -> None:
        """Opened from the navigation: the Day page is today (the computer's date), without a way back."""
        if self._busy:
            return
        today = self.page_controller.today()
        if today == self.page_controller.anchor_date and self.return_context is None:
            return
        self.return_context = None
        self._go_to(today)

    def _window_day(self) -> date:
        return self.page_controller.anchor_date

    def open_date(self, day: date, *, return_to: tuple[str, date] | None = None) -> None:
        """Show `day`; with return_to=(page, its date) a Back button returns to that Week/Month view."""
        if self._busy:
            return
        self.return_context = return_to
        self._go_to(day)

    def go_back(self) -> None:
        if self.return_context is not None and self._on_return is not None:
            self._on_return(self.return_context[0])

    def _go_to(self, value) -> None:
        if self._refuse_while_busy():
            return
        token = self._next_load()

        def done(result) -> None:
            if token == self._load_token:
                self._went_to(result)

        self._io(lambda: self.page_controller.set_anchor_date(value), done, blocking=False)

    def _went_to(self, result) -> None:
        if not result.ok:
            messagebox.showerror("Invalid Date", result.error or "Unknown error.", parent=self)
            self.start_date_var.set(self.page_controller.anchor_date.isoformat())
            return
        self._leave_edit_mode()
        self.last_run = None
        self.notice.hide()
        self.reasons_label.configure(text="")
        self._render(result.value)
        self._reset_editor()

    # ----------------------------- Engine -----------------------------

    def _engine_chosen(self, label: str) -> None:
        option = self._engine_labels.get(label)
        snapshot = self.snapshot
        if option is None or snapshot is None or snapshot.engine is None:
            return
        if self._busy:
            self._show_engine(snapshot)
            return
        if option.mode == snapshot.engine.effective:
            return
        self._save_engine(option.mode, f"The engine for {day_label(snapshot.day)} is now {option.label}.")

    def reset_engine(self) -> None:
        snapshot = self.snapshot
        if snapshot is None or snapshot.engine is None or not snapshot.engine.overridden or self._refuse_while_busy():
            return
        self._save_engine(None, f"{day_label(snapshot.day)} uses the default engine "
                                f"({engine_label(snapshot.engine.inherited)}) again.")

    def _save_engine(self, mode, done: str) -> None:
        version = self.snapshot.preference_version
        self._set_busy(True, saving=True)

        def work() -> ControllerResult[DaySnapshot]:
            return self.page_controller.set_engine(mode, expected_version=version)

        def finish(result: ControllerResult[DaySnapshot]) -> None:
            self._set_busy(False)
            if result.value is not None:
                self._render(result.value)
            if not result.ok:
                # The dropdown shows the stored engine again; nothing will be generated with the failed choice.
                self.notice.show("error", f"The engine was not changed: {result.error}")
                if result.value is None:
                    self.reload()
                return
            extra = ""
            if result.value.freshness == Freshness.STALE:
                extra = " The saved schedule is now out of date; Make Schedule keeps the work that still fits."
            self.notice.show("success", done + extra)

        if not run_in_background(self, work, finish):
            self._set_busy(False)

    # ----------------------------- Generation -----------------------------

    def make_schedule(self) -> None:
        self._start_run(self.page_controller.make_schedule_for)

    def regenerate(self) -> None:
        if self._refuse_while_busy():
            return
        message = (f"Regenerate {day_label(self.page_controller.anchor_date)} from scratch? Scheduled work that has "
                   "not started is replaced; started or finished work stays where it is.")
        if self._confirm("Regenerate?", message, "Regenerate"):
            self._start_run(self.page_controller.regenerate_for)

    def _start_run(self, operation: Callable[[date], ControllerResult[DayRun]]) -> None:
        if self._refuse_while_busy():
            return
        day = self.page_controller.anchor_date
        self._set_busy(True)
        if not run_in_background(self, lambda: operation(day), lambda result: self._on_run_done(day, result)):
            self._set_busy(False)

    def _on_run_done(self, day: date, result: ControllerResult[DayRun]) -> None:
        self._set_busy(False)
        run = result.value
        if day != self.page_controller.anchor_date or run is None or run.snapshot is None:
            self.reload()
            if not result.ok:
                self.notice.show("error", result.error or "Scheduling failed.")
            return
        self.last_run = run
        self._render(run.snapshot)
        tone = {"generated": "success", "already_current": "info", "nothing_placed": "warning",
                "needs_regeneration": "warning", "failed": "error"}[run.status]
        self.notice.show(tone, run.message)
        lines = run.problems or run.reasons
        self.reasons_label.configure(text="\n".join(f"• {line}" for line in lines))
        if run.status == "needs_regeneration":
            message = run.message + "\n\n" + "\n".join(f"- {line}" for line in run.problems) + "\n\nRegenerate now?"
            if self._confirm("Saved Work No Longer Fits", message, "Regenerate"):
                self._start_run(self.page_controller.regenerate_for)

    # ----------------------------- Day data -----------------------------

    def open_preferences(self) -> None:
        if self._refuse_while_busy():
            return
        self.preferences_dialog = DayPreferencesDialog(self, self.page_controller, on_closed=self.reload,
                                                       background_io=self.background_io)
        self.preferences_dialog.present()

    def reset_day(self) -> None:
        if self._refuse_while_busy():
            return
        self._io(self.page_controller.reset_plan, self._reset_planned)

    def _reset_planned(self, plan) -> None:
        if not plan.ok:
            self.notice.show("error", plan.error or "The reset could not be prepared.")
            return
        if plan.value.blocked:
            self.notice.show("error", plan.value.message)
            return
        if not self._confirm("Reset Day?", plan.value.message, "Reset Day", danger=True):
            self.notice.show("info", "Reset cancelled; nothing was deleted.")
            return
        self._io(lambda: self.page_controller.reset_day(plan.value), self._day_reset)

    def _day_reset(self, result) -> None:
        if result.value is not None:
            self._render(result.value)
        if not result.ok:
            self.notice.show("error", result.error or "The reset failed; nothing was deleted.")
            return
        self._leave_edit_mode()
        self._reset_editor()
        self.last_run = None
        self.reasons_label.configure(text="")
        self.notice.show("success", f"{day_label(self.page_controller.anchor_date)} was reset. Your default "
                                    "preferences, undated tasks and execution history were kept.")

    def import_csv(self) -> None:
        if self._refuse_while_busy():
            return
        path = filedialog.askopenfilename(title="Import Planning CSV", parent=self,
                                          filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        self._io(lambda: self.page_controller.csv_plan(path), lambda plan: self._csv_planned(path, plan))

    def _csv_planned(self, path: str, plan) -> None:
        if not plan.ok:
            self.notice.show("error", plan.error or "The file cannot be imported.")
            return
        if plan.value.kind == "legacy":
            ChoiceDialog(
                self, title="Legacy CSV", prompt=plan.value.summary + "\n\nImport it as a legacy file?",
                options=[(ImportMode.APPEND.value, "Append: add its tasks and fixed blocks as new records"),
                         (ImportMode.REPLACE.value, "Replace: clear the dates it covers first, then add it")],
                on_choose=lambda mode: self._apply_legacy(plan.value, ImportMode(mode)),
            )
            return
        if not self._confirm("Import CSV?", f"{Path(path).name}: {plan.value.summary}\n\nImport it now?", "Import"):
            self.notice.show("info", "Import cancelled; nothing was changed.")
            return
        self._apply_csv(plan.value)

    def _apply_legacy(self, plan, mode: ImportMode) -> None:
        if self._confirm("Import Legacy CSV?", self.page_controller.import_description(mode) + "\n\nContinue?",
                         "Import", danger=mode == ImportMode.REPLACE):
            self._apply_csv(plan, legacy_mode=mode)

    def _apply_csv(self, plan, *, legacy_mode: ImportMode | None = None) -> None:
        self._io(lambda: self.page_controller.apply_csv(plan, legacy_mode=legacy_mode), self._csv_applied)

    def _csv_applied(self, result) -> None:
        if not result.ok:
            if result.value is not None:
                self._render(result.value)
            self.notice.show("error", result.error or "The import failed; nothing was changed.")
            return
        self._leave_edit_mode()
        self._render(result.value.snapshot)
        self._reset_editor()
        self.notice.show("success", result.value.summary)

    def export_csv(self) -> None:
        day = self.page_controller.anchor_date
        path = filedialog.asksaveasfilename(title="Export Planning CSV", defaultextension=".csv", parent=self,
                                            initialfile=f"planning_{day.isoformat()}.csv",
                                            filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        self._io(lambda: self.page_controller.export_csv(path), self._csv_exported)

    def _csv_exported(self, result) -> None:
        if not result.ok:
            self.notice.show("error", result.error or "The export failed.")
            return
        exported = result.value
        self.notice.show("success", f"Exported {exported.tasks} task(s), {exported.fixed_blocks} fixed block(s) and "
                                    f"{exported.placements} scheduled entr(ies) to {exported.path} (CSV format v2).")

    def remove_item(self, item: TimelineItem) -> None:
        self.remove_ref(item.ref)

    def release_item(self, item: TimelineItem) -> None:
        """Release a manual placement's intent (in the background): it stays put; Make Schedule may replace it."""
        if self._refuse_while_busy():
            return
        day = self.page_controller.anchor_date
        self._set_busy(True)

        def finish(result: ControllerResult[DaySnapshot]) -> None:
            self._set_busy(False)
            if day != self.page_controller.anchor_date:
                self.reload()
                return
            if result.value is not None:
                self._render(result.value)
            if not result.ok:
                self.notice.show("error", result.error or "The placement could not be released.")
                return
            self.notice.show("success", f"{item.name} was released: it stays where it is, but Make Schedule may "
                                        "now replace it like generated work.")

        if not run_in_background(self, lambda: self.page_controller.release_manual_placement(item), finish):
            self._set_busy(False)

    # ----------------------------- Rendering -----------------------------

    def _render(self, snapshot: DaySnapshot) -> None:
        self.snapshot = snapshot
        self.start_date_var.set(snapshot.start_date.isoformat())
        if self._on_anchor_changed is not None:
            self._on_anchor_changed(self.mode_name, snapshot.start_date)
        self.range_label.configure(text=f"{snapshot.start_date:%A, %B} {snapshot.start_date.day}, "
                                        f"{snapshot.start_date.year}   ·   times in {snapshot.timezone}")
        self._show_freshness(snapshot)
        self._show_return()
        if not self._editing:
            self._refresh_options()
        self.schedule_canvas.draw(snapshot)
        self._show_engine(snapshot)
        self._show_available(snapshot)
        self.regenerate_button.configure(
            state="normal" if snapshot.freshness != Freshness.NONE and not self._busy else "disabled")
        self._refresh_status_board(snapshot)
        self._refresh_day_window()

    # ----------------------------- Scheduled-task board -----------------------------

    def _refresh_status_board(self, snapshot: DaySnapshot) -> None:
        """Re-read the statuses of the date's saved placements (only the newest read is shown)."""
        if self.status_controller is None:
            return
        self._board_token += 1
        token, day, executables = self._board_token, snapshot.day, list(snapshot.executables)

        def done(result) -> None:
            if token != self._board_token:
                return
            if result.ok:
                self.status_board.render(result.value)
            else:
                self.status_board.notice.show("error", result.error or "The task statuses could not be read.")

        self._io(lambda: self.status_controller.board(day, executables), done, blocking=False)

    def move_task(self, card, target) -> None:
        """Persist one card's move to another column, then redraw the board from what was saved."""
        if self.status_controller is None or self._refuse_while_busy():
            return
        self.status_board.set_busy(True)

        def done(result) -> None:
            self.status_board.set_busy(False)
            if result.ok:
                self.status_board.notice.hide()
            else:
                self.status_board.notice.show("error", result.error or "The status was not changed.")
            if self.snapshot is not None:
                self._refresh_status_board(self.snapshot)

        self._io(lambda: self.status_controller.move(card, target), done)

    def _show_freshness(self, snapshot: DaySnapshot) -> None:
        tone = theme.TONES[_FRESHNESS_TONE[snapshot.freshness]]
        self.freshness_badge.configure(text=snapshot.freshness_label, fg_color=tone.background,
                                       text_color=tone.foreground)
        engine = f" Engine: {snapshot.engine.summary}." if snapshot.engine is not None else ""
        self.status_label.configure(text=f"{snapshot.freshness_label}: {snapshot.freshness_detail}{engine}")

    def _show_return(self) -> None:
        context = self.return_context
        if context is None or self._on_return is None:
            self.back_button.grid_remove()
            return
        name = _RETURN_NAMES.get(context[0], context[0].title())
        self.back_button.configure(text=f"‹ Back to {name} ({day_label(context[1])})")
        self.back_button.grid()

    def _show_engine(self, snapshot: DaySnapshot) -> None:
        engine = snapshot.engine
        if engine is None:
            return
        self.engine_select.variable.set(engine.label)
        option = next((o for o in self._engine_labels.values() if o.mode == engine.effective), None)
        where = "set for this date" if engine.overridden else "the default"
        self.engine_note.configure(text=f"{engine.label} ({where}): {option.explanation if option else ''}")
        self.use_default_engine_button.configure(
            state="normal" if engine.overridden and not self._busy else "disabled")

    def _show_available(self, snapshot: DaySnapshot) -> None:
        self._available_tasks = list(snapshot.unplaced)
        self._place_chips()
        if self._available_tasks:
            self.chip_area.grid()
        else:
            self.chip_area.grid_remove()
        notes = []
        if not snapshot.unplaced:
            notes.append("Every task for this date is on the schedule." if snapshot.timeline
                         else "No tasks for this date yet. Add one below.")
        if snapshot.unexplained_count:
            notes.append(f"The saved schedule could not place {snapshot.unexplained_count} task(s). Their reasons "
                         "were not stored, so they are not shown; they appear after the next run that changes the "
                         "schedule.")
        self.available_note.configure(text=" ".join(notes))

    def _render_available(self) -> None:
        """Keep only the viewport and one spare row of large lists as Tk buttons."""
        if self._chip_scale != self.chip_area._get_widget_scaling():
            self._place_chips()
            return
        tasks = self._available_tasks
        columns = _CHIP_COLUMNS.get(self.layout, 1)
        first, last = 0, len(tasks)
        if len(tasks) > 24:
            canvas = self.chip_area._parent_canvas
            row_height = round(50 * self.chip_area._get_widget_scaling())
            row = max(0, int(canvas.canvasy(0) / row_height))
            first = max(0, row - 1) * columns
            last = min(len(tasks), (row + int(canvas.winfo_height() / row_height) + 3) * columns)
        previous = self.chips
        by_ref = {chip.task.ref: chip for chip in previous}
        chips = []
        for index, task in enumerate(tasks[first:last], start=first):
            text = task.text + (f"\n{task.reason}" if task.reason else "")
            chip = by_ref.pop(task.ref, None)
            if chip is None:
                chip = AppButton(self.chip_area, text, lambda ref=task.ref: self.edit_ref(ref), variant="secondary",
                                 height=44, font=font(theme.SIZE_SMALL))
                target = focus_target(chip)
                for key, direction in (("Up", -1), ("Down", 1)):
                    target.bind(f"<{key}>", lambda event, chip=chip, direction=direction:
                                self._focus_available(chip.task.ref, direction), add="+")
            else:
                # RowRef equality deliberately ignores version; refresh the edit
                # precondition even when this task's displayed text is unchanged.
                if chip.cget("text") != text:
                    chip.configure(text=text)
                if chip.task.ref.version != task.ref.version:
                    chip.configure(command=lambda ref=task.ref: self.edit_ref(ref))
            chip.task = task
            cell = (index // columns, index % columns)
            if getattr(chip, "_task_cell", None) != cell:
                chip.grid(row=cell[0], column=cell[1], sticky="ew", padx=4, pady=3)
                chip._task_cell = cell
            chips.append(chip)
        for chip in by_ref.values():
            chip.destroy()
        self.chips = chips

    def _focus_available(self, ref, direction):
        """Arrow navigation can reach tasks beyond the currently instantiated rows."""
        index = next((i for i, task in enumerate(self._available_tasks) if task.ref == ref), None)
        if index is None:
            return "break"
        index = max(0, min(len(self._available_tasks) - 1, index + direction))
        task = self._available_tasks[index]
        if not any(chip.task.ref == task.ref for chip in self.chips):
            columns = _CHIP_COLUMNS.get(self.layout, 1)
            rows = (len(self._available_tasks) + columns - 1) // columns
            self.chip_area._parent_canvas.yview_moveto((index // columns) / max(1, rows))
            self._render_available()
        for chip in self.chips:
            if chip.task.ref == task.ref:
                focus_target(chip).focus_set()
                break
        return "break"

    def _place_chips(self) -> None:
        columns = _CHIP_COLUMNS.get(self.layout, 1)
        for column in range(3):
            self.chip_area.columnconfigure(column, weight=1 if column < columns else 0, uniform="chip")
        virtual = len(self._available_tasks) > 24
        self._chip_scale = self.chip_area._get_widget_scaling()
        rows = (len(self._available_tasks) + columns - 1) // columns if virtual else 0
        height = round(50 * self._chip_scale)
        for row in range(max(rows, self._chip_rows)):
            self.chip_area.rowconfigure(row, minsize=height if row < rows else 0)
        self._chip_rows = rows
        # Retain the full scroll extent without constructing offscreen controls.
        tk.Frame.grid_propagate(self.chip_area, not virtual)
        if virtual:
            tk.Frame.configure(self.chip_area, height=max(1, rows * height))
        self._render_available()

    # ----------------------------- Busy state -----------------------------

    def _set_busy(self, busy: bool, *, saving: bool = False) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.make_schedule_button.configure(
            state=state, text=("Saving engine..." if saving else "Scheduling...") if busy else "Make Schedule")
        self.engine_select.menu.configure(state=state)
        for button in (self.preferences_button, self.import_button, self.reset_button, self.prev_button,
                       self.next_button, self.today_button, self.go_button):
            button.configure(state=state)
        self.window_bar.set_enabled(not busy)
        self.status_board.set_busy(busy)
        self.regenerate_button.configure(state="disabled" if busy or self.snapshot is None
                                         or self.snapshot.freshness == Freshness.NONE else "normal")
        self.use_default_engine_button.configure(
            state="normal" if not busy and self.snapshot is not None and self.snapshot.engine is not None
            and self.snapshot.engine.overridden else "disabled")

    def _refuse_while_busy(self) -> bool:
        if self._busy:
            messagebox.showinfo("Please Wait", "The schedule for this date is still being made or saved.", parent=self)
        return self._busy

    def _confirm(self, title: str, message: str, confirm_text: str, *, danger: bool = False) -> bool:
        return ask_confirm(self, title=title, message=message, confirm_text=confirm_text, danger=danger)
