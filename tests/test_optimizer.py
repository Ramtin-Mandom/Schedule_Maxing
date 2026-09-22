"""Tests for app/optimizer.py: the greedy scheduling orchestration.

Every test forces load_reward_settings(config_path=None) to resolve to "no
config file found" so RewardSettings() defaults are deterministic regardless
of what config files exist on the machine running the tests.

Note: reward._resolve_config_path(None) doesn't just check the current
directory -- it also walks every ancestor directory (and each ancestor's own
config/ subdirectory) looking for task_prefrence.yaml/task_preference.yaml
(see test_reward.py's test_default_discovery_walks_ancestor_directories).
Merely chdir-ing to a tmp_path does not stop that walk from reaching real
directories above tmp_path (e.g. a developer's home directory), so isolation
here is done by monkeypatching the resolver's "no explicit path" branch
directly rather than relying on cwd.
"""

from __future__ import annotations

import pytest

import app.reward as reward_module
from app.optimizer import (
    combine_fixed_and_optimized_scheduled_tasks,
    optimize_day_schedule,
)


@pytest.fixture(autouse=True)
def isolate_reward_config_search(monkeypatch):
    original_resolve = reward_module._resolve_config_path

    def _fake_resolve(config_path=None):
        if config_path is None:
            return None
        return original_resolve(config_path)

    monkeypatch.setattr(reward_module, "_resolve_config_path", _fake_resolve)


# -----------------------------
# Fixed blocks
# -----------------------------


def test_fixed_blocks_are_preserved_unchanged(make_day_schedule, make_fixed_block) -> None:
    fixed = make_fixed_block("Sleep", start=0, end=480)
    schedule = make_day_schedule(day_start=0, day_end=1440, fixed_blocks=[fixed])

    output = optimize_day_schedule(schedule, date=1)

    assert len(output.scheduled_tasks) == 1
    scheduled = output.scheduled_tasks[0]
    assert scheduled.name == "Sleep"
    assert scheduled.time_window.start_time == 0
    assert scheduled.time_window.end_time == 480
    assert scheduled.score == 0.0


def test_fixed_blocks_never_move_even_with_competing_flexible_tasks(
    make_day_schedule, make_fixed_block, make_task
) -> None:
    fixed = make_fixed_block("Lunch", start=720, end=780)
    flexible = make_task("Study", duration=60, preference_start=700, preference_end=800)
    schedule = make_day_schedule(
        day_start=600, day_end=900, fixed_blocks=[fixed], tasks=[flexible]
    )

    output = optimize_day_schedule(schedule, date=1)

    lunch = next(t for t in output.scheduled_tasks if t.name == "Lunch")
    assert lunch.time_window.start_time == 720
    assert lunch.time_window.end_time == 780


# -----------------------------
# Fixed-block validation (regression: app/constraints.py's validate_fixed_blocks
# exists and is unit-tested in isolation, but was never wired into the shared
# optimizer, so invalid fixed blocks from a raw CSV/API caller could silently
# survive into the output).
# -----------------------------


def test_overlapping_fixed_blocks_are_rejected(make_day_schedule, make_fixed_block) -> None:
    first = make_fixed_block("Meeting", start=480, end=600)
    second = make_fixed_block("Breakfast", start=540, end=600)
    schedule = make_day_schedule(day_start=0, day_end=1440, fixed_blocks=[first, second])

    with pytest.raises(ValueError):
        optimize_day_schedule(schedule, date=1)


def test_fixed_block_outside_day_window_is_rejected(make_day_schedule, make_fixed_block) -> None:
    block = make_fixed_block("TooEarly", start=0, end=100)
    schedule = make_day_schedule(day_start=480, day_end=1320, fixed_blocks=[block])

    with pytest.raises(ValueError):
        optimize_day_schedule(schedule, date=1)


