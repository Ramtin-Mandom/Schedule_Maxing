"""The shared scheduling workflow (app/planning/workflow.py) on the local SQLite
store: already_current writes nothing (change capture included), incremental
generation never duplicates an occurrence, and an input written by another
connection while the engine runs refuses the save."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.execution.db import get_connection
from app.planning import workflow
from app.planning.application import PlanningService
from app.planning.errors import StaleInputsError
from app.planning.models import FixedBlock, Task
from app.planning.repository import PlanningRepository
from app.planning.workflow import GenerationMode

MON, TUE = date(2026, 9, 21), date(2026, 9, 22)


def task(name: str, **extra) -> Task:
    return Task(name=name, category="study", estimated_duration_minutes=60, priority=5, **extra)


def dirty(connection) -> list[tuple]:
    return [tuple(row) for row in connection.execute("SELECT entity_type, entity_id, local_rev FROM sync_dirty ORDER BY 1, 2")]


def live_placements(service: PlanningService, task_id) -> list:
    return service.active_placements_for_tasks([task_id]).get(task_id, [])


def test_an_already_current_generation_writes_nothing(planning_service: PlanningService, connection) -> None:
    planning_service.create_task(task("Read", preferred_dates=[MON]))
    first = workflow.generate(planning_service, range_start=MON, range_end=TUE, timezone_name="UTC")
    assert first.status == "generated"
    before = (dirty(connection), [tuple(r) for r in connection.execute("SELECT * FROM scheduled_tasks")],
              [tuple(r) for r in connection.execute("SELECT * FROM schedule_generations")])

    again = workflow.generate(planning_service, range_start=MON, range_end=TUE, timezone_name="UTC",
                              mode=GenerationMode.INCREMENTAL)
    assert again.status == "already_current" and again.reschedule is None
    assert (dirty(connection), [tuple(r) for r in connection.execute("SELECT * FROM scheduled_tasks")],
            [tuple(r) for r in connection.execute("SELECT * FROM schedule_generations")]) == before


def test_incremental_generation_never_duplicates_an_occurrence(planning_service: PlanningService) -> None:
    essay = planning_service.create_task(task("Essay", preferred_dates=[MON]))
    workflow.generate(planning_service, range_start=MON, range_end=MON, timezone_name="UTC")
    [monday] = live_placements(planning_service, essay.id)

    # The task now prefers Tuesday, so the range's allocation assigns it there -- but it is already scheduled.
    planning_service.update_task(essay.model_copy(update={"preferred_dates": [TUE]}), expected_version=essay.version)
    outcome = workflow.generate(planning_service, range_start=MON, range_end=TUE, generate_start=TUE,
                                timezone_name="UTC", mode=GenerationMode.INCREMENTAL)

    assert outcome.allocation.assignments[essay.id] == TUE
    assert live_placements(planning_service, essay.id) == [monday]  # kept where it was, not placed twice


def test_full_generation_leaves_an_occurrence_live_outside_its_dates(planning_service: PlanningService) -> None:
    essay = planning_service.create_task(task("Essay", preferred_dates=[MON]))
    workflow.generate(planning_service, range_start=MON, range_end=MON, timezone_name="UTC")
    [monday] = live_placements(planning_service, essay.id)
    planning_service.update_task(essay.model_copy(update={"preferred_dates": [TUE]}), expected_version=essay.version)
    outcome = workflow.generate(planning_service, range_start=MON, range_end=TUE, generate_start=TUE,
                                timezone_name="UTC")
    # Generating Tuesday never touches Monday: the occurrence stays there and is reported, not copied or moved.
    assert live_placements(planning_service, essay.id) == [monday]
    assert outcome.kept_elsewhere[essay.id].id == monday.id


def test_a_write_by_another_connection_during_generation_refuses_the_save(db_path, monkeypatch) -> None:
    connection = get_connection(db_path)
    other = get_connection(db_path)
    try:
        service = PlanningService(PlanningRepository(connection))
        rival = PlanningService(PlanningRepository(other))
        service.create_task(task("Read", preferred_dates=[MON]))
        real = workflow.generate_selected_day

        def racing(*args, **kwargs):
            start = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
            rival.create_fixed_block(FixedBlock(label="Surprise", planned_date=MON, timezone="UTC",
                                                planned_start=start, planned_end=start + timedelta(hours=1)))
            return real(*args, **kwargs)

        monkeypatch.setattr(workflow, "generate_selected_day", racing)
        with pytest.raises(StaleInputsError):
            workflow.generate(service, range_start=MON, range_end=MON, timezone_name="UTC")
        assert service.placements_for_date(MON) == [] and service.generation_records(MON, MON) == {}
    finally:
        other.close()
        connection.close()
