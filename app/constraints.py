from app.models import Task, FixedBlock, DaySchedule, ScheduledTask, TimeWindow


def does_overlap(
    start1: int,
    end1: int,
    start2: int,
    end2: int,
) -> bool:
    """
    Returns True if two time intervals overlap.

    Runtime:
        O(1)
    """
    return start1 < end2 and start2 < end1


def is_inside_day(
    start_time: int,
    end_time: int,
    day_schedule: DaySchedule,
) -> bool:
    """
    Checks if a time interval is inside the allowed day window.

    Runtime:
        O(1)
    """
    return (
        day_schedule.time_window.start_time <= start_time
        and end_time <= day_schedule.time_window.end_time
    )


def respects_duration(
    task: Task,
    start_time: int,
    end_time: int,
) -> bool:
    """
    Checks if the scheduled interval matches the task duration.

    Runtime:
        O(1)
    """
    return end_time - start_time == task.duration


def respects_fixed_blocks(
    start_time: int,
    end_time: int,
    fixed_blocks: list[FixedBlock],
) -> bool:
    """
    Checks that a task does not overlap with any fixed block.

    Runtime:
        O(f)
    """
    for block in fixed_blocks:
        if does_overlap(
            start_time,
            end_time,
            block.time_window.start_time,
            block.time_window.end_time,
        ):
            return False

    return True


def respects_other_tasks(
    start_time: int,
    end_time: int,
    scheduled_tasks: list[ScheduledTask],
) -> bool:
    """
    Checks that a task does not overlap with already scheduled tasks.

    Runtime:
        O(n)
    """
    for scheduled_task in scheduled_tasks:
        if does_overlap(
            start_time,
            end_time,
            scheduled_task.time_window.start_time,
            scheduled_task.time_window.end_time,
        ):
            return False

    return True


def is_valid_task_placement(
    task: Task,
    start_time: int,
    end_time: int,
    day_schedule: DaySchedule,
    scheduled_tasks: list[ScheduledTask] | None = None,
) -> bool:
    """
    Main hard-constraint checker used by the optimizer.

    Checks:
        - task is inside the day window
        - task duration is correct
        - task does not overlap fixed blocks
        - task does not overlap already scheduled tasks

    Runtime:
        O(f + n)
    """
    if scheduled_tasks is None:
        scheduled_tasks = []

    if not is_inside_day(start_time, end_time, day_schedule):
        return False

    if not respects_duration(task, start_time, end_time):
        return False

    if not respects_fixed_blocks(
        start_time,
        end_time,
        day_schedule.fixed_blocks,
    ):
        return False

    if not respects_other_tasks(
        start_time,
        end_time,
        scheduled_tasks,
    ):
        return False

    return True


def validate_fixed_blocks(day_schedule: DaySchedule) -> bool:
    """
    Checks if fixed blocks are valid.

    Fixed blocks must:
        - be inside the day window
        - have start_time < end_time
        - not overlap with each other

    Runtime:
        O(f^2)
    """
    fixed_blocks = day_schedule.fixed_blocks

    for block in fixed_blocks:
        start_time = block.time_window.start_time
        end_time = block.time_window.end_time

        if end_time <= start_time:
            return False

        if not is_inside_day(start_time, end_time, day_schedule):
            return False

    for i in range(len(fixed_blocks)):
        for j in range(i + 1, len(fixed_blocks)):
            first = fixed_blocks[i].time_window
            second = fixed_blocks[j].time_window

            if does_overlap(
                first.start_time,
                first.end_time,
                second.start_time,
                second.end_time,
            ):
                return False

    return True


def get_available_time_slots(day_schedule: DaySchedule) -> list[TimeWindow]:
    """
    Returns free time windows after removing fixed blocks.

    Runtime:
        O(f log f)
    """
    fixed_blocks = sorted(
        day_schedule.fixed_blocks,
        key=lambda block: block.time_window.start_time,
    )

    available_slots: list[TimeWindow] = []
    current_time = day_schedule.time_window.start_time

    for block in fixed_blocks:
        block_start = block.time_window.start_time
        block_end = block.time_window.end_time

        if current_time < block_start:
            available_slots.append(
                TimeWindow(
                    start_time=current_time,
                    end_time=block_start,
                )
            )

        current_time = max(current_time, block_end)

    if current_time < day_schedule.time_window.end_time:
        available_slots.append(
            TimeWindow(
                start_time=current_time,
                end_time=day_schedule.time_window.end_time,
            )
        )

    return available_slots


def can_task_fit_in_slot(task: Task, slot: TimeWindow) -> bool:
    """
    Checks if a task can fit inside a given available time slot.

    Runtime:
        O(1)
    """
    return slot.end_time - slot.start_time >= task.duration