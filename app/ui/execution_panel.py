"""
execution_panel.py

The Day page's Execute tab: pick a saved placement of the date, see where it
stands, and Start / Pause / Resume / Finish / Skip / Cancel it or Reschedule
it (Milestone 5), with a live active-time display and optional feedback on
Finish/Skip.

Everything here is presentation. The rules live in Tk-free code:
app/ui/execution_workflow.py (state now, legal actions, overdue/late,
planned/actual/active texts), ExecutionController.perform (the lifecycle
action, creating the execution only on the first explicit action) and
PlanningController.reschedule_placement (the validated move). Database work
runs off the Tk thread (app.ui.background.run_in_background); every result is
a ControllerResult, and failures are explained, never raised into Tk.

Identity and duplicates: items are saved placements (ExecutablePlacement,
keyed by task and placement id -- duplicate names stay distinct). Selecting,
refreshing or navigating only *looks up* executions; nothing is written until
the user acts. While an action runs every button is disabled, so a repeated
click cannot start a second one; a change made elsewhere meanwhile is
reported as a conflict and the saved state is reloaded.
"""

from __future__ import annotations

import tkinter as tk
import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone
from tkinter import messagebox

import customtkinter as ctk

from app.execution.models import ExecutionStatus, TaskExecution
from app.ui import theme
from app.ui.background import ControllerResult, run_in_background
from app.ui.execution_controller import ExecutionController
from app.ui.execution_workflow import ACTION_LABELS, ACTIONS, STATUS_LABELS, ExecutionItemView
from app.ui.feedback_dialog import FeedbackDialog
from app.ui.schedule_page_controller import ExecutablePlacement

# Kept for callers that import it; the labels live in execution_workflow.
_STATUS_LABELS = STATUS_LABELS

_TICK_INTERVAL_MS = 1000
_NO_TASKS_LABEL = "(no saved schedule -- run Make Schedule)"


def parse_reschedule_target(text: str, default_day: date) -> tuple[date, int]:
    """'HH:MM' (same date) or 'YYYY-MM-DD HH:MM' -> (date, minute of day). ValueError when unreadable."""
    parts = text.strip().split()
    if len(parts) == 2:
        day, clock = date.fromisoformat(parts[0]), parts[1]
    elif len(parts) == 1:
        day, clock = default_day, parts[0]
    else:
        raise ValueError("Enter HH:MM, or YYYY-MM-DD HH:MM.")
    hour, _, minute = clock.partition(":")
    if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ValueError("Enter the start time as HH:MM (24-hour).")
    return day, int(hour) * 60 + int(minute)


