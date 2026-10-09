"""
app/ui/task_actions.py

The task-form actions every schedule page shares (Day, Week, Month): add or
save the form's task/fixed block (on the page's selected date), and edit or
remove a record by its RowRef (from the Day timeline or an available-task
button). The page provides `page_controller` (a SchedulePageController),
`form` (app/ui/task_editor.TaskEditor), `_editing`, `_render(snapshot)`,
`reload()`, `show_panel(key)` and `_refuse_while_busy()`. Persistence and
validation stay in the presenter and the planning services.

Storage calls go through _io(): in a worker when the page runs inside the
desktop app (background_io; app/ui/background.run_io), the page counting as
busy meanwhile so a second action waits; at once for a page built on its own.
Every call works on a copy of the presenter frozen at the dates shown when
it was started (SchedulePageController.detached), so a worker neither reads
dates the page has since left nor changes the page's own; everything typed
or selected is read on the Tk thread before the call starts. Blocking calls
share one serialized lane (background.WRITE_LANE); reads that only matter
when newest replace their queued predecessors. Whatever the storage, a
failed save keeps everything typed in the form.
"""

from __future__ import annotations

from dataclasses import replace
from tkinter import messagebox

from app.persistence.errors import NotSignedInError
from app.ui.background import WRITE_LANE, ControllerResult, run_io
from app.ui.components import ChoiceDialog
from app.ui.schedule_page_controller import RowRef
from app.ui.task_form_model import FormErrors, TaskDraft


