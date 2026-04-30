from models import Task, FixedBlock, ScheduleInput


def does_overlap(start1: int, end1: int, start2: int, end2: int) -> bool:
    """
    Returns True if two time intervals overlap.
    Time is represented in minutes from start of day.
    """
    return start1 < end2 and start2 < end1


def is_inside_day(start_time: int, end_time: int, day_start: int, day_end: int) -> bool:
    """
    Checks if a task is placed inside the allowed schedule day.
    """
    return day_start <= start_time and end_time <= day_end


def respects_duration(start_time: int, end_time: int, duration: int) -> bool:
    """
    Checks if the task placement matches its required duration.
    """
    return end_time - start_time == duration


def respects_fixed_blocks(
    start_time: int,
    end_time: int,
    fixed_blocks: list[FixedBlock]
) -> bool:
    """
    Checks that a movable task does not overlap with any fixed block.
    """
    for block in fixed_blocks:
        if does_overlap(start_time, end_time, block.start_time, block.end_time):
            return False

    return True


def respects_other_tasks(
    start_time: int,
    end_time: int,
    scheduled_tasks: list[Task]
) -> bool:
    """
    Checks that a task does not overlap with already scheduled tasks.
    Only checks tasks that already have start_time and end_time.
    """
    for task in scheduled_tasks:
        if task.start_time is None or task.end_time is None:
            continue

        if does_overlap(start_time, end_time, task.start_time, task.end_time):
            return False

    return True


def is_valid_task_placement(
    task: Task,
    start_time: int,
    end_time: int,
    schedule_input: ScheduleInput,
    scheduled_tasks: list[Task] | None = None
) -> bool:
    """
    Main function used by the optimizer.

    It checks whether a task can be placed at a specific time.
    """
    if scheduled_tasks is None:
        scheduled_tasks = []

    if not is_inside_day(
        start_time,
        end_time,
        schedule_input.day_start,
        schedule_input.day_end
    ):
        return False

    if not respects_duration(start_time, end_time, task.duration):
        return False

    if not respects_fixed_blocks(
        start_time,
        end_time,
        schedule_input.fixed_blocks
    ):
        return False

    if not respects_other_tasks(start_time, end_time, scheduled_tasks):
        return False

    return True


def validate_fixed_blocks(schedule_input: ScheduleInput) -> bool:
    """
    Checks if fixed blocks are valid.

    Fixed blocks should:
    - be inside the day
    - not overlap with each other
    """
    fixed_blocks = schedule_input.fixed_blocks

    for block in fixed_blocks:
        if not is_inside_day(
            block.start_time,
            block.end_time,
            schedule_input.day_start,
            schedule_input.day_end
        ):
            return False

        if block.end_time <= block.start_time:
            return False

    for i in range(len(fixed_blocks)):
        for j in range(i + 1, len(fixed_blocks)):
            block1 = fixed_blocks[i]
            block2 = fixed_blocks[j]

            if does_overlap(
                block1.start_time,
                block1.end_time,
                block2.start_time,
                block2.end_time
            ):
                return False

    return True


def get_available_time_slots(schedule_input: ScheduleInput) -> list[tuple[int, int]]:
    """
    Returns free time slots after removing fixed blocks.

    Example:
    day_start = 480
    day_end = 1320
    fixed block = 600-660

    Result:
    [(480, 600), (660, 1320)]
    """
    fixed_blocks = sorted(
        schedule_input.fixed_blocks,
        key=lambda block: block.start_time
    )

    available_slots = []
    current_time = schedule_input.day_start

    for block in fixed_blocks:
        if current_time < block.start_time:
            available_slots.append((current_time, block.start_time))

        current_time = max(current_time, block.end_time)

    if current_time < schedule_input.day_end:
        available_slots.append((current_time, schedule_input.day_end))

    return available_slots


def can_task_fit_in_slot(task: Task, slot: tuple[int, int]) -> bool:
    """
    Checks if a task can fit inside a specific available time slot.
    """
    slot_start, slot_end = slot
    return slot_end - slot_start >= task.duration