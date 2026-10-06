from tests.ui.test_desktop_app import open_app, close_app, pump, WEDNESDAY
from tests.ui.test_desktop_app import dialogs as dialogs, pytestmark as pytestmark  # noqa: F401
from datetime import timedelta

from app.planning.models import Task


def test_native_projects(tmp_path, dialogs):
    app = open_app(tmp_path / "planning.db", tmp_path)
    try:
        app.show_page("projects")
        page = app.pages["projects"]
        pump(app, until=lambda: not page._busy)
        page.name.variable.set("My project")
        page.description.variable.set("Real persisted work")
        page.start_date.variable.set("2026-09-01")
        page.create_button.invoke()
        pump(app, until=lambda: not page._busy and page.snapshot is not None and page.snapshot.ongoing)
        assert page.selected is None and page.name.get() == ""  # still the overview, ready for the next project
        page.completed_section.toggle()  # each section collapses on its own
        assert not page.completed_section.expanded and page.ongoing_section.expanded
        page.open_project(page.snapshot.ongoing[0].id)
        pump(app, until=lambda: not page._busy and page.snapshot.selected is not None)
        assert page.snapshot.selected.name == "My project"
        planning = app.services.planning_controller
        saved = planning.add_or_update_task(Task(name="Study", category="study", priority=5,
                                                 estimated_duration_minutes=13, required_date=WEDNESDAY,
                                                 project_id=page.selected))
        assert saved.ok
        page.on_show()
        pump(app, until=lambda: not page._busy)
        assert page.snapshot.tasks[0].status == "Not scheduled"
        page.on_open_day(WEDNESDAY)
        assert app.shell.current == "day"
        day = app.pages["day"]
        day.make_schedule_button.invoke()
        pump(app, until=lambda: not day._busy)
        assert len(planning.get_placements(WEDNESDAY).value) == 1
        app.show_page("projects")
        pump(app, until=lambda: not page._busy)
        assert page.snapshot.tasks[0].dates == (WEDNESDAY,)

        # Add Task: the shared form with a date where the Day page's has the project choice.
        assert page.form.date_selector and page.form.date_field.winfo_manager() == "grid"
        assert page.form.project_select.winfo_manager() == ""
        page.form.name_field.variable.set("Outline")
        page.form.duration_field.variable.set("30m")
        page.form.date_field.variable.set("")
        page.form.submit()
        pump(app, until=lambda: not page._busy)
        assert page.form.date_field.error and len(page.snapshot.tasks) == 1  # a date is required; nothing was added
        page.form.date_field.variable.set(WEDNESDAY.isoformat())
        page.form.submit()
        pump(app, until=lambda: not page._busy and len(page.snapshot.tasks) == 2)
        outline = next(task for task in page.snapshot.tasks if task.name == "Outline")
        assert outline.status == "Not scheduled" and outline.dates == (WEDNESDAY,)

        # Project Tasks: cancelling the date dialog changes nothing; confirming moves the task, unscheduled.
        study = next(task for task in page.snapshot.tasks if task.name == "Study")
        page._ask_date = lambda task: None
        page.ask_task_date(study)
        assert not page._busy and len(planning.get_placements(WEDNESDAY).value) == 1
        later = WEDNESDAY + timedelta(days=2)
        page._ask_date = lambda task: later
        page.ask_task_date(study)
        pump(app, until=lambda: not page._busy and next(
            task for task in page.snapshot.tasks if task.name == "Study").dates == (later,))
        assert planning.get_placements(WEDNESDAY).value == []
        assert planning.get_task(study.task_id).value.required_date == later

        # Milestones: added through the dialog's values, listed by number, scored 1-10.
        page._ask_milestone = lambda: ("2", "Second", "Reviewed by the team")
        page.ask_milestone()
        pump(app, until=lambda: not page._busy and len(page.snapshot.milestones) == 1)
        page.add_milestone("1", "First", "Started")
        pump(app, until=lambda: not page._busy and len(page.snapshot.milestones) == 2)
        assert [(m.number, m.score) for m in page.snapshot.milestones] == [(1, 1), (2, 1)]
        page.set_score(page.snapshot.milestones[0].id, 9)
        pump(app, until=lambda: not page._busy and page.snapshot.milestones[0].score == 9)
        assert page.snapshot.milestones[0].band == "green"

        # The whole milestone widget carries its score's color; its score control stays a plain input.
        from app.ui.planning_pages import SCORE_COLORS

        cards = [card for card in page.milestones_list.winfo_children() if hasattr(card, "score_menu")]
        assert [tuple(card.cget("fg_color")) for card in cards] == [SCORE_COLORS["green"][0], SCORE_COLORS["neutral"][0]]
        assert cards[0].remove_button.cget("text") == "×" and cards[0].remove_button.grid_info()["column"] == 0

        # "×" removes a milestone after its confirmation (declining keeps it) and nothing else.
        page._confirm_milestone_removal = lambda milestone: False
        cards[1].remove_button.invoke()
        assert not page._busy and len(page.snapshot.milestones) == 2
        page._confirm_milestone_removal = lambda milestone: True
        cards[1].remove_button.invoke()
        pump(app, until=lambda: not page._busy and len(page.snapshot.milestones) == 1)
        assert page.snapshot.milestones[0].score == 9

        # Done completes a task right in the panel -- Outline was never scheduled -- and unticking undoes it.
        def row_of(name):
            return next(row for row in page.tasks_list.winfo_children()
                        if getattr(row, "done_box", None) is not None and row.winfo_children()[1].cget("text") == name)

        def outline_row():
            return next(task for task in page.snapshot.tasks if task.name == "Outline")

        assert not outline_row().scheduled and not outline_row().completed
        row_of("Outline").done_box.toggle()
        pump(app, until=lambda: not page._busy and outline_row().completed)
        assert planning.placements_for_tasks([outline_row().task_id]).value[outline_row().task_id] == []
        assert len(page.snapshot.tasks) == 2  # ticking Done selected, moved and removed nothing
        row_of("Outline").done_box.toggle()
        pump(app, until=lambda: not page._busy and not outline_row().completed)

        # "×" removes a task with the schedule pages' confirmation; cancelling keeps it.
        page._ask_removal = lambda description, choices: False
        row_of("Outline").remove_button.invoke()
        pump(app, until=lambda: not page._busy)
        assert len(page.snapshot.tasks) == 2
        page._ask_removal = lambda description, choices: None
        row_of("Outline").remove_button.invoke()
        pump(app, until=lambda: not page._busy and len(page.snapshot.tasks) == 1)
        assert [task.name for task in page.snapshot.tasks] == ["Study"]

        # Project defaults: saved from the Edit dialog's values, they prefill the next new task of this project.
        project = page.snapshot.selected
        assert not project.task_defaults.configured and page.form.duration_field.get() == ""
        result = app.pages["projects"].controller.update(
            project.id, project.name, project.description, expected_version=project.version,
            task_defaults=("45m", "", "12"))
        assert result.ok
        page.on_show()
        pump(app, until=lambda: not page._busy and page.snapshot.selected.task_defaults.configured)
        assert page.form.duration_field.get() == "45 min" and page.form.points_field.get() == "12"
        assert page.form.priority_select.get() == "5"  # not configured: the form's own value stays

        # "View performance" opens Performance -> Project with this project selected; coming back keeps the panel.
        page.performance_button.invoke()
        performance = app.pages["productivity"].content
        pump(app, until=lambda: performance.project_report is not None)
        assert app.shell.current == "productivity" and performance.section == "Project"
        assert performance.project_id == project.id
        app.show_page("projects")
        pump(app, until=lambda: not page._busy and page.snapshot.selected is not None)

        # Completion, and the way back to the overview.
        page.complete_button.invoke()
        pump(app, until=lambda: not page._busy and page.snapshot.selected.completed)
        page.back_button.invoke()
        pump(app, until=lambda: not page._busy and page.snapshot.selected is None)
        assert page.selected is None and [row.name for row in page.snapshot.completed] == ["My project"]
        assert page.snapshot.ongoing == []
    finally:
        close_app(app)
