"""Prompt 6: persisted projects, mixed-project allocation and actual native pages."""
from datetime import date, timedelta
import uuid

from app.planning.models import Task
from app.planning.scope import OwnerScope
from app.planning.preferences import PreferenceOverrides, DayWindowSpec
from app.ui.allocation_controller import AllocationController, filter_view
from app.ui.calendar_controller import CalendarController
from app.ui.day_controller import DayScheduleController
from app.ui.projects_controller import ProjectsController
from app.ui.app_services import open_app_services
from tests.ui.test_day_controller import services as services, db_path as db_path, ok  # noqa: F401

DAY = date(2026, 9, 24)


def task(planning, name, project=None, **kwargs):
    return ok(planning.add_or_update_task(Task(name=name, category="study", estimated_duration_minutes=13,
                                               priority=5, project_id=project, **kwargs)))


def test_project_crud_versions_reassign(services):
    planning = services.planning_controller
    projects = ProjectsController(planning, timezone="UTC")
    first = ok(projects.create("Research", "A description"))
    second = ok(projects.create("Research"))  # duplicate names keep different IDs
    original = task(planning, "Read", first.id)
    assert not projects.delete(first.id, expected_version=first.version).ok
    updated = ok(projects.update(first.id, "Paper", "Revised", expected_version=first.version))
    assert not projects.update(first.id, "Stale", "", expected_version=first.version).ok
    assert ok(projects.reassign_tasks(first.id, second.id)) == 1
    moved = ok(planning.get_task(original.id))
    assert moved.id == original.id and moved.project_id == second.id
    assert ok(projects.reassign_tasks(second.id, None)) == 1
    assert ok(planning.get_task(original.id)).project_id is None
    assert projects.delete(first.id, expected_version=updated.version).ok
    assert len(ok(ProjectsController(planning, timezone="UTC").load()).projects) == 1
    task(planning, "Stay here", second.id)
    assert not projects.reassign_tasks(second.id, uuid.uuid4()).ok
    assert len(ok(projects.load(second.id)).tasks) == 1


def test_allocation_filter_does_not_change_inputs_and_stale_preview_is_refused(services):
    planning = services.planning_controller
    projects = ProjectsController(planning, timezone="UTC")
    a, b = ok(projects.create("A")), ok(projects.create("B"))
    predecessor = task(planning, "First", a.id, required_date=DAY)
    successor = task(planning, "Second", b.id, required_date=DAY, dependency_ids=[predecessor.id])
    allocation = AllocationController(planning, timezone="UTC", selected=DAY)
    preview = ok(allocation.allocate())
    assert preview.assigned_count == 2
    filtered = filter_view(preview, b.id)
    assert filtered.assigned_count == 1 and filtered.fingerprint == preview.fingerprint
    assert [row.task_id for row in filtered.day(DAY).tasks] == [successor.id]
    assert ok(planning.get_placements(DAY)) == []

    assert allocation.schedule_date(preview, DAY).ok
    placements = ok(planning.get_placements(DAY))
    assert {p.task_id for p in placements} == {predecessor.id, successor.id}
    by_task = {p.task_id: p for p in placements}
    assert by_task[predecessor.id].planned_end <= by_task[successor.id].planned_start
    assert ok(planning.get_placements(DAY + timedelta(days=1))) == []
    calendar = ok(CalendarController(planning, mode="week", selected=DAY, timezone="UTC").load())
    assert [item.name for item in calendar.filtered(b.id).day(DAY).items] == ["Second"]
    assert len(calendar.day(DAY).items) == 2
    snapshot = ok(projects.load(b.id))
    assert snapshot.tasks[0].dates == (DAY,) and "Not scheduled" not in snapshot.tasks[0].status
    task(planning, "Changed", required_date=DAY)
    assert ok(allocation.is_stale(preview))
    assert not allocation.schedule_date(preview, DAY).ok
    assert ok(planning.get_placements(DAY)) == placements
    day = DayScheduleController(planning, anchor_date=DAY, timezone="UTC")
    day.allocation_context = (preview.period.start, preview.period.end, preview.fingerprint, DAY)
    assert not day.make_schedule_for(DAY).ok
    assert ok(planning.get_placements(DAY)) == placements


def test_scope_hides_projects_and_tasks(services):
    projects = ProjectsController(services.planning_controller, timezone="UTC")
    local = ok(projects.create("Local"))
    services.switch_workspace(OwnerScope.account(uuid.uuid4()))
    scoped = ProjectsController(services.planning_controller, timezone="UTC")
    assert ok(scoped.load()).projects == []
    assert not scoped.update(local.id, "Not mine", "", expected_version=local.version).ok
    owned = ok(scoped.create("Account"))
    assert owned.user_id == services.workspace.scope.user_id


def test_capacity_and_blocked_dependency_reasons_are_real(services):
    planning = services.planning_controller
    ok(planning.set_user_overrides(PreferenceOverrides(day_window=DayWindowSpec(start_minute=540, end_minute=550))))
    first = task(planning, "Too long", required_date=DAY)
    second = task(planning, "Depends on it", required_date=DAY, dependency_ids=[first.id])
    allocation = AllocationController(planning, timezone="UTC", selected=DAY)
    preview = ok(allocation.allocate())
    assert preview.assigned_count == 0
    rows = {row.task.task_id: row for row in preview.unallocated}
    assert set(rows) == {first.id, second.id}
    assert "free time" in rows[first.id].reason
    assert "depends on" in rows[second.id].reason
    assert all(row.explanation and row.certainty for row in rows.values())
    assert ok(planning.get_placements(DAY)) == []


def test_project_assignment_and_schedule_survive_reopen(tmp_path):
    path = tmp_path / "reopen.db"
    opened = open_app_services(path, timezone="UTC", project_root=str(tmp_path))
    try:
        projects = ProjectsController(opened.planning_controller, timezone="UTC")
        project = ok(projects.create("Durable"))
        original = task(opened.planning_controller, "Durable task", project.id, required_date=DAY)
        allocation = AllocationController(opened.planning_controller, timezone="UTC", selected=DAY)
        assert allocation.schedule_date(ok(allocation.allocate()), DAY).ok
    finally:
        opened.close()
    reopened = open_app_services(path, timezone="UTC", project_root=str(tmp_path))
    try:
        view = ok(ProjectsController(reopened.planning_controller, timezone="UTC").load(project.id))
        assert view.selected.name == "Durable"
        assert view.tasks[0].task_id == original.id and view.tasks[0].dates == (DAY,)
    finally:
        reopened.close()
