"""
app/ui/task_actions.py

The task-form actions every schedule page shares (Day, Week, Month): add or
save the form's task/fixed block, edit or remove a row (by its RowRef, from
the task list, the Day timeline or an available-task chip), and use rows as
dependencies. The page provides `page_controller` (a SchedulePageController),
`form` (app/ui/task_editor.TaskEditor), `added_tasks_panel`
(app/ui/task_list.AddedTasksPanel), `_editing`, `_render(snapshot)`,
`reload()`, `show_panel(key)` and `_refuse_while_busy()`. Persistence and
validation stay in the presenter and the planning services.
"""

from __future__ import annotations

from tkinter import messagebox

from app.ui.schedule_page_controller import RowRef
from app.ui.task_form_model import FormErrors, TaskDraft


class TaskFormActions:
    def submit_task(self, draft: TaskDraft) -> None:
        """Add (or, in edit mode, save) the form's task/fixed block through the presenter and services."""
        if self._refuse_while_busy():
            return
        result = self.page_controller.save_draft(draft, editing=self._editing)
        if result.value is not None:
            self._render(result.value)
        if not result.ok:
            # The form keeps everything typed; errors go next to their fields (FormErrors) and in the notice.
            errors = result.cause.errors if isinstance(result.cause, FormErrors) else None
            self.form.show_errors(result.error or "The task could not be saved.", errors)
            return
        saved_date = draft.date.strip()
        self._leave_edit_mode()
        self.form.reset_for_next(saved_date or self.page_controller.anchor_date.isoformat())
        if saved_date and saved_date not in {day.isoformat() for day in self.page_controller.dates}:
            self.form.notice.show("info", f"Saved for {saved_date}, which is outside the dates shown here.")

    # Kept for callers of the previous API name.
    def add_task(self, draft: TaskDraft) -> None:
        self.submit_task(draft)

    def edit_selected_task(self) -> None:
        if self._refuse_while_busy():
            return
        refs = self.added_tasks_panel.selected_refs()
        if len(refs) != 1:
            messagebox.showinfo("Edit Task", "Select exactly one task or fixed block to edit.", parent=self)
            return
        self.edit_ref(refs[0])

    def edit_ref(self, ref: RowRef) -> None:
        """Load one saved task/fixed block into the form for editing."""
        if self._refuse_while_busy():
            return
        draft = self.page_controller.draft_for(ref)
        if not draft.ok:
            messagebox.showerror("Could Not Edit", draft.error or "Unknown error.", parent=self)
            self.reload()
            return
        options = self.page_controller.editor_options(ref, category=draft.value.category,
                                                      project_id=draft.value.project_id)
        if options.ok:
            self.form.set_options(options.value)
        self._editing = ref
        self.form.load(draft.value, editing=True)
        self.show_panel("input")

    def cancel_edit(self) -> None:
        self._leave_edit_mode()
        self._reset_editor()

    def _reset_editor(self) -> None:
        """A blank form for this page's date, with current choices (dependencies, projects, categories)."""
        options = self.page_controller.editor_options()
        if options.ok:
            self.form.set_options(options.value)
        self.form.load(self.page_controller.blank_draft(self.form.kind), editing=False)

    def use_selected_as_dependencies(self) -> None:
        refs = self.added_tasks_panel.selected_refs()
        task_ids = [ref.id for ref in refs if ref.kind == "task"]
        if len(task_ids) != len(refs):
            messagebox.showinfo("Dependencies", "Fixed blocks cannot be dependencies; they were ignored.", parent=self)
        if self.form.kind != "task":
            messagebox.showinfo("Dependencies", "Only a flexible task has dependencies.", parent=self)
            return
        self.form.set_dependencies(task_ids)
        self.show_panel("input")

    def remove_selected_task(self) -> None:
        if self._refuse_while_busy():
            return
        refs = self.added_tasks_panel.selected_refs()
        if not refs:
            messagebox.showinfo("No Selection", "Select a task to remove first.", parent=self)
            return
        if len(refs) > 1:
            messagebox.showinfo("Remove Task", "Remove one task or fixed block at a time.", parent=self)
            return
        self.remove_ref(refs[0])

    def remove_ref(self, ref: RowRef) -> None:
        """Remove one saved task/fixed block after confirming what goes with it."""
        if self._refuse_while_busy():
            return
        description = self.page_controller.delete_description(ref)
        if not description.ok:
            messagebox.showerror("Could Not Remove", description.error or "Unknown error.", parent=self)
            self.reload()
            return
        if not messagebox.askyesno("Remove?", description.value, icon="warning", parent=self):
            return
        result = self.page_controller.delete(ref)
        self._render_result(result, "Could Not Remove Task")
        if result.ok and self._editing == ref:
            self.cancel_edit()

    def _render_result(self, result, error_title: str) -> None:
        """Redraw from the (re-read) snapshot; on failure show the error, then the committed state."""
        if result.value is not None:
            self._render(result.value)
        if not result.ok:
            messagebox.showerror(error_title, result.error or "Unknown error.", parent=self)
            if result.value is None:
                self.reload()

    def _leave_edit_mode(self) -> None:
        self._editing = None
        self.form.editing = False
