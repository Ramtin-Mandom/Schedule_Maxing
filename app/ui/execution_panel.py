"""
execution_panel.py

The task-execution controls widget embedded into the desktop UI's schedule
page: pick a scheduled task, then Start/Pause/Resume/Complete/Skip it, with
a live elapsed-active-time display and optional feedback on completion/skip.

All database access goes through ExecutionController (app/ui/execution_controller.py),
run off the Tk main thread via app.ui.background.run_in_background, and every
result is a ControllerResult -- failures are shown via messagebox, never left
to raise into Tk's event loop.

Duplicate prevention: this widget always resolves a selection through
ExecutionController.get_or_create_execution, never a raw "create" call, so
re-selecting the same task after the schedule is refreshed or the app is
reopened reuses the existing execution record (see
ExecutionService.get_or_create_execution in app/execution/service.py).
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from tkinter import messagebox

import customtkinter as ctk

from app.execution.models import ExecutionStatus, TaskExecution
from app.ui import theme
from app.ui.background import ControllerResult, run_in_background
from app.ui.execution_controller import ExecutionController
from app.ui.feedback_dialog import FeedbackDialog

_STATUS_LABELS = {
    ExecutionStatus.SCHEDULED: "Scheduled",
    ExecutionStatus.IN_PROGRESS: "In progress",
    ExecutionStatus.PAUSED: "Paused",
    ExecutionStatus.COMPLETED: "Completed",
    ExecutionStatus.SKIPPED: "Skipped",
}

_ACTION_LABELS = {
    "start": "Start",
    "pause": "Pause",
    "resume": "Resume",
    "complete": "Complete",
    "skip": "Skip",
}

_TICK_INTERVAL_MS = 1000


@dataclass(frozen=True)
class ExecutableTask:
    """One scheduled task the execution panel can track, decoupled from app.models/app.optimizer types."""

    day: int
    task_name: str
    category: str
    tag: str
    planned_start: int
    planned_end: int
    priority: int

    @property
    def planned_duration(self) -> int:
        return self.planned_end - self.planned_start

    @property
    def label(self) -> str:
        return f"Day {self.day}: {self.task_name}"


class ExecutionPanel(ctk.CTkFrame):
    """Task selector + Start/Pause/Resume/Complete/Skip controls + live elapsed time."""

    def __init__(self, parent: tk.Widget, execution_controller: ExecutionController) -> None:
        super().__init__(parent, fg_color="transparent")
        self._controller = execution_controller

        self._tasks: list[ExecutableTask] = []
        self._current_task: ExecutableTask | None = None
        self._current_execution: TaskExecution | None = None
        self._elapsed_base_minutes = 0.0
        self._elapsed_base_time: datetime | None = None
        self._tick_job: str | None = None

        self.columnconfigure(0, weight=1)
        self._build()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_scheduled_tasks(self, tasks: list[ExecutableTask]) -> None:
        """Replace the list of selectable tasks, e.g. after Make Schedule runs."""
        self._tasks = tasks
        labels = [task.label for task in tasks] or ["(no scheduled tasks -- run Make Schedule)"]
        self.task_menu.configure(values=labels)
        self.task_var.set(labels[0])
        self._stop_ticking()
        self._current_task = None
        self._current_execution = None
        if tasks:
            self._on_task_selected(labels[0])
        else:
            self._render_status(None)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.task_var = tk.StringVar(value="(no scheduled tasks -- run Make Schedule)")
        ctk.CTkLabel(self, text="Task", text_color=theme.TEXT_MUTED, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=4, pady=(4, 2)
        )
        self.task_menu = ctk.CTkOptionMenu(
            self, variable=self.task_var, values=[self.task_var.get()], command=self._on_task_selected
        )
        self.task_menu.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 10))

        self.status_label = ctk.CTkLabel(
            self, text="Select a task to begin.", text_color=theme.TEXT_PRIMARY, anchor="w"
        )
        self.status_label.grid(row=2, column=0, sticky="ew", padx=4, pady=(0, 2))

        self.elapsed_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_MUTED, anchor="w")
        self.elapsed_label.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 10))

        button_bar = ctk.CTkFrame(self, fg_color="transparent")
        button_bar.grid(row=4, column=0, sticky="ew", padx=4)
        button_bar.columnconfigure((0, 1), weight=1)

        self._action_buttons: dict[str, ctk.CTkButton] = {}
        for index, action in enumerate(("start", "pause", "resume", "complete", "skip")):
            button = ctk.CTkButton(
                button_bar,
                text=_ACTION_LABELS[action],
                state="disabled",
                fg_color=theme.DANGER if action == "skip" else theme.ACCENT,
                hover_color=theme.DANGER_HOVER if action == "skip" else theme.ACCENT_HOVER,
                command=lambda action=action: self._perform_action(action),
            )
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=4, pady=4)
            self._action_buttons[action] = button

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_task_selected(self, label: str) -> None:
        task = next((task for task in self._tasks if task.label == label), None)
        self._current_task = task
        self._stop_ticking()

        if task is None:
            self._render_status(None)
            return

        self.status_label.configure(text=f"{task.label} -- loading...")
        self._for_buttons(lambda button: button.configure(state="disabled"))

        run_in_background(
            self,
            lambda: self._controller.get_or_create_execution(
                task_name=task.task_name,
                category=task.category,
                tag=task.tag,
                planned_date=task.day,
                planned_start=task.planned_start,
                planned_end=task.planned_end,
                planned_duration=task.planned_duration,
                priority=task.priority,
            ),
            self._on_execution_loaded,
        )

    def _on_execution_loaded(self, result: ControllerResult[TaskExecution]) -> None:
        if not result.ok:
            self._show_error(result.error)
            self._render_status(None)
            return

        self._current_execution = result.value
        self._render_status(result.value)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _perform_action(self, action: str) -> None:
        if self._current_execution is None:
            return

        if action in ("complete", "skip"):
            self._collect_feedback_then(action)
            return

        self._run_transition(action)

    def _collect_feedback_then(self, action: str) -> None:
        title = "Complete Task" if action == "complete" else "Skip Task"

        def on_submit(
            focus_rating: int | None,
            energy_rating: int | None,
            interruption_count: int | None,
            note: str | None,
        ) -> None:
            self._run_transition(
                action,
                focus_rating=focus_rating,
                energy_rating=energy_rating,
                interruption_count=interruption_count,
                note=note,
            )

        FeedbackDialog(self, title=title, on_submit=on_submit)

    def _run_transition(
        self,
        action: str,
        *,
        focus_rating: int | None = None,
        energy_rating: int | None = None,
        interruption_count: int | None = None,
        note: str | None = None,
    ) -> None:
        execution_id = self._current_execution.id
        transition = getattr(self._controller, action)
        self._for_buttons(lambda button: button.configure(state="disabled"))

        def work() -> ControllerResult[TaskExecution]:
            result = transition(execution_id)
            has_feedback = any(
                value is not None
                for value in (focus_rating, energy_rating, interruption_count, note)
            )
            if result.ok and has_feedback:
                return self._controller.record_feedback(
                    execution_id,
                    focus_rating=focus_rating,
                    energy_rating=energy_rating,
                    interruption_count=interruption_count,
                    note=note,
                )
            return result

        run_in_background(self, work, self._on_transition_done)

    def _on_transition_done(self, result: ControllerResult[TaskExecution]) -> None:
        if not result.ok:
            self._show_error(result.error)
            # Re-render with the last known execution so buttons re-enable sensibly.
            self._render_status(self._current_execution)
            return

        self._current_execution = result.value
        self._render_status(result.value)

    # ------------------------------------------------------------------
    # Status / elapsed-time rendering
    # ------------------------------------------------------------------

    def _render_status(self, execution: TaskExecution | None) -> None:
        self._stop_ticking()

        if execution is None:
            self.status_label.configure(text="Select a task to begin.")
            self.elapsed_label.configure(text="")
            self._for_buttons(lambda button: button.configure(state="disabled"))
            return

        self.status_label.configure(text=f"{self._current_task.label} -- {_STATUS_LABELS[execution.status]}")

        allowed = set(self._controller.available_actions(execution.status))
        for action, button in self._action_buttons.items():
            button.configure(state="normal" if action in allowed else "disabled")

        if execution.status == ExecutionStatus.IN_PROGRESS:
            self._start_ticking(execution.id)
        else:
            self._show_static_elapsed(execution)

    def _show_static_elapsed(self, execution: TaskExecution) -> None:
        if execution.status == ExecutionStatus.COMPLETED and execution.actual_active_duration_minutes is not None:
            self.elapsed_label.configure(text=f"Active time: {execution.actual_active_duration_minutes:g} min")
        else:
            run_in_background(
                self,
                lambda: self._controller.elapsed_active_minutes(execution.id),
                self._on_static_elapsed_loaded,
            )

    def _on_static_elapsed_loaded(self, result: ControllerResult[float]) -> None:
        if result.ok:
            self.elapsed_label.configure(text=f"Active time: {result.value:g} min")

    def _start_ticking(self, execution_id: str) -> None:
        run_in_background(
            self,
            lambda: self._controller.elapsed_active_minutes(execution_id),
            self._on_tick_base_loaded,
        )

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
        self.elapsed_label.configure(text=f"Active time: {displayed_minutes:.1f} min (live)")
        self._tick_job = self.after(_TICK_INTERVAL_MS, self._tick)

    def _stop_ticking(self) -> None:
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self._elapsed_base_time = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _for_buttons(self, apply) -> None:
        for button in self._action_buttons.values():
            apply(button)

    def _show_error(self, message: str | None) -> None:
        messagebox.showerror("Execution Tracking Error", message or "An unknown error occurred.", parent=self)
