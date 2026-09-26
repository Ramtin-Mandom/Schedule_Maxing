"""Native project management and date-allocation previews; services own all writes."""
from __future__ import annotations

import customtkinter as ctk

from app.ui import theme
from app.ui.allocation_controller import filter_view
from app.ui.background import run_in_background
from app.ui.components import AppButton, LabeledEntry, LabeledSelect, Notice, ask_confirm, font
from app.ui.pages import PageHeader
from app.ui.projects_controller import project_choices


class PlanningPage(ctk.CTkFrame):
    def __init__(self, parent, title, subtitle):
        super().__init__(parent, fg_color=theme.APP_BG)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._busy = False
        self.surface = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.surface.grid(row=0, column=0, sticky="nsew")
        self.surface.columnconfigure(0, weight=1)
        header = PageHeader(self.surface, title, subtitle)
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=16)
        for label in header.winfo_children():
            label.configure(wraplength=360)
        self.controls = ctk.CTkFrame(self.surface, fg_color="transparent")
        self.controls.grid(row=1, column=0, sticky="ew", padx=20)
        self.controls.columnconfigure(0, weight=1)
        self.body = ctk.CTkFrame(self.surface, fg_color="transparent")
        self.body.grid(row=2, column=0, sticky="nsew", padx=16)
        self.body.columnconfigure(0, weight=1)
        self.notice = Notice(self.surface, wraplength=360)
        self.notice.grid(row=3, column=0, sticky="ew", padx=20, pady=8)
        self.notice.hide()

    def work(self, operation, done):
        if self._busy:
            return
        self._busy = True
        self.notice.show("info", "Working…")

        def finish(result):
            self._busy = False
            if result.ok:
                self.notice.hide()
                done(result.value)
            else:
                self.notice.show("error", result.error)

        if not run_in_background(self, operation, finish):
            self._busy = False

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def line(self, text, row):
        label = ctk.CTkLabel(self.body, text=text, anchor="w", justify="left", wraplength=360,
                             font=font(), text_color=theme.TEXT_PRIMARY)
        label.grid(row=row, column=0, sticky="ew", pady=6)
        return label


class ProjectsPage(PlanningPage):
    def __init__(self, parent, controller, *, on_open_day):
        super().__init__(parent, "Project Schedule", "Manage projects and see their saved work. Assign tasks in any task form.")
        self.controller, self.on_open_day = controller, on_open_day
        self.snapshot = None
        self.selected = None
        self.choices = {}
        self.project_select = LabeledSelect(self.controls, "Project", ["New project"], command=self.choose)
        self.project_select.grid(row=0, column=0, sticky="ew")
        self.name = LabeledEntry(self.controls, "Name")
        self.name.grid(row=1, column=0, sticky="ew", pady=4)
        self.description = LabeledEntry(self.controls, "Description")
        self.description.grid(row=2, column=0, sticky="ew", pady=4)
        bar = ctk.CTkFrame(self.controls, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="w", pady=8)
        self.save_button = AppButton(bar, "Save project", self.save)
        self.save_button.grid(row=0, column=0, padx=(0, 8))
        self.delete_button = AppButton(bar, "Delete empty project", self.delete, variant="danger")
        self.delete_button.grid(row=0, column=1)
        self.move_select = LabeledSelect(self.controls, "Move this project's tasks to", ["No project"])
        self.move_select.grid(row=4, column=0, sticky="ew")
        AppButton(self.controls, "Move tasks…", self.move, variant="secondary").grid(row=5, column=0, sticky="w", pady=8)

    def on_show(self):
        selected = self.selected
        self.work(lambda: self.controller.load(selected), self.render)

    def choose(self, label):
        if self._busy:
            return
        self.selected = self.choices.get(label)
        self.on_show()

    def render(self, snapshot):
        self.snapshot = snapshot
        self.choices = project_choices({p.id: p.name for p in snapshot.projects})
        chosen = next((label for label, key in self.choices.items() if key == self.selected), "New project")
        self.project_select.set_values(["New project", *self.choices], selected=chosen)
        self.move_select.set_values(["No project", *self.choices], selected="No project")
        self.selected = snapshot.selected.id if snapshot.selected else None
        self.name.variable.set(snapshot.selected.name if snapshot.selected else "")
        self.description.variable.set(snapshot.selected.description if snapshot.selected else "")
        self.clear()
        self.line(f"{len(snapshot.projects)} project(s); {snapshot.unassigned_count} task(s) without a project.", 0)
        if not snapshot.tasks:
            self.line("No tasks in this project. Use the Project choice in a task form to assign work.", 1)
        for index, task in enumerate(snapshot.tasks):
            self.line(f"{task.name} · {task.duration_text} · {task.date_text}\n{task.status}"
                      + (f"\nDeadline: {task.deadline_text}" if task.deadline_text else "")
                      + ("\nRecurring rule saved; occurrences are not expanded." if task.recurring else ""), 2 * index + 1)
            dates = ctk.CTkFrame(self.body, fg_color="transparent")
            dates.grid(row=2 * index + 2, column=0, sticky="ew")
            for row, day in enumerate(task.dates):
                AppButton(dates, f"Open {day}", lambda day=day: self.on_open_day(day), variant="secondary").grid(
                    row=row, column=0, sticky="w", pady=2)

    def save(self):
        if self._busy:
            return
        name, description = self.name.get(), self.description.get()
        selected = self.snapshot.selected if self.snapshot else None
        operation = (lambda: self.controller.update(selected.id, name, description, expected_version=selected.version)) \
            if selected else (lambda: self.controller.create(name, description))

        def saved(project):
            self.selected = project.id
            self.on_show()
        self.work(operation, saved)

    def delete(self):
        selected = self.snapshot.selected if self.snapshot else None
        if self._busy or selected is None:
            return
        if ask_confirm(self, title="Delete project?", message=f"Delete {selected.name}? Tasks are never deleted with it.",
                       confirm_text="Delete", danger=True):
            def deleted(_):
                self.selected = None
                self.on_show()
            self.work(lambda: self.controller.delete(selected.id, expected_version=selected.version), deleted)

    def move(self):
        source, target = self.selected, self.choices.get(self.move_select.get())
        if self._busy or source is None or source == target:
            return
        if ask_confirm(self, title="Move tasks?", message=f"Move all tasks of this project to {self.move_select.get()}?",
                       confirm_text="Move tasks"):
            self.work(lambda: self.controller.reassign_tasks(source, target), lambda _: self.on_show())


