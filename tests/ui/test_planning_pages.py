from tests.ui.test_desktop_app import open_app, close_app, pump, WEDNESDAY
from tests.ui.test_desktop_app import dialogs as dialogs, pytestmark as pytestmark  # noqa: F401
from app.planning.models import Task


def test_native_projects_and_allocation(tmp_path, dialogs):
    app = open_app(tmp_path / "planning.db", tmp_path)
    try:
        app.show_page("projects")
        page = app.pages["projects"]
        pump(app, until=lambda: not page._busy)
        page.name.variable.set("My project")
        page.description.variable.set("Real persisted work")
        page.save_button.invoke()
        pump(app, until=lambda: not page._busy)
        assert page.snapshot.selected.name == "My project"
        planning = app.services.planning_controller
        saved = planning.add_or_update_task(Task(name="Study", category="study", priority=5,
                                                 estimated_duration_minutes=13, required_date=WEDNESDAY,
                                                 project_id=page.selected))
        assert saved.ok
        page.on_show()
        pump(app, until=lambda: not page._busy)
        assert page.snapshot.tasks[0].status == "Not scheduled"
        app.show_page("allocation")
        allocation = app.pages["allocation"]
        allocation.allocate_button.invoke()
        pump(app, until=lambda: not allocation._busy)
        assert allocation.preview.assigned_count == 1
        allocation.open_day(WEDNESDAY)
        pump(app, until=lambda: not allocation._busy)
        assert app.shell.current == "day"
        day = app.pages["day"]
        assert day.page_controller.allocation_context[2] == allocation.preview.fingerprint
        day.make_schedule_button.invoke()
        pump(app, until=lambda: not day._busy)
        assert len(planning.get_placements(WEDNESDAY).value) == 1
        app.show_page("projects")
        pump(app, until=lambda: not page._busy)
        assert page.snapshot.tasks[0].dates == (WEDNESDAY,)
    finally:
        close_app(app)