class TaskFormActions:
    #: True inside the desktop app (local and direct storage): storage calls run off the Tk thread.
    background_io = False

    def _io(self, work, done, *, blocking: bool = True, newest: str | None = None) -> None:
        """
        Run a storage call (see the module docstring). `blocking` marks the page
        busy while it runs and orders it behind earlier changes; `newest` names
        a read that a later one of the same name replaces.
        """
        if blocking and self.background_io:
            self._busy = True

        def finish(result) -> None:
            if blocking and self.background_io:
                self._busy = False
            done(result)

        finish.__qualname__ = getattr(done, "__qualname__", finish.__qualname__)  # the name diagnostics report
        if not run_io(self, work, finish, background=self.background_io, serial=WRITE_LANE if blocking else None,
                      supersede=(id(self), newest) if newest and not blocking else None) and blocking:
            self._busy = False

    def _next_load(self) -> int:
        """A token for a load: only the newest load's result is shown."""
        self._load_token = getattr(self, "_load_token", 0) + 1
        return self._load_token

    def _next_options(self) -> int:
        """A token for a read of the form's choices: only the newest read's choices are shown."""
        self._options_token = getattr(self, "_options_token", 0) + 1
        return self._options_token

    def _load_failed(self, result) -> None:
        if isinstance(result.cause, NotSignedInError):
            return  # direct storage before sign-in: the Account page says so; no dialog per page
        messagebox.showerror("Could Not Load Saved Data", result.error or "Unknown error.", parent=self)

    def submit_task(self, draft: TaskDraft) -> None:
        """
        Add (or, in edit mode, save) the form's task/fixed block through the
        presenter and services. A new record always gets the page's selected
        date (page_controller.form_date) -- the form has no date to type; an
        edit keeps the record's own date.
        """
        if self._refuse_while_busy():
            return
        editing = self._editing
        if not editing:
            draft = replace(draft, date=self.page_controller.form_date.isoformat())
        if editing and draft.recurrence_role == "occurrence":
            # One occurrence of a repeating task: ask what the change applies to (docs/recurrence.md).
            ChoiceDialog(self, title="Change a repeating task", prompt="Apply this change to:", options=[
                ("occurrence", "Only this occurrence"),
                ("future", "This and every later occurrence"),
                ("series", "Every occurrence (the entire series)"),
            ], note="Occurrences already started or finished, and ones edited on their own, are kept as they are.",
                on_choose=lambda scope: self._save_with_scope(draft, editing, scope))
            return
        self._save_with_scope(draft, editing, None)

    def _save_with_scope(self, draft: TaskDraft, editing, scope: str | None) -> None:
        controller = self.page_controller.detached()
        self._io(lambda: controller.save_draft(draft, editing=editing, scope=scope),
                 lambda result: self._task_saved(draft, result))

    def _task_saved(self, draft: TaskDraft, result) -> None:
        if result.value is not None:
            self._render(result.value)
        if not result.ok:
            # The form keeps everything typed; errors go next to their fields (FormErrors) and in the notice.
            errors = result.cause.errors if isinstance(result.cause, FormErrors) else None
            self.form.show_errors(result.error or "The task could not be saved.", errors)
            return
        saved_date = draft.date.strip()
        self._leave_edit_mode()
        self.form.reset_for_next(self.page_controller.form_date.isoformat())
        if saved_date and saved_date not in {day.isoformat() for day in self.page_controller.dates}:
            self.form.notice.show("info", f"Saved for {saved_date}, which is outside the dates shown here.")

    # Kept for callers of the previous API name.
    def add_task(self, draft: TaskDraft) -> None:
        self.submit_task(draft)

    def edit_ref(self, ref: RowRef) -> None:
        """Load one saved task/fixed block into the form for editing."""
        if self._refuse_while_busy():
            return

        controller = self.page_controller.detached()

        def work():
            draft = controller.draft_for(ref)
            if not draft.ok:
                return draft, None
            return draft, controller.editor_options(ref, category=draft.value.category,
                                                    project_id=draft.value.project_id)

        self._io(work, lambda loaded: self._edit_loaded(ref, loaded))

    def _edit_loaded(self, ref: RowRef, loaded) -> None:
        draft, options = loaded if not isinstance(loaded, ControllerResult) else (loaded, None)
        if not draft.ok:
            messagebox.showerror("Could Not Edit", draft.error or "Unknown error.", parent=self)
            self.reload()
            return
        if options is not None and options.ok:
            self.form.set_options(options.value)
        self._editing = ref
        self.form.load(draft.value, editing=True)
        self.show_panel("input")

    def cancel_edit(self) -> None:
        self._leave_edit_mode()
        self._reset_editor()

    def _reset_editor(self) -> None:
        """A blank form for this page's date, with current choices (dependencies, projects, categories)."""

        token = self._next_options()

        def apply(options) -> None:
            if options.ok and token == self._options_token:
                self.form.set_options(options.value)
            self.form.load(self.page_controller.blank_draft(self.form.kind), editing=False)

        self._io(self.page_controller.detached().editor_options, apply, blocking=False)

    def _refresh_options(self) -> None:
        """Refresh the form's choices after a redraw (not while an edit is open)."""

        token = self._next_options()

        def apply(options) -> None:
            if options.ok and not self._editing and token == self._options_token:
                self.form.set_options(options.value)

        self._io(self.page_controller.detached().editor_options, apply, blocking=False, newest="options")

    def remove_ref(self, ref: RowRef) -> None:
        """Remove one saved task/fixed block after confirming what goes with it."""
        if self._refuse_while_busy():
            return
        controller = self.page_controller.detached()
        self._io(lambda: (controller.delete_description(ref), controller.removal_choices(ref)),
                 lambda loaded: self._confirm_removal(ref, *loaded))

    def remove_editing(self) -> None:
        """Remove the record loaded in the form (the form's "Remove task" button)."""
        if self._editing:
            self.remove_ref(self._editing)

    def _confirm_removal(self, ref: RowRef, description, choices=None) -> None:
        if not description.ok:
            messagebox.showerror("Could Not Remove", description.error or "Unknown error.", parent=self)
            self.reload()
            return
        if choices is not None and choices.ok and len(choices.value) > 1:
            # One occurrence of a repeating task: skip/delete it, it and every later one, or the series.
            ChoiceDialog(self, title="Remove a repeating task", prompt=description.value, options=choices.value,
                         danger=True, on_choose=lambda scope: self._remove_with_scope(ref, scope))
            return
        if not messagebox.askyesno("Remove?", description.value, icon="warning", parent=self):
            return
        scope = choices.value[0][0] if choices is not None and choices.ok and choices.value else None
        self._remove_with_scope(ref, scope)

    def _remove_with_scope(self, ref: RowRef, scope: str | None) -> None:
        controller = self.page_controller.detached()
        self._io(lambda: controller.delete(ref, scope=scope), lambda result: self._removed(ref, result))

    def _removed(self, ref: RowRef, result) -> None:
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
