"""Restart safety of the planning controller's authoritative state (Milestone 3):
preference layers (including the engine mode) and schedule provenance live
in SQLite, survive close/reopen, keep YAML -> user -> date precedence with
absent/value/None semantics, and tell current from stale schedules after a
restart. Fixed-block categories survive too.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.planning.application import PlanningService, RangeScope
from app.planning.models import FixedBlock, LocalTimeWindow, Task
from app.planning.preferences import OptimizerMode, PreferenceOverrides
from app.planning.provenance import StaleReason
from app.planning.repository import PlanningRepository
from app.planning.service import DayResultStatus
from app.ui.planning_controller import PlanningController

MON, TUE, SUN = date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 9)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "config" / "task_preference.yaml").write_text(
        "category_weights:\n  study: 2.0\n  work: 3.0\n  chores: 4.0\n", encoding="utf-8"
    )
    return root


class Session:
    def __init__(self, db_path: Path, project_root: Path, tz: str = "UTC") -> None:
        self.connection = get_connection(db_path)
        self.service = PlanningService(PlanningRepository(self.connection))
        self.controller = PlanningController(service=self.service, timezone=tz, project_root=str(project_root))

    def close(self) -> None:
        self.connection.close()


def ok(result):
    assert result.ok, result.error
    return result.value


def task(name: str = "Study", **overrides) -> Task:
    return Task(**{"name": name, "category": "study", "estimated_duration_minutes": 60, "priority": 5, **overrides})


def week_schedule(session: Session) -> None:
    ok(session.controller.schedule_range(MON, SUN, scope=RangeScope.ELIGIBLE))


def statuses(session: Session) -> dict[date, DayResultStatus]:
    return {day: state.status for day, state in ok(session.controller.day_states([MON + timedelta(d) for d in range(7)])).items()}


# -----------------------------------------------------------------------------
# Preferences
# -----------------------------------------------------------------------------


def test_preferences_and_engine_mode_survive_reopen_with_layer_semantics(tmp_path, project_root) -> None:
    db_path = tmp_path / "app.db"
    first = Session(db_path, project_root)
    ok(first.controller.set_user_overrides(PreferenceOverrides(
        optimizer_mode=OptimizerMode.ADHD_FRIENDLY,
        category_multipliers={"study": 5.0, "work": None},  # value, and an explicit clear of YAML's 3.0
        category_preferred_windows={"study": LocalTimeWindow(start_minute=540, end_minute=720)},
    )))
    ok(first.controller.set_date_overrides(TUE, PreferenceOverrides(
        optimizer_mode=OptimizerMode.PRECISE_GREEDY,
        category_multipliers={"study": None, "chores": 0.0},  # clear the user's 5.0; a deliberate 0.0
    )))
    first.close()

    second = Session(db_path, project_root)
    try:
        monday = ok(second.controller.resolve_preferences(MON))
        tuesday = ok(second.controller.resolve_preferences(TUE))

        assert monday.optimizer_mode == OptimizerMode.ADHD_FRIENDLY  # user layer
        assert monday.category_multipliers == {"study": 5.0, "chores": 4.0}  # work cleared, chores from YAML
        assert monday.category_preferred_windows["study"].start_minute == 540

        assert tuesday.optimizer_mode == OptimizerMode.PRECISE_GREEDY  # date layer wins
        assert tuesday.effective_category_multiplier("study") == 1.0  # None: cleared to neutral, not back to YAML
        assert tuesday.category_multipliers["chores"] == 0.0  # an explicit 0.0 is kept, not treated as unset
        assert "study" in tuesday.category_preferred_windows  # absent in the date layer: inherited unchanged

        stored = ok(second.controller.date_preferences(TUE))
        assert stored.overrides.category_multipliers == {"study": None, "chores": 0.0}  # None survives storage
        assert stored.overrides.reward.weight_importance is None  # absent stays absent
    finally:
        second.close()


def test_controllers_always_read_the_authoritative_stored_layers(tmp_path, project_root) -> None:
    db_path = tmp_path / "app.db"
    a, b = Session(db_path, project_root), Session(db_path, project_root)
    try:
        ok(a.controller.set_engine_mode(OptimizerMode.ADHD_FRIENDLY))
        assert ok(b.controller.resolve_preferences(MON)).optimizer_mode == OptimizerMode.ADHD_FRIENDLY

        layer = ok(b.controller.user_preferences())
        ok(b.controller.set_user_overrides(PreferenceOverrides(), expected_version=layer.version))
        assert ok(a.controller.resolve_preferences(MON)).optimizer_mode == OptimizerMode.PRECISE_GREEDY

        stale = a.controller.set_user_overrides(PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY),
                                                expected_version=layer.version)
        assert not stale.ok and "changed by someone else" in stale.error
        # Deleting needs the current version too.
        assert not a.controller.set_user_overrides(None).ok
        assert ok(a.controller.set_user_overrides(None, expected_version=layer.version + 1)) is None
        assert ok(b.controller.user_preferences()) is None
    finally:
        a.close()
        b.close()


# -----------------------------------------------------------------------------
# Current vs. stale after restart
# -----------------------------------------------------------------------------


def test_restored_unchanged_schedule_is_current_including_empty_days(tmp_path, project_root) -> None:
    db_path = tmp_path / "app.db"
    first = Session(db_path, project_root)
    ok(first.controller.add_or_update_task(task(required_date=MON)))
    week_schedule(first)
    first.close()

    second = Session(db_path, project_root)
    try:
        assert set(statuses(second).values()) == {DayResultStatus.GENERATED}  # six of the days placed nothing
        assert ok(second.controller.day_state(TUE)).result.placements == []
        record = second.service.generation_record(TUE)
        assert (record.placement_count, record.range_start, record.range_end) == (0, MON, SUN)
    finally:
        second.close()


@pytest.mark.parametrize("edit", ["task", "fixed_block", "engine_mode", "date_preference", "timezone", "placement"])
def test_relevant_edits_make_a_restored_schedule_stale(tmp_path, project_root, edit) -> None:
    db_path = tmp_path / "app.db"
    first = Session(db_path, project_root)
    study = ok(first.controller.add_or_update_task(task(required_date=MON)))
    week_schedule(first)
    first.close()

    second = Session(db_path, project_root, tz="America/New_York" if edit == "timezone" else "UTC")
    try:
        if edit == "task":
            ok(second.controller.add_or_update_task(study.model_copy(update={"priority": 9}),
                                                    expected_version=study.version))
        elif edit == "fixed_block":
            start = datetime(2024, 6, 5, 12, tzinfo=timezone.utc)
            ok(second.controller.save_fixed_block(FixedBlock(
                label="Lunch", planned_date=date(2024, 6, 5), timezone="UTC", planned_start=start,
                planned_end=start + timedelta(hours=1),
            )))
        elif edit == "engine_mode":
            ok(second.controller.set_engine_mode(OptimizerMode.ADHD_FRIENDLY))
        elif edit == "date_preference":
            ok(second.controller.set_date_overrides(SUN, PreferenceOverrides(category_multipliers={"study": 2.5})))
        elif edit == "placement":
            [placement] = second.service.placements_for_date(MON)
            second.service.replace_placements(MON, MON, [placement.model_copy(update={"score": 1.0})],
                                              expected_versions={placement.id: placement.version})

        states = ok(second.controller.day_states([MON, TUE]))
        if edit == "placement":
            # Only the date whose saved placements changed; its inputs did not.
            assert states[MON].stale_reason == StaleReason.PLACEMENTS_CHANGED
            assert states[TUE].status == DayResultStatus.GENERATED
        else:
            # An input of the range changed: every date generated from it is stale.
            assert {state.status for state in states.values()} == {DayResultStatus.STALE}
            assert states[MON].stale_reason == StaleReason.INPUTS_CHANGED
    finally:
        second.close()


def test_irrelevant_edits_and_version_only_saves_keep_the_schedule_current(tmp_path, project_root) -> None:
    db_path = tmp_path / "app.db"
    session = Session(db_path, project_root)
    try:
        study = ok(session.controller.add_or_update_task(task(required_date=MON)))
        week_schedule(session)

        # A task planned for another week is not part of this week's inputs.
        ok(session.controller.add_or_update_task(task("Next week", required_date=date(2024, 6, 12))))
        # Saving unchanged content is a no-op (no version bump, nothing to invalidate).
        ok(session.controller.add_or_update_task(study, expected_version=study.version))

        assert set(statuses(session).values()) == {DayResultStatus.GENERATED}
    finally:
        session.close()


def test_regenerating_makes_it_current_again(tmp_path, project_root) -> None:
    session = Session(tmp_path / "app.db", project_root)
    try:
        ok(session.controller.add_or_update_task(task(required_date=MON)))
        week_schedule(session)
        ok(session.controller.set_engine_mode(OptimizerMode.ADHD_FRIENDLY))
        assert set(statuses(session).values()) == {DayResultStatus.STALE}

        week_schedule(session)
        assert set(statuses(session).values()) == {DayResultStatus.GENERATED}
        assert session.service.generation_record(MON).engine_mode == OptimizerMode.ADHD_FRIENDLY
        assert session.service.generation_record(MON).version == 2  # the date's record was updated, not duplicated
    finally:
        session.close()


def test_fixed_block_category_survives_reopen(tmp_path, project_root) -> None:
    db_path = tmp_path / "app.db"
    first = Session(db_path, project_root)
    start = datetime(2024, 6, 3, 0, tzinfo=timezone.utc)
    block = ok(first.controller.save_fixed_block(FixedBlock(
        label="Sleep", category="sleep", planned_date=MON, timezone="UTC", planned_start=start,
        planned_end=start + timedelta(hours=8),
    )))
    first.close()

    second = Session(db_path, project_root)
    try:
        assert ok(second.controller.get_fixed_blocks(MON)) == [block]
        assert block.category == "sleep"
    finally:
        second.close()