def test_fixed_block_with_nonpositive_window_is_rejected(make_day_schedule, make_fixed_block) -> None:
    block = make_fixed_block("Bad", start=480, end=480)
    schedule = make_day_schedule(day_start=0, day_end=1440, fixed_blocks=[block])

    with pytest.raises(ValueError):
        optimize_day_schedule(schedule, date=1)


def test_valid_touching_fixed_blocks_are_accepted(make_day_schedule, make_fixed_block) -> None:
    first = make_fixed_block("Sleep", start=0, end=480)
    second = make_fixed_block("Breakfast", start=480, end=540)
    schedule = make_day_schedule(day_start=0, day_end=1440, fixed_blocks=[first, second])

    output = optimize_day_schedule(schedule, date=1)

    assert len(output.scheduled_tasks) == 2
    by_name = {t.name: t for t in output.scheduled_tasks}
    assert by_name["Sleep"].time_window == make_fixed_block("Sleep", start=0, end=480).time_window
    assert by_name["Breakfast"].time_window == make_fixed_block("Breakfast", start=480, end=540).time_window


# -----------------------------
# No overlap, exact duration, day-window boundaries
# -----------------------------


def test_flexible_tasks_do_not_overlap_fixed_or_each_other(
    make_day_schedule, make_fixed_block, make_task
) -> None:
    fixed = make_fixed_block("Lunch", start=720, end=780)
    task_a = make_task("Task A", duration=60, preference_start=0, preference_end=1440)
    task_b = make_task("Task B", duration=60, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(
        day_start=600, day_end=900, fixed_blocks=[fixed], tasks=[task_a, task_b]
    )

    output = optimize_day_schedule(schedule, date=1)

    assert output.unscheduled_tasks == []
    scheduled = sorted(output.scheduled_tasks, key=lambda s: s.time_window.start_time)

    for earlier, later in zip(scheduled, scheduled[1:]):
        assert earlier.time_window.end_time <= later.time_window.start_time

    for task in scheduled:
        if task.name == "Lunch":
            continue
        assert not (task.time_window.start_time < 780 and 720 < task.time_window.end_time)


def test_scheduled_tasks_get_their_exact_duration(
    make_day_schedule, make_task
) -> None:
    task = make_task("Study", duration=90, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=0, day_end=480, tasks=[task])

    output = optimize_day_schedule(schedule, date=1)

    assert len(output.scheduled_tasks) == 1
    scheduled = output.scheduled_tasks[0]
    assert scheduled.time_window.end_time - scheduled.time_window.start_time == 90


def test_scheduled_tasks_stay_inside_day_window(
    make_day_schedule, make_task
) -> None:
    task = make_task("Study", duration=60, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=480, day_end=600, tasks=[task])

    output = optimize_day_schedule(schedule, date=1)

    assert len(output.scheduled_tasks) == 1
    scheduled = output.scheduled_tasks[0]
    assert scheduled.time_window.start_time >= 480
    assert scheduled.time_window.end_time <= 600


def test_task_too_long_for_day_window_is_unscheduled_with_reason(
    make_day_schedule, make_task
) -> None:
    task = make_task("Too Long", duration=120, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=0, day_end=90, tasks=[task])

    output = optimize_day_schedule(schedule, date=1)

    assert output.scheduled_tasks == []
    assert len(output.unscheduled_tasks) == 1
    assert output.unscheduled_tasks[0].name == "Too Long"
    assert output.unscheduled_tasks[0].reason


def test_task_unscheduled_when_fixed_blocks_fill_entire_day(
    make_day_schedule, make_fixed_block, make_task
) -> None:
    fixed = make_fixed_block("Busy", start=0, end=1440)
    task = make_task("Impossible", duration=30, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=0, day_end=1440, fixed_blocks=[fixed], tasks=[task])

    output = optimize_day_schedule(schedule, date=1)

    assert len(output.unscheduled_tasks) == 1
    assert output.unscheduled_tasks[0].name == "Impossible"
    assert output.unscheduled_tasks[0].reason


# -----------------------------
# Dependency ordering
# -----------------------------


def test_dependent_task_is_scheduled_after_its_prerequisite(
    make_day_schedule, make_task
) -> None:
    prerequisite = make_task(
        "Study Math", duration=60, priority=8, preference_start=0, preference_end=1440
    )
    dependent = make_task(
        "Math Review",
        duration=60,
        priority=5,
        preference_start=0,
        preference_end=1440,
        dependencies=["Study Math"],
    )
    schedule = make_day_schedule(day_start=0, day_end=480, tasks=[prerequisite, dependent])

    output = optimize_day_schedule(schedule, date=1)

    by_name = {task.name: task for task in output.scheduled_tasks}
    assert output.unscheduled_tasks == []
    assert by_name["Study Math"].time_window.end_time <= by_name["Math Review"].time_window.start_time


def test_dependency_cycle_raises_value_error(make_day_schedule, make_task) -> None:
    task_a = make_task("A", duration=30, dependencies=["B"])
    task_b = make_task("B", duration=30, dependencies=["A"])
    schedule = make_day_schedule(day_start=0, day_end=480, tasks=[task_a, task_b])

    with pytest.raises(ValueError):
        optimize_day_schedule(schedule, date=1)


def test_missing_dependency_name_is_ignored(make_day_schedule, make_task) -> None:
    task = make_task("Solo Task", duration=30, dependencies=["Nonexistent Task"])
    schedule = make_day_schedule(day_start=0, day_end=480, tasks=[task])

    output = optimize_day_schedule(schedule, date=1)

    assert output.unscheduled_tasks == []
    assert len(output.scheduled_tasks) == 1
    assert output.scheduled_tasks[0].name == "Solo Task"


def test_high_priority_dependent_waits_for_all_prerequisites_reversed_input_order(
    make_day_schedule, make_task
) -> None:
    """The dependent is listed first (reversed order) and outscores both of its
    prerequisites on priority alone, so a naive "highest score wins" pass
    without dependency gating would try to schedule it immediately. It must
    instead wait until both real prerequisites have been scheduled.
    """
    dependent = make_task(
        "Final Review",
        duration=30,
        priority=10,
        preference_start=0,
        preference_end=1440,
        dependencies=["Prep A", "Prep B"],
    )
    prep_a = make_task("Prep A", duration=60, priority=2, preference_start=0, preference_end=1440)
    prep_b = make_task("Prep B", duration=60, priority=2, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=0, day_end=300, tasks=[dependent, prep_a, prep_b])

    output = optimize_day_schedule(schedule, date=1)

    assert output.unscheduled_tasks == []
    by_name = {t.name: t for t in output.scheduled_tasks}
    prereq_end = max(by_name["Prep A"].time_window.end_time, by_name["Prep B"].time_window.end_time)
    assert by_name["Final Review"].time_window.start_time >= prereq_end


def test_impossible_prerequisite_leaves_dependent_unscheduled_but_independent_task_progresses(
    make_day_schedule, make_task
) -> None:
    """An unschedulable prerequisite (too long for the day) plus its dependent,
    alongside an unrelated independent task. The independent task must still
    be scheduled, the prerequisite and its dependent must both end up
    unscheduled, and the optimizer's made_progress/break logic must terminate
    rather than loop forever.
    """
    impossible_prereq = make_task(
        "Impossible Prep", duration=10_000, preference_start=0, preference_end=1440
    )
    blocked_dependent = make_task(
        "Blocked Followup", duration=30, dependencies=["Impossible Prep"],
        preference_start=0, preference_end=1440,
    )
    independent = make_task("Independent", duration=30, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(
        day_start=0, day_end=200, tasks=[impossible_prereq, blocked_dependent, independent]
    )

    output = optimize_day_schedule(schedule, date=1)

    scheduled_names = {t.name for t in output.scheduled_tasks}
    unscheduled_names = {t.name for t in output.unscheduled_tasks}

    assert scheduled_names == {"Independent"}
    assert unscheduled_names == {"Impossible Prep", "Blocked Followup"}


def test_mixed_real_and_missing_dependencies_ignores_missing_and_enforces_real(
    make_day_schedule, make_task
) -> None:
    prerequisite = make_task("Real Prep", duration=60, preference_start=0, preference_end=1440)
    task = make_task(
        "Mixed Deps Task",
        duration=30,
        dependencies=["Real Prep", "Ghost Task"],
        preference_start=0,
        preference_end=1440,
    )
    schedule = make_day_schedule(day_start=0, day_end=300, tasks=[prerequisite, task])

    output = optimize_day_schedule(schedule, date=1)

    assert output.unscheduled_tasks == []
    by_name = {t.name: t for t in output.scheduled_tasks}
    assert by_name["Real Prep"].time_window.end_time <= by_name["Mixed Deps Task"].time_window.start_time


# -----------------------------
# 30-minute grid snapping
# -----------------------------


def test_start_rounds_up_to_next_30_minute_boundary_from_nongrid_day_start(
    make_day_schedule, make_task
) -> None:
    """day_start=17 is not on the 30-minute grid; the task's actual start must
    snap up to 30, while its exact (non-30-multiple) 45-minute duration is
    still preserved fully.
    """
    task = make_task("Odd Duration", duration=45, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=17, day_end=200, tasks=[task])

    output = optimize_day_schedule(schedule, date=1)

    scheduled = output.scheduled_tasks[0]
    assert scheduled.time_window.start_time == 30
    assert scheduled.time_window.end_time == 75


def test_start_rounds_up_to_next_30_minute_boundary_after_nongrid_prerequisite_end(
    tmp_path, make_day_schedule, make_task
) -> None:
    """The prerequisite's 25-minute duration ends at a non-grid time (25); the
    dependent task's earliest possible start must snap up to the next
    30-minute boundary (30), while its own exact (non-30-multiple) 20-minute
    duration is preserved.

    Relation/fragmentation scoring are zeroed out via an explicit config so
    that every grid slot ties on score and the earliest one wins -- otherwise
    the fragmentation penalty legitimately pushes the greedy pick to a later
    slot that keeps a larger gap from its neighbor (see
    test_higher_scoring_task_wins_contested_preferred_slot and
    test_fragmentation_penalty_for_small_gap in test_reward.py for that
    behavior in isolation).
    """
    config = tmp_path / "no_neighbor_effects.yaml"
    config.write_text(
        "weights:\n  tag_relation: 0\n  fragmentation_penalty: 0\n", encoding="utf-8"
    )

    prerequisite = make_task("Prep", duration=25, preference_start=0, preference_end=1440)
    dependent = make_task(
        "Followup", duration=20, dependencies=["Prep"], preference_start=0, preference_end=1440
    )
    schedule = make_day_schedule(day_start=0, day_end=200, tasks=[prerequisite, dependent])

    output = optimize_day_schedule(schedule, date=1, config_path=config)

    by_name = {t.name: t for t in output.scheduled_tasks}
    assert by_name["Prep"].time_window.end_time == 25
    assert by_name["Followup"].time_window.start_time == 30
    assert by_name["Followup"].time_window.end_time == 50


# -----------------------------
# 1440-minute (end of day) boundary
# -----------------------------


def test_task_can_be_scheduled_to_end_exactly_at_1440(make_day_schedule, make_task) -> None:
    task = make_task("Late Task", duration=60, preference_start=1380, preference_end=1440)
    schedule = make_day_schedule(day_start=0, day_end=1440, tasks=[task])

    output = optimize_day_schedule(schedule, date=1)

    assert output.unscheduled_tasks == []
    scheduled = output.scheduled_tasks[0]
    assert scheduled.time_window.end_time == 1440


def test_task_that_cannot_fit_before_day_end_is_unscheduled(make_day_schedule, make_task) -> None:
    task = make_task("Overflow", duration=90, preference_start=1380, preference_end=1440)
    schedule = make_day_schedule(day_start=1380, day_end=1440, tasks=[task])

    output = optimize_day_schedule(schedule, date=1)

    assert output.scheduled_tasks == []
    assert len(output.unscheduled_tasks) == 1
    assert output.unscheduled_tasks[0].name == "Overflow"


# -----------------------------
# Deterministic greedy selection
# -----------------------------


def test_higher_scoring_task_wins_contested_preferred_slot(make_day_schedule, make_task) -> None:
    high_priority = make_task("Important", duration=60, priority=10, preference_start=480, preference_end=540)
    low_priority = make_task("Trivial", duration=60, priority=1, preference_start=480, preference_end=540)
    schedule = make_day_schedule(day_start=480, day_end=600, tasks=[low_priority, high_priority])

    output = optimize_day_schedule(schedule, date=1)

    by_name = {t.name: t for t in output.scheduled_tasks}
    assert by_name["Important"].time_window.start_time == 480
    assert by_name["Trivial"].time_window.start_time == 540


def test_equal_scoring_tasks_break_ties_by_input_order_and_earliest_start(
    make_day_schedule, make_task
) -> None:
    first_task = make_task("First", duration=60, priority=5, preference_start=0, preference_end=1440)
    second_task = make_task("Second", duration=60, priority=5, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=0, day_end=120, tasks=[first_task, second_task])

    output = optimize_day_schedule(schedule, date=1)

    by_name = {t.name: t for t in output.scheduled_tasks}
    assert by_name["First"].time_window.start_time == 0
    assert by_name["Second"].time_window.start_time == 60


# -----------------------------
# Explicit config_path affects scoring/selection
# -----------------------------


def test_explicit_config_path_changes_scoring_and_selection(
    tmp_path, make_day_schedule, make_task
) -> None:
    config = tmp_path / "custom_rewards.yaml"
    config.write_text(
        "weights:\n"
        "  importance: 1\n"
        "  time_bonus: 0\n"
        "  tag_relation: 0\n"
        "  fragmentation_penalty: 0\n"
        "category_weights:\n"
        "  entertainment: 100.0\n",
        encoding="utf-8",
    )

    study_task = make_task(
        "Study", category="study", priority=5, duration=60, preference_start=0, preference_end=1440
    )
    fun_task = make_task(
        "Fun", category="entertainment", priority=5, duration=60, preference_start=0, preference_end=1440
    )
    schedule = make_day_schedule(day_start=0, day_end=120, tasks=[study_task, fun_task])

    default_output = optimize_day_schedule(schedule, date=1)
    boosted_output = optimize_day_schedule(schedule, date=1, config_path=config)

    default_first = min(default_output.scheduled_tasks, key=lambda t: t.time_window.start_time)
    boosted_first = min(boosted_output.scheduled_tasks, key=lambda t: t.time_window.start_time)

    assert default_first.name == "Study"  # deterministic input-order tie-break under equal defaults
    assert boosted_first.name == "Fun"  # explicit config's category_weights bias now decides


# -----------------------------
# Complete output accounting
# -----------------------------


def test_complete_output_accounting_for_mixed_fixed_and_flexible_schedule(
    make_day_schedule, make_fixed_block, make_task
) -> None:
    sleep = make_fixed_block("Sleep", start=0, end=480)
    lunch = make_fixed_block("Lunch", start=720, end=780)

    tasks = [
        make_task("Study", duration=90, priority=8, preference_start=480, preference_end=720),
        make_task("Gym", duration=60, priority=6, preference_start=780, preference_end=900),
        make_task("Read", duration=45, priority=3, preference_start=900, preference_end=1000),
        make_task("Overflow", duration=1000, priority=9, preference_start=0, preference_end=1440),
    ]
    schedule = make_day_schedule(day_start=0, day_end=1000, fixed_blocks=[sleep, lunch], tasks=tasks)

    output = optimize_day_schedule(schedule, date=1)

    flexible_names = {task.name for task in tasks}
    scheduled_flexible_names = {t.name for t in output.scheduled_tasks if t.name not in {"Sleep", "Lunch"}}
    unscheduled_names = {t.name for t in output.unscheduled_tasks}

    # Each uniquely named flexible input is scheduled or unscheduled exactly once.
    assert scheduled_flexible_names | unscheduled_names == flexible_names
    assert scheduled_flexible_names & unscheduled_names == set()
    assert "Overflow" in unscheduled_names

    # Fixed blocks survive unchanged.
    by_name = {t.name: t for t in output.scheduled_tasks}
    assert by_name["Sleep"].time_window.start_time == 0
    assert by_name["Sleep"].time_window.end_time == 480
    assert by_name["Lunch"].time_window.start_time == 720
    assert by_name["Lunch"].time_window.end_time == 780

    # No overlaps, all inside the day window, exact durations for flexible tasks.
    ordered = sorted(output.scheduled_tasks, key=lambda t: t.time_window.start_time)
    for earlier, later in zip(ordered, ordered[1:]):
        assert earlier.time_window.end_time <= later.time_window.start_time
    for scheduled_task in ordered:
        assert scheduled_task.time_window.start_time >= 0
        assert scheduled_task.time_window.end_time <= 1000

    durations_by_name = {task.name: task.duration for task in tasks}
    for scheduled_task in ordered:
        if scheduled_task.name in durations_by_name:
            span = scheduled_task.time_window.end_time - scheduled_task.time_window.start_time
            assert span == durations_by_name[scheduled_task.name]

    assert output.total_score == round(sum(t.score for t in output.scheduled_tasks), 2)


# -----------------------------
# Wrapper/direct-entry-point equality
# -----------------------------


def test_wrapper_output_equals_direct_optimize_day_schedule_output(
    tmp_path, make_day_schedule, make_fixed_block, make_task
) -> None:
    config = tmp_path / "rewards.yaml"
    config.write_text("weights:\n  importance: 7\n", encoding="utf-8")

    fixed = make_fixed_block("Sleep", start=0, end=480)
    task = make_task("Study", duration=60, priority=6, preference_start=480, preference_end=600)
    schedule = make_day_schedule(day_start=0, day_end=600, fixed_blocks=[fixed], tasks=[task])

    direct = optimize_day_schedule(schedule, date=5, config_path=config)
    wrapped = combine_fixed_and_optimized_scheduled_tasks(date=5, day_schedule=schedule, config_path=config)

    assert wrapped == direct


# -----------------------------
# Score / output consistency
# -----------------------------


def test_total_score_matches_sum_of_scheduled_task_scores(
    make_day_schedule, make_task
) -> None:
    task_a = make_task("A", duration=60, priority=5, preference_start=0, preference_end=1440)
    task_b = make_task("B", duration=60, priority=8, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=0, day_end=480, tasks=[task_a, task_b])

    output = optimize_day_schedule(schedule, date=1)

    assert output.total_score == round(sum(t.score for t in output.scheduled_tasks), 2)


def test_output_date_defaults_to_provided_date(make_day_schedule) -> None:
    schedule = make_day_schedule(day_start=0, day_end=480)
    output = optimize_day_schedule(schedule, date=7)
    assert output.date == 7


def test_combine_fixed_and_optimized_scheduled_tasks_matches_optimize_day_schedule(
    make_day_schedule, make_task
) -> None:
    task = make_task("Study", duration=60, preference_start=0, preference_end=1440)
    schedule = make_day_schedule(day_start=0, day_end=480, tasks=[task])

    output = combine_fixed_and_optimized_scheduled_tasks(
        date=3, day_schedule=schedule
    )

    assert output.date == 3
    assert len(output.scheduled_tasks) == 1
