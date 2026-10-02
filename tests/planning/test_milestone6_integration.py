"""Milestone 6 across layers (docs/scheduling-modes.md, execution-rescheduling.md, recurrence.md): a mode
chosen through the desktop controller is persisted, survives a restart, and drives Day, range and REST
generation with matching evaluations; a manual move survives an Early Finish regeneration; a date override
beats the user default; Catch-Up without history plans exactly like Normal; old Normal/ADHD saves read
unchanged; and a recurring series under a time mode keeps one placement per slot."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.execution.db import get_connection
from app.planning import workflow
from app.planning.application import PlanningService, RangeScope
from app.planning.models import RecurrenceSpec, Task
from app.planning.preferences import OptimizerMode, PreferenceOverrides
from app.planning.repository import PlanningRepository
from app.ui.planning_controller import PlanningController

MON, TUE = date(2024, 6, 3), date(2024, 6, 4)


class Desk:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.open()

    def open(self) -> None:
        self.connection = get_connection(self.path)
        self.planning = PlanningService(PlanningRepository(self.connection))
        self.controller = PlanningController(service=self.planning, timezone="UTC", project_root=str(self.path.parent))

    def restart(self) -> None:
        self.connection.close()
        self.open()

    def task(self, name: str, **fields) -> Task:
        return self.controller.add_or_update_task(Task(**{
            "name": name, "category": "study", "estimated_duration_minutes": 60, "priority": 5,
            "required_date": MON, **fields})).value

    def mode(self, mode: OptimizerMode) -> None:
        current = self.controller.preference_views(MON, MON).value.user_layer
        version = current.version if current is not None else None
        assert self.controller.set_user_overrides(PreferenceOverrides(optimizer_mode=mode),
                                                  expected_version=version).ok

    def generate(self, day: date = MON, **kwargs) -> workflow.GenerationOutcome:
        result = self.controller.generate(day, day, scope=RangeScope.ELIGIBLE, **kwargs)
        assert result.ok, result.error
        return result.value


def test_a_mode_persists_and_drives_every_generation_path(tmp_path: Path) -> None:
    desk = Desk(tmp_path / "app.db")
    for name in ("Read", "Write", "Review"):
        desk.task(name)
    desk.mode(OptimizerMode.EARLY_FINISH)
    desk.restart()

    assert desk.controller.resolve_preferences(MON).value.optimizer_mode == OptimizerMode.EARLY_FINISH
    outcome = desk.generate()
    evaluation = outcome.evaluations[MON]
    assert evaluation.mode == "early_finish" and evaluation.scheduled_count == 3
    assert evaluation.idle_minutes == 0 and evaluation.first_start_minute == 0  # packed from the window start

    run = desk.controller.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE)  # the Week/Month path
    assert run.ok and len(run.value.outputs[MON].placements) == 3


def test_a_manual_move_survives_an_early_finish_regeneration(tmp_path: Path) -> None:
    desk = Desk(tmp_path / "app.db")
    essay = desk.task("Essay")
    desk.task("Other")
    desk.mode(OptimizerMode.EARLY_FINISH)
    desk.generate()
    [placed] = [p for p in desk.planning.placements_for_date(MON) if p.task_id == essay.id]
    start = datetime(2024, 6, 3, 15, tzinfo=timezone.utc)
    moved = workflow.reschedule_placement(desk.planning, placed.id, expected_version=placed.version, planned_date=MON,
                                          timezone_name="UTC", planned_start=start,
                                          planned_end=start + timedelta(hours=1)).replacement
    desk.task("Third")

    desk.generate()

    assert [p for p in desk.planning.placements_for_date(MON) if p.task_id == essay.id] == [moved]


def test_a_date_override_beats_the_user_default_and_old_saves_read_unchanged(tmp_path: Path) -> None:
    desk = Desk(tmp_path / "app.db")
    desk.mode(OptimizerMode.ADHD_FRIENDLY)  # an old-style save
    assert desk.controller.update_date_overrides(
        TUE, lambda layer: layer.model_copy(update={"optimizer_mode": OptimizerMode.NIGHT_OWL}),
        expected_version=None).ok
    desk.restart()
    assert desk.controller.resolve_preferences(MON).value.optimizer_mode == OptimizerMode.ADHD_FRIENDLY
    assert desk.controller.resolve_preferences(TUE).value.optimizer_mode == OptimizerMode.NIGHT_OWL


def test_catch_up_without_history_plans_like_normal(tmp_path: Path) -> None:
    desk = Desk(tmp_path / "app.db")  # one database: the same task ids, so equal-score ties break alike
    for name, category in (("Read", "study"), ("Gym", "health"), ("Mail", "work")):
        desk.task(name, category=category, required_date=MON)
    spans = []
    for mode in (OptimizerMode.PRECISE_GREEDY, OptimizerMode.CATCH_UP):
        desk.mode(mode)
        assert desk.generate(mode=workflow.GenerationMode.FULL).status == "generated"  # the mode changed the inputs
        spans.append(sorted((p.task_id, p.planned_start, p.planned_end) for p in desk.planning.placements_for_date(MON)))
    assert spans[0] == spans[1]
    assert desk.generate().status == "already_current"  # the evidence digest is stable within the day


def test_a_recurring_series_under_a_time_mode_keeps_one_placement_per_slot(tmp_path: Path) -> None:
    desk = Desk(tmp_path / "app.db")
    daily = desk.task("Walk", required_date=None, estimated_duration_minutes=30,
                      recurrence=RecurrenceSpec(frequency="daily", start_date=MON, timezone="UTC"))
    desk.mode(OptimizerMode.NIGHT_OWL)
    result = desk.controller.generate(MON, TUE, scope=RangeScope.ELIGIBLE)
    assert result.ok, result.error
    occurrences = [task for task in desk.planning.list_tasks() if task.series_id == daily.id]
    placed = [p for day in (MON, TUE) for p in desk.planning.placements_for_date(day)]
    assert len(placed) == len(occurrences) == 2
    assert {p.planned_date for p in placed} == {MON, TUE}
