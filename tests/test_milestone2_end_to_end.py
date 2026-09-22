"""Milestone 2 end-to-end acceptance test, headless, on a temporary database:

import CSV + create a task -> Make Schedule (optimize + save) -> create,
start, and complete an execution with feedback -> close every connection ->
reopen a fresh service/controller stack -> verify stable task/placement
ids, dates, timestamps, versions, dependencies, execution history, and
work sessions -> export (stored planning CSV, execution history) -> a plain
restart imports nothing.

It uses the same objects the desktop app wires together
(app/ui/app_services.py + SchedulePageController), so it exercises the real
UI -> controller -> service -> repository -> SQLite path.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.csv_import import ImportMode
from app.planning.repository import PlanningRepository
from app.ui import background
from app.ui.app_services import open_app_services
from app.ui.schedule_page_controller import SchedulePageController

ANCHOR = date(2026, 1, 5)


def open_stack(db_path: Path, project_root: Path):
    services = open_app_services(db_path, timezone="UTC", project_root=str(project_root))
    page = SchedulePageController(services.planning_controller, number_of_days=7, anchor_date=ANCHOR, timezone="UTC")
    return services, page


def persisted_state(services) -> dict:
    planning = PlanningService(PlanningRepository(services.connection))
    executions = ExecutionService(ExecutionRepository(services.connection))
    all_executions = executions.list_executions()
    return {
        "tasks": planning.list_tasks(),
        "blocks": planning.list_fixed_blocks(),
        "placements": planning.list_placements(),
        "executions": all_executions,
        "sessions": {execution.id: executions.list_sessions(execution.id) for execution in all_executions},
    }


def test_full_persistent_lifecycle(tmp_path: Path) -> None:
    previous_registry = background.current_registry()
    db_path = tmp_path / "lifecycle.db"
    try:
        services, page = open_stack(db_path, tmp_path)

        # 1. Import a CSV (append) and add one task through the form.
        imported = page.import_csv("samples/inputs/valid_multi_day_two_days.csv", ImportMode.APPEND)
        assert imported.ok, imported.error
        created = page.submit_task_form({
            "name": "Plan week", "day": "3", "category": "work", "tag": "admin", "fixed": "False",
            "start_time": "540", "end_time": "720", "duration": "60", "priority": "6",
        })
        assert created.ok, created.error

        # 2. Optimize and save the whole page range in one transaction.
        run = page.make_schedule()
        assert run.ok, run.error
        executables = run.value.snapshot.executables
        assert {e.placement.planned_date for e in executables} == {date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)}

        # 3. Execute one placement: start, pause, resume, complete with feedback.
        target = next(e for e in executables if e.task.name == "Write Draft")
        controller = services.execution_controller
        execution = controller.get_or_create_canonical_execution(target.task, target.placement).value
        for action in ("start", "pause", "resume", "complete"):
            assert getattr(controller, action)(execution.id).ok
        assert controller.record_feedback(execution.id, focus_rating=4, note="went well").ok

        before = persisted_state(services)
        write_draft = next(task for task in before["tasks"] if task.name == "Write Draft")
        research = next(task for task in before["tasks"] if task.name == "Research Topic")
        assert write_draft.dependency_ids == [research.id]
        services.close()

        # 4. Reopen a completely fresh stack on the same file.
        services, page = open_stack(db_path, tmp_path)
        after = persisted_state(services)

        assert after == before  # ids, dates, exact timestamps, versions, scores, dependencies
        [history] = after["executions"]
        assert history.status == ExecutionStatus.COMPLETED
        assert (history.task_id, history.scheduled_task_id) == (target.task.id, target.placement.id)
        assert history.focus_rating == 4 and history.note == "went well"
        assert history.canonical_planned_start == target.placement.planned_start
        sessions = after["sessions"][history.id]
        assert len(sessions) == 2 and all(session.ended_at is not None for session in sessions)

        snapshot = page.load().value
        assert [e.placement for e in snapshot.executables] == [e.placement for e in executables]
        restored = services.execution_controller.find_execution_for_placement(target.placement.id).value
        assert restored.id == history.id

        # 5. Exports read SQLite and do not change it.
        planning_csv = tmp_path / "planning.csv"
        exported = page.export_csv(str(planning_csv))
        assert exported.ok, exported.error
        with planning_csv.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        placement_rows = {row["id"]: row for row in rows if row["record_type"] == "placement"}
        assert placement_rows[str(target.placement.id)]["start_utc"] == target.placement.planned_start.isoformat()
        task_rows = {row["id"]: row for row in rows if row["record_type"] == "task"}
        assert json.loads(task_rows[str(write_draft.id)]["dependency_ids"]) == [str(research.id)]

        history_csv, history_json = tmp_path / "history.csv", tmp_path / "history.json"
        assert services.productivity_controller.export_execution_history(history_csv, "csv").value == 1
        assert services.productivity_controller.export_execution_history(history_json, "json").value == 1
        assert json.loads(history_json.read_text())[0]["scheduled_task_id"] == str(target.placement.id)
        dashboard = services.productivity_controller.build_dashboard()
        assert dashboard.ok and dashboard.value.observation_count == 1

        assert persisted_state(services) == after
        services.close()

        # 6. A plain restart restores everything and imports nothing.
        services, _ = open_stack(db_path, tmp_path)
        try:
            assert persisted_state(services) == after
        finally:
            services.close()
    finally:
        background.install_registry(previous_registry)