class ExecutionPanel(ctk.CTkFrame):
    """Task selector + lifecycle and reschedule controls + planned/actual/active details + the date's statuses."""

    def __init__(
        self,
        parent: tk.Widget,
        execution_controller: ExecutionController,
        *,
        planning_controller=None,
        on_schedule_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent, fg_color="transparent")
        self._controller = execution_controller
        self._planning = planning_controller
        self._on_schedule_changed = on_schedule_changed

        self._tasks: list[ExecutablePlacement] = []
        self._by_label: dict[str, ExecutablePlacement] = {}
        self._current_task: ExecutablePlacement | None = None
        self._current_execution: TaskExecution | None = None
        self._view: ExecutionItemView | None = None
        self._pending = False
        self._elapsed_base_minutes = 0.0
        self._elapsed_base_time: datetime | None = None
        self._tick_job: str | None = None

        self.columnconfigure(0, weight=1)
        self._build()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_scheduled_tasks(self, tasks: list[ExecutablePlacement]) -> None:
        """Replace the selectable saved placements (after a load/Make Schedule), keeping the selection if possible."""
        previous_id = self._current_task.placement.id if self._current_task is not None else None
        self._tasks = list(tasks)
        self._by_label = {task.label: task for task in self._tasks}
        labels = [task.label for task in self._tasks] or [_NO_TASKS_LABEL]
        self.task_menu.configure(values=labels)
        self._stop_ticking()
        self._current_task = None
        self._current_execution = None
        self._load_day_status()

        if not self._tasks:
            self.task_var.set(labels[0])
            self._render(None, None)
            return

        selected = self._find_by_placement_id(previous_id) or self._tasks[0]
        self.task_var.set(selected.label)
        self._on_task_selected(selected.label)

    @property
    def busy(self) -> bool:
        return self._pending

    def _find_by_placement_id(self, placement_id: uuid.UUID | None) -> ExecutablePlacement | None:
        return next((task for task in self._tasks if task.placement.id == placement_id), None)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.task_var = tk.StringVar(value=_NO_TASKS_LABEL)
        ctk.CTkLabel(self, text="Task", text_color=theme.TEXT_MUTED, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        self.task_menu = ctk.CTkOptionMenu(
            self, variable=self.task_var, values=[self.task_var.get()], command=self._on_task_selected)
        self.task_menu.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 8))

        self.status_label = ctk.CTkLabel(self, text="Select a task to begin.", text_color=theme.TEXT_PRIMARY,
                                         anchor="w", justify="left")
        self.status_label.grid(row=2, column=0, sticky="ew", padx=4, pady=(0, 2))
        self.timing_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_PRIMARY, anchor="w", justify="left",
                                         wraplength=320)
        self.timing_label.grid(row=3, column=0, sticky="ew", padx=4)
        self.detail_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                                         wraplength=320)
        self.detail_label.grid(row=4, column=0, sticky="ew", padx=4)
        self.elapsed_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_MUTED, anchor="w")
        self.elapsed_label.grid(row=5, column=0, sticky="ew", padx=4, pady=(0, 8))

        button_bar = ctk.CTkFrame(self, fg_color="transparent")
        button_bar.grid(row=6, column=0, sticky="ew", padx=4)
        button_bar.columnconfigure((0, 1), weight=1)
        self._action_buttons: dict[str, ctk.CTkButton] = {}
        for index, action in enumerate(ACTIONS):
            danger = action in ("skip", "cancel")
            button = ctk.CTkButton(
                button_bar, text=ACTION_LABELS[action], state="disabled",
                fg_color=theme.DANGER if danger else theme.ACCENT,
                hover_color=theme.DANGER_HOVER if danger else theme.ACCENT_HOVER,
                command=lambda action=action: self._perform_action(action),
            )
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=4, pady=4)
            self._action_buttons[action] = button

        self.blocked_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                                          wraplength=320)
        self.blocked_label.grid(row=7, column=0, sticky="ew", padx=4, pady=(4, 0))
        ctk.CTkLabel(self, text="This date", text_color=theme.TEXT_MUTED, anchor="w").grid(
            row=8, column=0, sticky="ew", padx=4, pady=(10, 0))
        self.day_status_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_PRIMARY, anchor="w",
                                             justify="left", wraplength=320)
        self.day_status_label.grid(row=9, column=0, sticky="ew", padx=4, pady=(0, 6))

    # ------------------------------------------------------------------
    # Loading (lookups only: viewing never writes)
    # ------------------------------------------------------------------

    def _on_task_selected(self, label: str) -> None:
        task = self._by_label.get(label)
        self._current_task = task
        self._stop_ticking()
        if task is None:
            self._render(None, None)
            return
        self.status_label.configure(text=f"{task.label} -- loading...")
        self._set_buttons(())
        self._load(task)

    def _load(self, task: ExecutablePlacement) -> None:
        def work() -> ControllerResult[tuple[TaskExecution | None, ExecutionItemView]]:
            found = self._controller.find_execution_for_placement(task.placement.id)
            if not found.ok:
                return found
            view = self._controller.describe(task.placement, found.value)
            return ControllerResult.success((found.value, view.value)) if view.ok else view

        run_in_background(self, work, lambda result: self._on_loaded(task, result))

    def _on_loaded(self, task: ExecutablePlacement, result) -> None:
        if task is not self._current_task:
            return  # the selection changed while this was loading
        if not result.ok:
            self._show_error(result.error)
            self._render(None, None)
            return
        execution, view = result.value
        self._current_execution = execution
        self._render(execution, view)

    def _load_day_status(self) -> None:
        tasks = list(self._tasks)
        if not tasks:
            self.day_status_label.configure(text="Nothing is saved for this date.")
            return

        def work() -> ControllerResult[list[str]]:
            lines = []
            for task in tasks:
                found = self._controller.find_execution_for_placement(task.placement.id)
                if not found.ok:
                    return found
                view = self._controller.describe(task.placement, found.value)
                if not view.ok:
                    return view
                marker = " (overdue)" if view.value.overdue else " (running late)" if view.value.late else ""
                lines.append(f"{task.label}: {view.value.status_text}{marker}")
            return ControllerResult.success(lines)

        def done(result) -> None:
            if tasks == self._tasks and result.ok:
                self.day_status_label.configure(text="\n".join(result.value))

        run_in_background(self, work, done)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _perform_action(self, action: str) -> None:
        if self._current_task is None or self._pending:
            return
        if self._view is not None and action not in self._view.actions:
            return
        if action == "reschedule":
            self._reschedule()
        elif action in ("complete", "skip"):
            self._collect_feedback_then(action)
        else:
            self._run_transition(action)

    def _collect_feedback_then(self, action: str) -> None:
        title = "Finish Task" if action == "complete" else "Skip Task"

        def on_submit(focus_rating, energy_rating, interruption_count, note) -> None:
            self._run_transition(action, feedback={"focus_rating": focus_rating, "energy_rating": energy_rating,
                                                   "interruption_count": interruption_count, "note": note})

        FeedbackDialog(self, title=title, on_submit=on_submit)

    def _run_transition(self, action: str, *, feedback: dict | None = None) -> None:
        task, known = self._current_task, self._current_execution
        if task is None or self._pending:
            return
        self._begin_pending()
        run_in_background(
            self, lambda: self._controller.perform(task.task, task.placement, action, known, feedback=feedback),
            lambda result: self._on_action_done(task, result),
        )

    def _on_action_done(self, task: ExecutablePlacement, result: ControllerResult) -> None:
        self._pending = False
        if task is not self._current_task:
            return
        if not result.ok:
            self._show_error(result.error)
        # Success or not, show the authoritative saved state (a conflict reloads what is stored now).
        self._load(task)
        self._load_day_status()

    def ask_reschedule_target(self, task: ExecutablePlacement) -> str | None:
        """Ask for the new start ('HH:MM' or 'YYYY-MM-DD HH:MM'); None when cancelled. Tests replace this."""
        dialog = ctk.CTkInputDialog(
            title="Reschedule",
            text=f"Move {task.task.name} (not started) to a new start.\n"
                 f"Enter HH:MM for {task.placement.planned_date}, or YYYY-MM-DD HH:MM "
                 f"({task.placement.timezone}). The duration stays the same.",
        )
        return dialog.get_input()

    def _reschedule(self) -> None:
        task = self._current_task
        if task is None or self._planning is None:
            return
        text = self.ask_reschedule_target(task)
        if not text:
            return
        try:
            day, start_minute = parse_reschedule_target(text, task.placement.planned_date)
        except ValueError as error:
            self._show_error(str(error))
            return
        placement = task.placement
        duration = round((placement.planned_end - placement.planned_start).total_seconds() / 60)
        self._begin_pending()
        run_in_background(
            self,
            lambda: self._planning.reschedule_placement(
                placement.id, expected_version=placement.version, planned_date=day, start_minute=start_minute,
                duration_minutes=duration, timezone_name=placement.timezone),
            lambda result: self._on_rescheduled(task, result),
        )

    def _on_rescheduled(self, task: ExecutablePlacement, result: ControllerResult) -> None:
        self._pending = False
        if not result.ok:
            self._show_error(result.error)
        # The schedule changed (or may have changed elsewhere): the page re-reads it from storage.
        if self._on_schedule_changed is not None:
            self._on_schedule_changed()
        elif task is self._current_task:
            self._load(task)

    def _begin_pending(self) -> None:
        self._pending = True
        self._set_buttons(())
        self.status_label.configure(text=f"{self._current_task.label} -- saving...")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self, execution: TaskExecution | None, view: ExecutionItemView | None) -> None:
        self._stop_ticking()
        self._view = view
        task = self._current_task
        if task is None or view is None:
            self.status_label.configure(text="Select a task to begin." if task is None else f"{task.label}")
            for label in (self.timing_label, self.detail_label, self.elapsed_label, self.blocked_label):
                label.configure(text="")
            self._set_buttons(())
            return

        self.status_label.configure(text=f"{task.label} -- {view.status_text}")
        self.timing_label.configure(text=view.timing_text)
        details = [view.planned_text, view.actual_text]
        if view.sync_text:
            details.append(view.sync_text)
        self.detail_label.configure(text="\n".join(details))
        self.blocked_label.configure(text=view.reschedule_blocked or "")
        actions = view.actions if self._planning is not None else tuple(a for a in view.actions if a != "reschedule")
        self._set_buttons(actions)
        if execution is not None and execution.status == ExecutionStatus.IN_PROGRESS:
            self._start_ticking(execution.id)
        else:
            self.elapsed_label.configure(text=view.active_text)

    def _set_buttons(self, allowed) -> None:
        for action, button in self._action_buttons.items():
            button.configure(state="normal" if action in allowed and not self._pending else "disabled")

    def _start_ticking(self, execution_id: str) -> None:
        run_in_background(self, lambda: self._controller.elapsed_active_minutes(execution_id),
                          self._on_tick_base_loaded)

    def _on_tick_base_loaded(self, result: ControllerResult[float]) -> None:
        if not result.ok:
            self._show_error(result.error)
            return
        self._elapsed_base_minutes = result.value
        self._elapsed_base_time = datetime.now(timezone.utc)
        self._tick()

    def _tick(self) -> None:
        if self._elapsed_base_time is None:
            return
        elapsed_seconds = (datetime.now(timezone.utc) - self._elapsed_base_time).total_seconds()
        displayed_minutes = self._elapsed_base_minutes + elapsed_seconds / 60
        self.elapsed_label.configure(text=f"Active time: {displayed_minutes:.1f} min (live; pauses excluded)")
        self._tick_job = self.after(_TICK_INTERVAL_MS, self._tick)

    def _stop_ticking(self) -> None:
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self._elapsed_base_time = None

    def destroy(self) -> None:
        self._stop_ticking()
        super().destroy()

    def _show_error(self, message: str | None) -> None:
        messagebox.showerror("Execution Tracking", message or "An unknown error occurred.", parent=self)

