"""Existing local data opens unchanged through the desktop's startup (open_app_services),
using temporary fixtures only (never a real user database):

- a genuine Milestone 3 database (schema v5) that was synchronized and associated --
  account-owned records with recurrence, tags, dependencies, a project, fixed blocks,
  a generated schedule with provenance, preference layers, a completed execution,
  server shadows and a pending outbox operation -- migrates to v6 without changing any
  id, timestamp, owner, local version, server version, cursor or queued operation, stays
  "current", and reopens in its owner's workspace;
- a genuine v1 database (pre-planning execution history) upgrades through every step
  and opens in the ownerless workspace with its history intact.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import LATEST_SCHEMA_VERSION, AppConnection, initialize_schema
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.models import FixedBlock, Project, RecurrenceFrequency, RecurrenceSpec, Task
from app.planning.preferences import OptimizerMode, PreferenceOverrides
from app.planning.repository import PlanningRepository
from app.planning.scope import OwnerScope
from app.planning.service import DayResultStatus
from app.ui import background
from app.ui.app_services import open_app_services
from app.ui.planning_controller import PlanningController
from tests.execution.test_migration_v2 import _build_v1_database

MON = date(2024, 6, 3)
ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ACCOUNT_KEY = "https://backend.test|aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PRESERVED_TABLES = ("projects", "tasks", "task_tags", "task_dependencies", "task_recurrence_weekdays", "fixed_blocks",
                    "scheduled_tasks", "schedule_generations", "preference_overrides", "executions", "work_sessions",
                    "sync_dirty", "sync_shadows", "sync_outbox")


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


def rows(connection, table: str, columns: list[str] | None = None) -> list[tuple]:
    selected = ", ".join(columns) if columns else "*"
    return [tuple(row) for row in connection.execute(f"SELECT {selected} FROM {table} ORDER BY 1, 2")]


def column_names(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


#: Placement columns added by schema v7 (Milestone 5); Milestone 3 code neither had nor wrote them.
V7_PLACEMENT_COLUMNS = ("task_category", "removal_reason", "superseded_by_id")
#: Schema v8 (task points and the execution snapshot of them) -- absent from a Milestone 3 database.
V8_COLUMNS = ("points",)
#: Schema v9 (recurrence expansion, docs/recurrence.md) -- task columns a Milestone 3 database lacks.
V9_TASK_COLUMNS = ("recurrence_start_date", "recurrence_timezone", "series_id", "occurrence_slot", "occurrence_state",
                   "series_version", "series_predecessor_id")
#: Schema v10 (manual placements, docs/execution-rescheduling.md) -- placement and execution columns.
V10_PLACEMENT_COLUMNS = {"origin": None, "preserved": 0}
V10_EXECUTION_COLUMNS = ("cancel_reason",)
#: Schema v12 (task types and placement planning snapshots, docs/productivity-redesign-plan.md).
V12_TASK_COLUMNS = ("task_type_id",)
V12_PLACEMENT_COLUMNS = ("task_name", "task_tags", "task_points", "task_estimate_minutes", "task_type_id",
                         "task_type_label")
#: Schema v14 (a project's planned dates, completion and milestones) -- project columns older code never wrote.
#: Schema v16: a task's kind and preferred third of the day.
V16_TASK_COLUMNS = ("kind", "preferred_time")
V14_PROJECT_COLUMNS = ("start_date", "estimated_end_date", "completed_at", "milestones", "task_defaults")


@contextmanager
def pre_v8_writers():
    """
    The planning and execution repositories as they were before schema v8: no points or recurrence columns --
    and a planning service that, like that code, knows no task types or planning snapshots (schema v12).
    """
    from app.execution import repository as execution_repository
    from app.planning import repository as planning_repository
    from app.planning.application import PlanningService as CurrentService
    from app.planning.models import PLACEMENT_SNAPSHOT_FIELDS

    absent = (*V8_COLUMNS, *V9_TASK_COLUMNS, *V10_EXECUTION_COLUMNS, *V12_TASK_COLUMNS, *V16_TASK_COLUMNS)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(CurrentService, "_resolve_task_types", lambda self, tasks, stored: tasks)
        patch.setattr(CurrentService, "placement_snapshots", lambda self, tasks: {
            task.id: dict.fromkeys(PLACEMENT_SNAPSHOT_FIELDS) for task in tasks})
        for module, columns_name, to_row_name, from_row_name in (
            (planning_repository, "_TASK_COLUMNS", "_task_to_row", None),
            (execution_repository, "_EXECUTION_COLUMNS", "_execution_to_row", "_row_to_execution"),
        ):
            columns, to_row = getattr(module, columns_name), getattr(module, to_row_name)
            patch.setattr(module, columns_name, tuple(c for c in columns if c not in absent))
            patch.setattr(module, to_row_name, lambda record, columns=columns, to_row=to_row: tuple(
                value for column, value in zip(columns, to_row(record)) if column not in absent))
            if from_row_name is not None:
                from_row = getattr(module, from_row_name)
                patch.setattr(module, from_row_name, lambda row, from_row=from_row: from_row(
                    {**dict(row), **{column: None for column in (*V8_COLUMNS, *V10_EXECUTION_COLUMNS)}}))
        project_columns, project_to_row = planning_repository._PROJECT_COLUMNS, planning_repository._project_to_row
        row_to_project = planning_repository._row_to_project
        patch.setattr(planning_repository, "_PROJECT_COLUMNS",
                      tuple(c for c in project_columns if c not in V14_PROJECT_COLUMNS))
        patch.setattr(planning_repository, "_project_to_row", lambda project: tuple(
            value for column, value in zip(project_columns, project_to_row(project))
            if column not in V14_PROJECT_COLUMNS))
        patch.setattr(planning_repository, "_row_to_project", lambda row: row_to_project(
            {**dict(row), **dict.fromkeys(V14_PROJECT_COLUMNS)}))
        original_row_to_task = planning_repository._row_to_task
        patch.setattr(planning_repository, "_row_to_task", lambda row, *rest: original_row_to_task(
            {**dict(row), "points": 1, "kind": "flexible", "preferred_time": None,
             **{column: None for column in (*V9_TASK_COLUMNS, *V12_TASK_COLUMNS)}}, *rest))
        # Schema v16: a fixed block's points.
        block_columns, block_to_row = planning_repository._FIXED_BLOCK_COLUMNS, planning_repository._fixed_block_to_row
        row_to_block = planning_repository._row_to_fixed_block
        patch.setattr(planning_repository, "_FIXED_BLOCK_COLUMNS", tuple(c for c in block_columns if c != "points"))
        patch.setattr(planning_repository, "_fixed_block_to_row", lambda block: tuple(
            value for column, value in zip(block_columns, block_to_row(block)) if column != "points"))
        patch.setattr(planning_repository, "_row_to_fixed_block", lambda row: row_to_block({**dict(row), "points": 0}))
        yield


@contextmanager
def milestone3_placement_writer():
    """The planning repository as Milestone 3 shipped it: placements without the v7 columns."""
    from app.planning import repository

    columns = repository._PLACEMENT_COLUMNS
    to_row, from_row = repository._placement_to_row, repository._row_to_placement
    absent = (*V7_PLACEMENT_COLUMNS, *V10_PLACEMENT_COLUMNS, *V12_PLACEMENT_COLUMNS)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repository, "_PLACEMENT_COLUMNS", tuple(c for c in columns if c not in absent))
        patch.setattr(repository, "_placement_to_row", lambda placement: tuple(
            value for column, value in zip(columns, to_row(placement)) if column not in absent))
        patch.setattr(repository, "_row_to_placement", lambda row: from_row(
            {**dict(row), **{column: None for column in (*V7_PLACEMENT_COLUMNS, *V12_PLACEMENT_COLUMNS)},
             **V10_PLACEMENT_COLUMNS}))
        yield


def build_milestone3_database(db_path: Path, project_root: Path) -> dict:
    """A v5 database as Milestone 3 left it after association and a sync (written by v5-era code paths)."""
    connection = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False, factory=AppConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    initialize_schema(connection, target_version=5)
    older_code = pre_v8_writers()
    older_code.__enter__()
    connection.execute(
        "INSERT INTO sync_accounts (account_key, backend_url, user_id, email, pull_cursor, active, associated_at, "
        "created_at) VALUES (?, 'https://backend.test', ?, 'alice@example.com', 42, 1, ?, ?)",
        (ACCOUNT_KEY, str(ALICE), "2024-06-01T09:00:00+00:00", "2024-06-01T08:00:00+00:00"),
    )
    planning = PlanningService(PlanningRepository(connection))
    project = planning.create_project(Project(user_id=ALICE, name="Thesis"))
    outline = planning.create_task(Task(user_id=ALICE, name="Outline", category="study", estimated_duration_minutes=45,
                                        priority=7, tags=["writing", "deep"], preferred_dates=[MON], project_id=project.id))
    planning.create_task(Task(user_id=ALICE, name="Draft", category="study", estimated_duration_minutes=60, priority=6,
                              preferred_dates=[MON], dependency_ids=[outline.id], project_id=project.id))
    planning.create_task(Task(user_id=ALICE, name="Gym", category="exercise", estimated_duration_minutes=30, priority=4,
                              recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.WEEKLY, weekdays=[0, 2, 4])))
    start = datetime(2024, 6, 3, 0, tzinfo=timezone.utc)
    planning.create_fixed_block(FixedBlock(user_id=ALICE, label="Sleep", category="sleep", planned_date=MON,
                                           timezone="UTC", planned_start=start, planned_end=start + timedelta(hours=7)))
    planning.save_user_preferences(PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY))
    planning.save_date_preferences(MON, PreferenceOverrides(optimizer_mode=OptimizerMode.PRECISE_GREEDY))
    connection.execute("UPDATE preference_overrides SET user_id = ?", (str(ALICE),))  # as association left them
    # Milestone 3's desktop Make Schedule (device-wide, the same template the desktop opens with below).
    with milestone3_placement_writer():
        run = PlanningController(service=planning, timezone="UTC", project_root=str(project_root)).schedule_range(
            MON, MON)
    assert run.ok, run.error
    connection.execute("UPDATE schedule_generations SET user_id = ?", (str(ALICE),))

    with milestone3_placement_writer():
        placement = planning.placements_for_date(MON)[0]
    executions = ExecutionService(ExecutionRepository(connection))
    execution = executions.get_or_create_canonical_execution(planning.get_task(placement.task_id), placement,
                                                             user_id=ALICE)
    for action in (executions.start, executions.complete):
        action(execution.id)

    # The server's state of one task (server version 7, unlike the local version) and one queued operation.
    connection.execute(
        "INSERT INTO sync_shadows (account_key, entity_type, entity_id, server_version, deleted, record) "
        "VALUES (?, 'task', ?, 7, 0, '{}')", (ACCOUNT_KEY, str(outline.id)))
    connection.execute(
        "INSERT INTO sync_outbox (op_id, account_key, entity_type, entity_id, local_id, kind, base_version, payload, "
        "local_rev, state, created_at) VALUES ('op-1', ?, 'task', ?, ?, 'update', 7, '{}', 3, 'pending', ?)",
        (ACCOUNT_KEY, str(outline.id), str(outline.id), "2024-06-02T10:00:00+00:00"))
    older_code.__exit__(None, None, None)
    connection.close()
    return {"placement_id": placement.id, "execution_id": execution.id, "outline_id": outline.id}


def test_a_synchronized_milestone3_database_opens_unchanged_in_its_owners_workspace(tmp_path: Path) -> None:
    db = tmp_path / "m3.db"
    facts = build_milestone3_database(db, tmp_path)
    before_connection = sqlite3.connect(str(db))
    assert before_connection.execute("PRAGMA user_version").fetchone()[0] == 5
    owners = {row[0] for table in ("tasks", "fixed_blocks", "scheduled_tasks", "executions")
              for row in before_connection.execute(f"SELECT user_id FROM {table}")}
    assert owners == {str(ALICE)}  # every record is the account's, as association leaves it
    before_columns = {table: column_names(before_connection, table) for table in PRESERVED_TABLES}
    before = {table: rows(before_connection, table) for table in PRESERVED_TABLES}
    before_connection.close()

    for _ in range(2):  # the migrating open, then a plain reopen
        services = open_app_services(db, timezone="UTC", project_root=str(tmp_path))
        try:
            connection = services.connection
            assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            after = {table: rows(connection, table, before_columns[table]) for table in PRESERVED_TABLES}
            # Schema v13 queues the task types that v12 derived for upload; every existing mark is untouched.
            after["sync_dirty"] = [row for row in after["sync_dirty"] if row[0] != "task_type"]
            # Schema v16 completes (for 0 points) the fixed blocks saved before blocks could be completed: one
            # new execution per block, its id derived from the block's -- never a second one on a reopen.
            from app.planning.models import fixed_block_execution_id

            added = {str(fixed_block_execution_id(uuid.UUID(row[0])))
                     for row in connection.execute("SELECT id FROM fixed_blocks")}
            backfilled = [row for row in after["executions"] if row[0] in added]
            assert len(backfilled) == len(added) == 1
            after["executions"] = [row for row in after["executions"] if row[0] not in added]
            assert after == before
            assert set(rows(connection, "scheduled_tasks", list(V7_PLACEMENT_COLUMNS))) == {(None, None, None)}
            account = connection.execute("SELECT pull_cursor, active, associated_at, last_synced_at FROM sync_accounts "
                                         "WHERE account_key = ?", (ACCOUNT_KEY,)).fetchone()
            assert tuple(account) == (42, 1, "2024-06-01T09:00:00+00:00", None)

            assert services.workspace.scope == OwnerScope.account(ALICE)
            controller = services.planning_controller
            tasks = {task.name: task for task in controller.list_tasks().value}
            assert set(tasks) == {"Outline", "Draft", "Gym"}
            assert tasks["Gym"].recurrence.weekdays == [0, 2, 4] and tasks["Outline"].tags == ["writing", "deep"]
            assert tasks["Draft"].dependency_ids == [facts["outline_id"]]
            state = controller.day_state(MON).value
            assert state.status == DayResultStatus.GENERATED  # the migration did not make the schedule stale
            assert facts["placement_id"] in {p.id for p in state.result.placements}
            assert controller.resolve_preferences(MON).value.optimizer_mode == OptimizerMode.PRECISE_GREEDY
            assert controller.resolve_preferences(MON + timedelta(days=1)).value.optimizer_mode == \
                OptimizerMode.ADHD_FRIENDLY

            execution = services.execution_controller.find_execution_for_placement(facts["placement_id"]).value
            assert execution.id == facts["execution_id"] and execution.status.value == "completed"
            status = services.sync_service.status()
            assert status.configured is False and status.last_successful_sync_at is None
        finally:
            services.close()


def test_a_version1_database_upgrades_and_keeps_its_history_in_the_ownerless_workspace(tmp_path: Path) -> None:
    db = tmp_path / "v1.db"
    _build_v1_database(db)
    legacy = sqlite3.connect(str(db))
    history = rows(legacy, "executions")
    sessions = rows(legacy, "work_sessions")
    legacy.close()

    services = open_app_services(db, timezone="UTC", project_root=str(tmp_path))
    try:
        assert services.workspace.scope == OwnerScope.ownerless()
        listed = services.execution_controller.list_executions().value
        assert sorted(execution.id for execution in listed) == sorted(row[0] for row in history)
        kept_sessions = services.connection.execute("SELECT id, execution_id, started_at FROM work_sessions").fetchall()
        assert sorted(tuple(row) for row in kept_sessions) == sorted((row[0], row[1], row[2]) for row in sessions)
        assert services.productivity_controller.build_dashboard().ok
    finally:
        services.close()
