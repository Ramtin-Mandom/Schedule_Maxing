"""One real native desktop -> backend -> second-device workflow, with disposable data."""
from app.planning.preferences import OptimizerMode
from app.planning.workflow import Freshness
from app.ui.projects_controller import ProjectsController
from tests.sync.conftest import InProcessTransport, PASSWORD
from tests.sync.test_desktop_account_page import Backend, open_app, close, pump, WEDNESDAY
from tests.sync.test_desktop_account_page import dialogs as dialogs  # noqa: F401
from tests.ui.test_desktop_app import fill_form
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401


def ok(result):
    assert result.ok, result.error
    return result.value


def test_complete_native_cloud_workflow(tmp_path, dialogs, server, make_device):
    backend = Backend(InProcessTransport(server.client))
    path = tmp_path / "milestone.db"
    app = open_app(path, tmp_path, backend)
    email = "milestone@example.com"
    try:
        # Begin offline: exact-minute fixed/flexible work and a real project.
        day = app.pages["day"]
        project = ok(ProjectsController(app.services.planning_controller, timezone="UTC").create("Paper"))
        fill_form(day, name="Meeting", fixed=True, start="540", end="613")
        day.form.submit_button.invoke()
        fill_form(day, name="Read", start="613", end="720", duration="13")
        day.form.submit_button.invoke()
        planning = app.services.planning_controller
        read = ok(planning.list_tasks())[0]
        read = ok(planning.add_or_update_task(read.model_copy(update={"project_id": project.id}),
                                               expected_version=read.version))
        account = app.account_controller
        ok(account.configure_backend("http://backend.test"))
        ok(account.register(email, PASSWORD, display_name="Milestone tester"))
        ok(account.sign_in(email, PASSWORD))
        app.apply_workspace()
        app.show_page("account")
        page = app.pages["account"]
        pump(app, lambda: not page.busy)
        page.review_association()
        page.dialog.cancel()
        assert app.services.connection.execute("SELECT user_id FROM tasks WHERE id=?", (str(read.id),)).fetchone()[0] is None
        page.review_association()
        page.dialog.primary_button.invoke()
        pump(app, lambda: not page.busy)
        assert "Milestone tester" in page.profile_label.cget("text")

        app.show_page("settings")
        preferences = app.pages["settings"]
        preferences.change("day_window", "save", ("9:00 AM", "5:00 PM"))
        assert preferences.preference_notice.tone == "success"
        app.show_page("day")
        day = app.pages["day"]
        planning = app.services.planning_controller
        day.make_schedule_button.invoke()
        pump(app, lambda: not day._busy)
        initial = ok(planning.get_placements(WEDNESDAY))
        assert len(initial) == 1 and initial[0].planned_start.minute == 13
        day.make_schedule_button.invoke()
        pump(app, lambda: not day._busy)
        assert ok(planning.get_placements(WEDNESDAY)) == initial

        # High-priority additions do not move already scheduled work.
        fill_form(day, name="Write", duration="43", start="613", end="900")
        day.form.priority_select.variable.set("10")
        day.form.submit_button.invoke()
        day.make_schedule_button.invoke()
        pump(app, lambda: not day._busy)
        saved = ok(planning.get_placements(WEDNESDAY))
        retained = next(p for p in saved if p.task_id == read.id)
        assert retained.id == initial[0].id and retained.planned_start == initial[0].planned_start
        for key in ("week", "month"):
            app.show_page(key)
            assert len(app.pages[key].snapshot.day(WEDNESDAY).timed) == 3  # block plus two tasks

        # Execution history remains linked to the protected placement while another task is edited.
        execution = ok(app.services.execution_controller.get_or_create_canonical_execution(
            ok(planning.get_task(read.id)), retained))
        for action in ("start", "pause", "resume", "complete"):
            ok(getattr(app.services.execution_controller, action)(execution.id))
        writing = next(t for t in ok(planning.list_tasks()) if t.name == "Write")
        ok(planning.add_or_update_task(writing.model_copy(update={"name": "Write revised"}),
                                        expected_version=writing.version))
        app.show_page("day")
        day.engine_select.choose("ADHD friendly")
        pump(app, lambda: not day._busy)
        assert day.snapshot.freshness == Freshness.STALE
        day.regenerate()
        pump(app, lambda: not day._busy)
        assert day.snapshot.freshness == Freshness.CURRENT
        assert ok(planning.resolve_preferences(WEDNESDAY)).optimizer_mode == OptimizerMode.ADHD_FRIENDLY
        assert ok(app.services.execution_controller.find_execution_for_placement(retained.id)).id == execution.id

        csv_path = tmp_path / "roundtrip.csv"
        ok(day.page_controller.export_csv(str(csv_path)))
        csv_plan = ok(day.page_controller.csv_plan(str(csv_path)))
        before = ok(planning.get_placements(WEDNESDAY))
        ok(day.page_controller.apply_csv(csv_plan))
        assert ok(planning.get_placements(WEDNESDAY)) == before
        assert ok(account.sync_now()).status == "ok"

        other = make_device("second", InProcessTransport(server.client))
        other.sign_in(email)
        assert other.sync_now().status == "ok"
        assert other.planning.get_task(read.id).project_id == project.id
        assert other.planning.date_preferences(WEDNESDAY).overrides.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
        assert other.executions.get_execution(execution.id).scheduled_task_id == retained.id
        remote = other.planning.get_task(writing.id)
        other.planning.update_task(remote.model_copy(update={"name": "Remote draft"}), expected_version=remote.version)
        assert other.sync_now().status == "ok"
        local = ok(planning.get_task(writing.id))
        ok(planning.add_or_update_task(local.model_copy(update={"name": "Local draft"}), expected_version=local.version))
        assert ok(account.sync_now()).conflicts == 1
        conflict = ok(account.conflicts())[0]
        ok(account.resolve(conflict.id, "accept_remote"))
        assert ok(planning.get_task(writing.id)).name == "Remote draft"
        assert ok(account.sync_now()).status == "ok"
    finally:
        close(app)

    app = open_app(path, tmp_path, backend)
    try:
        planning = app.services.planning_controller
        assert ok(planning.get_task(read.id)).project_id == project.id
        assert ok(planning.resolve_preferences(WEDNESDAY)).optimizer_mode == OptimizerMode.ADHD_FRIENDLY
        assert ok(app.services.execution_controller.find_execution_for_placement(retained.id)).id == execution.id
        ok(app.account_controller.sign_in(email, PASSWORD))
        app.apply_workspace()
        ok(app.account_controller.sign_out())
        app.apply_workspace()
        assert ok(app.services.planning_controller.list_tasks()) == []
        assert dialogs.errors == []
    finally:
        close(app)
