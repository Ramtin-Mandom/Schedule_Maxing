"""Transactional rescheduling (Milestone 3): occurrence/supersession identity,
cleanup of superseded placements outside the rescheduled range only, and
dependencies outside the range resolved from persisted completion/placement
state (completed / scheduled / skipped / cancelled / pending / missing stay
distinguishable).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.service import ExecutionService
from app.planning.application import GenerationProvenance, PlanningService, RangeScope
from app.planning.errors import VersionConflictError
from app.planning.models import DayScheduleOutput, RecurrenceSpec, ScheduledTask, Task, TaskRegistry, compute_total_score
from app.planning.occurrence import occurrence_key
from app.planning.preferences import OptimizerMode
from app.planning.repository import PlanningRepository
from app.planning.service import DayResultStatus
from app.ui.planning_controller import PlanningController

MON, TUE, WED, THU = date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 6, 6)
LAST_MON = date(2024, 5, 27)


def make_task(name: str, **overrides) -> Task:
    return Task(**{"name": name, "category": "study", "estimated_duration_minutes": 60, "priority": 5, **overrides})


def placement(task: Task, day: date, hour: int = 9) -> ScheduledTask:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return ScheduledTask(task_id=task.id, planned_date=day, timezone="UTC", planned_start=start,
                         planned_end=start + timedelta(hours=1))


def output(day: date, tasks: list[Task], placements: list[ScheduledTask]) -> DayScheduleOutput:
    return DayScheduleOutput(date=day, timezone="UTC", tasks=TaskRegistry(tasks={t.id: t for t in tasks}),
                             placements=placements, total_score=compute_total_score(placements))


def provenance(start: date, end: date) -> GenerationProvenance:
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    return GenerationProvenance(
        allocation_id=uuid.uuid4(), range_start=start, range_end=end, range_scope="planned", fingerprint="f" * 64,
        timezone="UTC", engine_modes={day: OptimizerMode.PRECISE_GREEDY for day in days},
        generated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )


# -----------------------------------------------------------------------------
# Occurrence identity
# -----------------------------------------------------------------------------


def test_occurrence_identity_is_the_task_or_one_date_of_a_recurring_template() -> None:
    single = make_task("Single")
    recurring = make_task("Daily", recurrence=RecurrenceSpec(frequency="daily"))

    assert occurrence_key(placement(single, MON), single) == occurrence_key(placement(single, WED), single)
    assert occurrence_key(placement(recurring, MON), recurring) != occurrence_key(placement(recurring, WED), recurring)
    assert occurrence_key(placement(recurring, MON, 9), recurring) == occurrence_key(placement(recurring, MON, 15), recurring)
    with pytest.raises(ValueError):
        occurrence_key(placement(single, MON), recurring)


# -----------------------------------------------------------------------------
# Supersession cleanup
# -----------------------------------------------------------------------------


def test_rescheduling_never_touches_placements_outside_the_range(
    planning_service: PlanningService, execution_service: ExecutionService
) -> None:
    moved = make_task("Moved")
    untouched = make_task("Not rescheduled")
    template = make_task("Daily", recurrence=RecurrenceSpec(frequency="daily"))
    started = make_task("Started elsewhere")
    merely_linked = make_task("Linked, not started")
    planning_service.save_tasks([moved, untouched, template, started, merely_linked])

    wednesday = planning_service.replace_placements(WED, WED, [
        placement(moved, WED, 8), placement(untouched, WED, 10), placement(template, WED, 12),
        placement(started, WED, 14), placement(merely_linked, WED, 16),
    ]).placements
    by_task = {p.task_id: p for p in wednesday}
    thursday = planning_service.replace_placements(THU, THU, [placement(untouched, THU)]).placements
    history = execution_service.get_or_create_canonical_execution(started, by_task[started.id])
    execution_service.start(history.id)
    linked = execution_service.get_or_create_canonical_execution(merely_linked, by_task[merely_linked.id])

    new_monday = [placement(task, MON, hour) for task, hour in ((moved, 8), (template, 10), (started, 12), (merely_linked, 14))]
    result = planning_service.reschedule_range(
        MON, MON, {MON: output(MON, [moved, template, started, merely_linked], new_monday)},
        expected_versions={}, provenance=provenance(MON, MON),
    )

    # Milestone 6: the save writes only its own dates. (A generation never even produces a second placement of an
    # occurrence live elsewhere -- it reports it instead; see test_make_schedule_leaves_a_task_planned_elsewhere.)
    assert result.superseded_ids == [] and result.history_protected_ids == []
    assert planning_service.placements_for_date(WED) == wednesday  # untouched: same ids, versions, no tombstones
    assert planning_service.placements_for_date(THU) == thursday  # an unrelated date is untouched
    assert execution_service.get_execution(linked.id).scheduled_task_id == by_task[merely_linked.id].id  # history kept
    assert len(planning_service.placements_for_date(MON)) == 4


def test_rescheduling_requires_the_previous_placements_it_read(planning_service: PlanningService) -> None:
    task = make_task("Task")
    planning_service.save_task(task)
    [current] = planning_service.replace_placements(MON, MON, [placement(task, MON)]).placements

    with pytest.raises(VersionConflictError):  # generated from an empty day, but the day has a placement now
        planning_service.reschedule_range(
            MON, MON, {MON: output(MON, [task], [placement(task, MON, 15)])},
            expected_versions={}, provenance=provenance(MON, MON),
        )
    assert planning_service.placements_for_date(MON) == [current]
    assert planning_service.generation_record(MON) is None


def test_make_schedule_leaves_a_task_planned_elsewhere(tmp_path: Path) -> None:
    connection = get_connection(tmp_path / "app.db")
    try:
        service = PlanningService(PlanningRepository(connection))
        controller = PlanningController(service=service, timezone="UTC", project_root=str(tmp_path))
        floating = controller.add_or_update_task(make_task("Floating")).value
        week = controller.schedule_range(MON, date(2024, 6, 9), scope=RangeScope.ELIGIBLE).value
        first_day = week.allocation.assignments[floating.id]
        other_day = TUE if first_day != TUE else WED

        before = service.placements_for_date(first_day)

        rerun = controller.schedule_range(other_day, other_day, scope=RangeScope.ELIGIBLE).value

        assert service.placements_for_date(other_day) == []  # no double booking ...
        assert service.placements_for_date(first_day) == before  # ... and nothing outside the range is touched
        assert rerun.superseded_ids == []
        assert rerun.kept_elsewhere[floating.id].id == before[0].id  # reported, not silently moved
        assert controller.day_state(first_day).value.status == DayResultStatus.GENERATED
    finally:
        connection.close()


# -----------------------------------------------------------------------------
# Dependencies outside the range
# -----------------------------------------------------------------------------
