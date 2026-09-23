"""
execution_panel.py

The task-execution controls widget embedded into the desktop UI's schedule
page: pick a scheduled task, then Start/Pause/Resume/Complete/Skip it, with
a live elapsed-active-time display and optional feedback on completion/skip.

All database access goes through ExecutionController (app/ui/execution_controller.py),
run off the Tk main thread via app.ui.background.run_in_background, and every
result is a ControllerResult -- failures are shown via messagebox, never left
to raise into Tk's event loop.

Identity and duplicate prevention (Milestone 2): the selectable items are
saved canonical placements (ExecutablePlacement: the persisted Task and
ScheduledTask), and a selection always resolves through
ExecutionController.get_or_create_canonical_execution, keyed by
task_id/scheduled_task_id -- never by task name or day index. Selecting a
placement only *looks up* its execution (find_execution_for_placement), so
re-selecting it after a refresh or after reopening the app restores the
existing execution (status, sessions, feedback) and merely viewing a
placement never writes a row. The execution is created (get-or-create,
so never duplicated) the first time the user starts or skips it. Two tasks
with the same name stay distinct. A refresh keeps the current selection
when that placement still exists.
"""

from __future__ import annotations

import tkinter as tk
import uuid
from datetime import datetime, timezone
from tkinter import messagebox

import customtkinter as ctk

from app.execution.models import ExecutionStatus, TaskExecution
from app.ui import theme
from app.ui.background import ControllerResult, run_in_background
from app.ui.execution_controller import ExecutionController
from app.ui.feedback_dialog import FeedbackDialog
from app.ui.schedule_page_controller import ExecutablePlacement

_STATUS_LABELS = {
    ExecutionStatus.SCHEDULED: "Scheduled",
    ExecutionStatus.IN_PROGRESS: "In progress",
    ExecutionStatus.PAUSED: "Paused",
    ExecutionStatus.COMPLETED: "Completed",
    ExecutionStatus.SKIPPED: "Skipped",
    # No Cancel control is wired into this panel yet (see Task 6), but a
    # cancelled execution can still exist (e.g. created via the canonical
    # API elsewhere) and must render, not KeyError, if ever displayed here.
    ExecutionStatus.CANCELLED: "Cancelled",
}

_ACTION_LABELS = {
    "start": "Start",
    "pause": "Pause",
    "resume": "Resume",
    "complete": "Complete",
    "skip": "Skip",
}

_TICK_INTERVAL_MS = 1000
_NO_TASKS_LABEL = "(no saved schedule -- run Make Schedule)"


class ExecutionPanel(ctk.CTkFrame):
    """Task selector + Start/Pause/Resume/Complete/Skip controls + live elapsed time."""

    def __init__(self, parent: tk.Widget, execution_controller: ExecutionController) -> None:
        super().__init__(parent, fg_color="transparent")
        self._controller = execution_controller

        self._tasks: list[ExecutablePlacement] = []
        self._by_label: dict[str, ExecutablePlacement] = {}
        self._current_task: ExecutablePlacement | None = None
        self._current_execution: TaskExecution | None = None
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

        if not self._tasks:
            self.task_var.set(labels[0])
            self._render_status(None)
            return

        selected = self._find_by_placement_id(previous_id) or self._tasks[0]
        self.task_var.set(selected.label)
        self._on_task_selected(selected.label)

    def _find_by_placement_id(self, placement_id: uuid.UUID | None) -> ExecutablePlacement | None:
        return next((task for task in self._tasks if task.placement.id == placement_id), None)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.task_var = tk.StringVar(value=_NO_TASKS_LABEL)
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
        task = self._by_label.get(label)
        self._current_task = task
        self._stop_ticking()

        if task is None:
            self._render_status(None)
            return

        self.status_label.configure(text=f"{task.label} -- loading...")
        self._for_buttons(lambda button: button.configure(state="disabled"))

        run_in_background(
            self,
            lambda: self._controller.find_execution_for_placement(task.placement.id),
            lambda result: self._on_execution_loaded(task, result),
        )

    def _on_execution_loaded(self, task: ExecutablePlacement, result: ControllerResult[TaskExecution | None]) -> None:
        if task is not self._current_task:
            return  # the selection changed while this was loading
        if not result.ok:
            self._show_error(result.error)
            self._render_status(None)
            return

        self._current_execution = result.value  # None: not started yet
        self._render_status(result.value)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _perform_action(self, action: str) -> None:
        if self._current_task is None:
            return
        if self._current_execution is None and action not in ("start", "skip"):
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
        task = self._current_task
        known = self._current_execution
        transition = getattr(self._controller, action)
        self._for_buttons(lambda button: button.configure(state="disabled"))

        def work() -> ControllerResult[TaskExecution]:
            if known is None:
                # First action on this placement: create its execution (get-or-create, never a duplicate).
                created = self._controller.get_or_create_canonical_execution(task.task, task.placement)
                if not created.ok:
                    return created
                current = created.value
            else:
                current = known
            # The version on screen is the precondition: a change made
            # elsewhere since then is reported instead of overwritten.
            result = transition(current.id, expected_version=current.version)
            has_feedback = any(
                value is not None
                for value in (focus_rating, energy_rating, interruption_count, note)
            )
            if result.ok and has_feedback:
                return self._controller.record_feedback(
                    current.id,
                    expected_version=result.value.version,
                    focus_rating=focus_rating,
                    energy_rating=energy_rating,
                    interruption_count=interruption_count,
                    note=note,
                )
            return result

        run_in_background(self, work, lambda result: self._on_transition_done(task, result))

    def _on_transition_done(self, task: ExecutablePlacement, result: ControllerResult[TaskExecution]) -> None:
        if task is not self._current_task:
            return  # the selection changed while the transition was running
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

        if self._current_task is None:
            self.status_label.configure(text="Select a task to begin.")
            self.elapsed_label.configure(text="")
            self._for_buttons(lambda button: button.configure(state="disabled"))
            return

        if execution is None:
            self.status_label.configure(text=f"{self._current_task.label} -- Not started")
            self.elapsed_label.configure(text="")
            allowed = set(self._controller.available_actions(ExecutionStatus.SCHEDULED))
            for action, button in self._action_buttons.items():
                button.configure(state="normal" if action in allowed else "disabled")
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

    def destroy(self) -> None:
        self._stop_ticking()
        super().destroy()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _for_buttons(self, apply) -> None:
        for button in self._action_buttons.values():
            apply(button)

    def _show_error(self, message: str | None) -> None:
        messagebox.showerror("Execution Tracking Error", message or "An unknown error occurred.", parent=self)
