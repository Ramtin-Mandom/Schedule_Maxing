"""Tests for app/constraints.py: the hard-constraint and free-slot helpers."""

from __future__ import annotations

from app.constraints import (
    can_task_fit_in_slot,
    does_overlap,
    get_available_time_slots,
    is_inside_day,
    is_valid_task_placement,
    respects_duration,
    respects_fixed_blocks,
    respects_other_tasks,
    validate_fixed_blocks,
)

# -----------------------------
# does_overlap
# -----------------------------


def test_does_overlap_true_for_overlapping_intervals() -> None:
    assert does_overlap(0, 60, 30, 90) is True


def test_does_overlap_false_for_disjoint_intervals() -> None:
    assert does_overlap(0, 60, 120, 180) is False


def test_does_overlap_false_when_intervals_only_touch() -> None:
    # end1 == start2: back-to-back, not an overlap.
    assert does_overlap(0, 60, 60, 120) is False


# -----------------------------
# is_inside_day
# -----------------------------


def test_is_inside_day_true_within_bounds(make_day_schedule) -> None:
    schedule = make_day_schedule(day_start=480, day_end=1320)
    assert is_inside_day(540, 600, schedule) is True


def test_is_inside_day_true_at_exact_boundaries(make_day_schedule) -> None:
    schedule = make_day_schedule(day_start=480, day_end=1320)
    assert is_inside_day(480, 1320, schedule) is True


def test_is_inside_day_false_when_starting_before_window(make_day_schedule) -> None:
    schedule = make_day_schedule(day_start=480, day_end=1320)
    assert is_inside_day(479, 600, schedule) is False


def test_is_inside_day_false_when_ending_after_window(make_day_schedule) -> None:
    schedule = make_day_schedule(day_start=480, day_end=1320)
    assert is_inside_day(1300, 1321, schedule) is False


# -----------------------------
# respects_duration
# -----------------------------


def test_respects_duration_true_for_exact_match(make_task) -> None:
    task = make_task(duration=60)
    assert respects_duration(task, 100, 160) is True


def test_respects_duration_false_for_shorter_interval(make_task) -> None:
    task = make_task(duration=60)
    assert respects_duration(task, 100, 150) is False


def test_respects_duration_false_for_longer_interval(make_task) -> None:
    task = make_task(duration=60)
    assert respects_duration(task, 100, 200) is False


# -----------------------------
# respects_fixed_blocks / respects_other_tasks
# -----------------------------


def test_respects_fixed_blocks_true_when_no_overlap(make_fixed_block) -> None:
    blocks = [make_fixed_block("Sleep", start=0, end=480)]
    assert respects_fixed_blocks(500, 560, blocks) is True


def test_respects_fixed_blocks_false_when_overlapping(make_fixed_block) -> None:
    blocks = [make_fixed_block("Sleep", start=0, end=480)]
    assert respects_fixed_blocks(400, 500, blocks) is False


def test_respects_other_tasks_true_when_no_overlap(make_scheduled_task) -> None:
    scheduled = [make_scheduled_task("A", start=0, end=60)]
    assert respects_other_tasks(60, 120, scheduled) is True


def test_respects_other_tasks_false_when_overlapping(make_scheduled_task) -> None:
    scheduled = [make_scheduled_task("A", start=0, end=60)]
    assert respects_other_tasks(30, 90, scheduled) is False


# -----------------------------
# is_valid_task_placement
# -----------------------------


def test_is_valid_task_placement_true_for_fully_valid_slot(
    make_task, make_fixed_block, make_day_schedule, make_scheduled_task
) -> None:
    task = make_task(duration=60)
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[make_fixed_block("Sleep", start=0, end=480)],
    )
    scheduled = [make_scheduled_task("Other", start=600, end=660)]

    assert is_valid_task_placement(task, 500, 560, schedule, scheduled) is True


def test_is_valid_task_placement_false_outside_day_window(
    make_task, make_day_schedule
) -> None:
    task = make_task(duration=60)
    schedule = make_day_schedule(day_start=480, day_end=1320)

    assert is_valid_task_placement(task, 0, 60, schedule) is False


def test_is_valid_task_placement_false_wrong_duration(
    make_task, make_day_schedule
) -> None:
    task = make_task(duration=60)
    schedule = make_day_schedule(day_start=0, day_end=1440)

    assert is_valid_task_placement(task, 500, 530, schedule) is False


def test_is_valid_task_placement_false_overlapping_fixed_block(
    make_task, make_fixed_block, make_day_schedule
) -> None:
    task = make_task(duration=60)
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[make_fixed_block("Sleep", start=0, end=480)],
    )

    assert is_valid_task_placement(task, 450, 510, schedule) is False


