"""Tests for app/optimizer.py: the greedy scheduling orchestration.

Every test runs with cwd redirected to an empty tmp_path so
load_reward_settings()'s implicit filesystem search can't pick up a real
YAML file, keeping RewardSettings() defaults deterministic regardless of
what config files exist on the machine running the tests.
"""

from __future__ import annotations

import pytest

from app.optimizer import (
    combine_fixed_and_optimized_scheduled_tasks,
    optimize_day_schedule,
)


@pytest.fixture(autouse=True)
def isolate_reward_config_search(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


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
