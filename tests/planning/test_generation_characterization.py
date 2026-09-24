"""Characterization of the canonical full generation (written before the shared
workflow took over the controller's generation path): the desktop controller's
Make Schedule / generate_day must keep producing exactly what the direct
recipe -- allocate_tasks, then generate_selected_day per date with the saved
placements as previous_result -- produces, for both engines, and
app/planning/workflow.py's FULL mode (without history protection) must too."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.planning import workflow
from app.planning.allocation import allocate_tasks
from app.planning.application import PlanningService, RangeScope
from app.planning.external_dependencies import allocation_dates, satisfaction_instants
from app.planning.models import FixedBlock, Task
from app.planning.preferences import OptimizerMode, PreferenceOverrides, resolve_day_preferences
from app.planning.service import generate_selected_day
from app.ui.planning_controller import PlanningController

MON, TUE, WED = date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)


def seed(service: PlanningService) -> list[Task]:
    tasks = [
        Task(name="Essay", category="study", estimated_duration_minutes=95, priority=9, required=True, required_date=MON),
        Task(name="Gym", category="health", estimated_duration_minutes=60, priority=4, preferred_dates=[TUE]),
        Task(name="Reading", category="study", estimated_duration_minutes=45, priority=6, preferred_dates=[MON]),
        Task(name="Errands", category="chores", estimated_duration_minutes=30, priority=3, preferred_dates=[WED]),
    ]
    saved = service.save_tasks(tasks)
    dependent = service.create_task(Task(name="Review", category="study", estimated_duration_minutes=25, priority=7,
                                         preferred_dates=[TUE], dependency_ids=[saved[0].id]))
    for day in (MON, TUE, WED):
        start = datetime(day.year, day.month, day.day, 0, tzinfo=timezone.utc)
        service.create_fixed_block(FixedBlock(label="Sleep", planned_date=day, timezone="UTC",
                                              planned_start=start, planned_end=start + timedelta(hours=8)))
    return [*saved, dependent]


def direct_recipe(service: PlanningService, mode: OptimizerMode) -> dict:
    """The pre-workflow recipe, spelled out."""
    dates = [MON, TUE, WED]
    loaded = service.load_range(MON, WED, scope=RangeScope.PLANNED)
    prefs = {d: resolve_day_preferences(date=d, timezone="UTC", yaml_overrides=service.preference_template(),
                                        user_overrides=PreferenceOverrides(optimizer_mode=mode)) for d in dates}
    external = service.external_dependencies(loaded.tasks.tasks.values(), MON, WED, "UTC")
    allocation = allocate_tasks(start_date=MON, end_date=WED, tasks=loaded.tasks, task_ids=loaded.task_ids,
                                preferences_by_date=prefs, fixed_blocks_by_date=loaded.fixed_blocks_by_date,
                                external_dependency_dates=allocation_dates(external))
    result = {}
    for day in dates:
        output, _ = generate_selected_day(allocation, day, loaded.tasks, {day: prefs[day]},
                                          {day: loaded.fixed_blocks_by_date[day]},
                                          external_dependency_satisfaction=satisfaction_instants(external))
        result[day] = sorted((p.task_id, p.planned_start, p.planned_end, p.score) for p in output.placements)
    return result


def saved(service: PlanningService) -> dict:
    return {day: sorted((p.task_id, p.planned_start, p.planned_end, p.score) for p in service.placements_for_date(day))
            for day in (MON, TUE, WED)}


@pytest.mark.parametrize("mode", [OptimizerMode.PRECISE_GREEDY, OptimizerMode.ADHD_FRIENDLY])
def test_the_controller_and_the_workflow_match_the_direct_recipe(planning_service: PlanningService, mode) -> None:
    seed(planning_service)
    planning_service.save_user_preferences(PreferenceOverrides(optimizer_mode=mode))
    expected = direct_recipe(planning_service, mode)

    controller = PlanningController(service=planning_service, timezone="UTC")
    run = controller.schedule_range(MON, WED)
    assert run.ok, run.error
    assert saved(planning_service) == expected
    ids = {p.id for day in (MON, TUE, WED) for p in planning_service.placements_for_date(day)}

    # Re-running on unchanged inputs keeps every id (previous_result reuse), through the workflow too.
    outcome = workflow.generate(planning_service, range_start=MON, range_end=WED, timezone_name="UTC",
                                template=controller._yaml_overrides)
    assert outcome.status == "already_current"
    planning_service.create_task(Task(name="Late addition", category="work", estimated_duration_minutes=20,
                                      priority=1, preferred_dates=[WED]))
    regenerated = workflow.generate(planning_service, range_start=MON, range_end=WED, timezone_name="UTC",
                                    template=controller._yaml_overrides)
    assert regenerated.status == "generated"
    unchanged = {p.id for day in (MON, TUE) for p in planning_service.placements_for_date(day)}
    assert unchanged <= ids