def test_is_valid_task_placement_false_overlapping_scheduled_task(
    make_task, make_day_schedule, make_scheduled_task
) -> None:
    task = make_task(duration=60)
    schedule = make_day_schedule(day_start=0, day_end=1440)
    scheduled = [make_scheduled_task("Other", start=500, end=560)]

    assert is_valid_task_placement(task, 530, 590, schedule, scheduled) is False


def test_is_valid_task_placement_defaults_scheduled_tasks_to_empty(
    make_task, make_day_schedule
) -> None:
    task = make_task(duration=60)
    schedule = make_day_schedule(day_start=0, day_end=1440)

    assert is_valid_task_placement(task, 500, 560, schedule) is True


# -----------------------------
# validate_fixed_blocks
# -----------------------------


def test_validate_fixed_blocks_true_for_valid_blocks(
    make_day_schedule, make_fixed_block
) -> None:
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[
            make_fixed_block("Sleep", start=0, end=480),
            make_fixed_block("Lunch", start=720, end=780),
        ],
    )
    assert validate_fixed_blocks(schedule) is True


def test_validate_fixed_blocks_false_when_end_before_start(
    make_day_schedule, make_fixed_block
) -> None:
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[make_fixed_block("Bad", start=480, end=480)],
    )
    assert validate_fixed_blocks(schedule) is False


def test_validate_fixed_blocks_false_when_outside_day_window(
    make_day_schedule, make_fixed_block
) -> None:
    schedule = make_day_schedule(
        day_start=480,
        day_end=1320,
        fixed_blocks=[make_fixed_block("TooEarly", start=0, end=100)],
    )
    assert validate_fixed_blocks(schedule) is False


def test_validate_fixed_blocks_false_when_blocks_overlap(
    make_day_schedule, make_fixed_block
) -> None:
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[
            make_fixed_block("A", start=0, end=100),
            make_fixed_block("B", start=50, end=150),
        ],
    )
    assert validate_fixed_blocks(schedule) is False


# -----------------------------
# get_available_time_slots
# -----------------------------


def test_get_available_time_slots_no_fixed_blocks(make_day_schedule) -> None:
    schedule = make_day_schedule(day_start=0, day_end=1440, fixed_blocks=[])
    slots = get_available_time_slots(schedule)

    assert len(slots) == 1
    assert slots[0].start_time == 0
    assert slots[0].end_time == 1440


def test_get_available_time_slots_with_one_block_in_the_middle(
    make_day_schedule, make_fixed_block
) -> None:
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[make_fixed_block("Lunch", start=720, end=780)],
    )
    slots = get_available_time_slots(schedule)

    assert [(s.start_time, s.end_time) for s in slots] == [(0, 720), (780, 1440)]


def test_get_available_time_slots_block_touching_day_start(
    make_day_schedule, make_fixed_block
) -> None:
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[make_fixed_block("Sleep", start=0, end=480)],
    )
    slots = get_available_time_slots(schedule)

    assert [(s.start_time, s.end_time) for s in slots] == [(480, 1440)]


def test_get_available_time_slots_block_covering_whole_day(
    make_day_schedule, make_fixed_block
) -> None:
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[make_fixed_block("Busy", start=0, end=1440)],
    )
    slots = get_available_time_slots(schedule)

    assert slots == []


def test_get_available_time_slots_multiple_blocks_unordered_input(
    make_day_schedule, make_fixed_block
) -> None:
    schedule = make_day_schedule(
        day_start=0,
        day_end=1440,
        fixed_blocks=[
            make_fixed_block("Dinner", start=1080, end=1140),
            make_fixed_block("Sleep", start=0, end=480),
            make_fixed_block("Lunch", start=720, end=780),
        ],
    )
    slots = get_available_time_slots(schedule)

    assert [(s.start_time, s.end_time) for s in slots] == [
        (480, 720),
        (780, 1080),
        (1140, 1440),
    ]


# -----------------------------
# can_task_fit_in_slot
# -----------------------------


def test_can_task_fit_in_slot_true_when_exact_fit(make_task, make_time_window) -> None:
    task = make_task(duration=60)
    slot = make_time_window(0, 60)
    assert can_task_fit_in_slot(task, slot) is True


def test_can_task_fit_in_slot_true_when_slot_larger(make_task, make_time_window) -> None:
    task = make_task(duration=60)
    slot = make_time_window(0, 120)
    assert can_task_fit_in_slot(task, slot) is True


def test_can_task_fit_in_slot_false_when_slot_too_small(make_task, make_time_window) -> None:
    task = make_task(duration=60)
    slot = make_time_window(0, 59)
    assert can_task_fit_in_slot(task, slot) is False