class AllocationPage(PlanningPage):
    def __init__(self, parent, controller, *, on_open_day):
        super().__init__(parent, "Allocation Planning",
                         "A date-only preview. Exact times are saved only when you schedule a date.")
        self.controller, self.on_open_day = controller, on_open_day
        self.preview = None
        self.choices = {}
        self.mode = LabeledSelect(self.controls, "Range", ["Week", "Month"], command=self.set_mode)
        self.mode.grid(row=0, column=0, sticky="ew")
        self.range_label = ctk.CTkLabel(self.controls, text=controller.period.title, font=font())
        self.range_label.grid(row=1, column=0, sticky="w")
        bar = ctk.CTkFrame(self.controls, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="w", pady=8)
        AppButton(bar, "Previous", lambda: self.shift(-1), variant="secondary", width=90).grid(row=0, column=0)
        AppButton(bar, "Next", lambda: self.shift(1), variant="secondary", width=90).grid(row=0, column=1, padx=6)
        self.allocate_button = AppButton(bar, "Allocate / Recalculate", self.allocate)
        self.allocate_button.grid(row=0, column=2)
        self.project_select = LabeledSelect(self.controls, "Show project (display only)", ["All projects"],
                                            command=lambda _: self.render())
        self.project_select.grid(row=3, column=0, sticky="ew")
        self.line("Choose a range, then Allocate. No preview is saved as a schedule.", 0)

    def set_mode(self, label):
        if not self._busy:
            self.controller.set_mode(label.lower())
            self.reset_preview()

    def shift(self, delta):
        if not self._busy:
            self.controller.shift(delta)
            self.reset_preview()

    def reset_preview(self):
        self.preview = None
        self.range_label.configure(text=self.controller.period.title)
        self.clear()
        self.line("Choose Allocate to preview this range.", 0)

    def allocate(self):
        period = self.controller.period
        self.work(lambda: self.controller.allocate(period), self.allocated)

    def allocated(self, view):
        self.preview = view
        self.choices = project_choices(view.projects)
        selected = self.project_select.get()
        self.project_select.set_values(["All projects", *self.choices],
                                       selected=selected if selected in self.choices else "All projects")
        self.render()

    def on_show(self):
        if self.preview is not None:
            preview = self.preview
            self.work(lambda: self.controller.is_stale(preview), self.checked)

    def checked(self, stale):
        if stale:
            self.notice.show("warning", "Preview is out of date. Recalculate before scheduling a date.")

    def render(self):
        if self.preview is None:
            return
        self.clear()
        view = filter_view(self.preview, self.choices.get(self.project_select.get()))
        self.line(f"Preview · {view.assigned_count} assigned · {len(view.unallocated)} without a date", 0)
        row = 1
        for day in view.days:
            freshness = {"none": "Not scheduled", "stale": "Out of date", "current": "Current"}[day.freshness.value]
            self.line(f"{day.date} · {day.free_text} free · {freshness}\n" +
                      ("\n".join(f"{t.name} · {t.duration_minutes} min · {t.project_name or 'No project'}"
                                 + (f" · deadline {t.deadline_text}" if t.deadline_text else "")
                                 for t in day.tasks) or "No tasks allocated"), row)
            bar = ctk.CTkFrame(self.body, fg_color="transparent")
            bar.grid(row=row + 1, column=0, sticky="w", pady=4)
            AppButton(bar, "Open Day", lambda date=day.date: self.open_day(date), variant="secondary").grid(row=0, column=0)
            AppButton(bar, "Schedule this date", lambda date=day.date: self.schedule(date)).grid(row=0, column=1, padx=8)
            row += 2
        for item in view.unallocated:
            self.line(f"{item.task.name}: {item.reason}\n{item.explanation}\n{item.certainty}", row)
            row += 1

    def open_day(self, day):
        if self._busy:
            return
        preview = self.preview
        self.work(lambda: self.controller.is_stale(preview),
                  lambda stale: self.checked(True) if stale else self.on_open_day(day))

    def schedule(self, day):
        preview = self.preview
        if preview is not None:
            self.work(lambda: self.controller.schedule_date(preview, day),
                      lambda message: self.notice.show("success", message))
