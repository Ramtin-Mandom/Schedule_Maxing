from app.models import Task, ScheduledTask, TimeWindow
from config.settings import (
    WEIGHT_PRIORITY,
    WEIGHT_PREFERENCE_TIME,
    WEIGHT_TAG_RELATION,
    WEIGHT_SPACING,
    WEIGHT_NO_BREAK_PENALTY,
    PREFERENCE_TIME_DISTANCE_SCALE,
    TAG_RELATION_MAX_GAP,
    MIN_GOOD_BREAK,
    MAX_GOOD_BREAK,
    BACK_TO_BACK_GAP,
)


def get_window_center(time_window: TimeWindow) -> float:
    """
    Computes the midpoint of a given time window.
    Args: time_window (TimeWindow): The time interval.
    Returns: float: The center time of the interval.
    Runtime: O(1)
    """
    return (time_window.start_time + time_window.end_time) / 2


def score_priority(task: Task) -> float:
    """
    Computes score contribution based on task priority.
    Args:task (Task): The original task.
    Returns: float: Priority-based score.
    Runtime: O(1)
    """
    return task.priority * WEIGHT_PRIORITY


def score_preference_time(task: Task, scheduled_task: ScheduledTask) -> float:
    """
    Scores how close a scheduled task is to its preferred time window.
    Args:task (Task): Original task containing preference_time.
        scheduled_task (ScheduledTask): Task after scheduling.
    Returns: float: Higher score for closer alignment to preferred time.
    Runtime: O(1)
    """
    preferred_center = get_window_center(task.preference_time)
    scheduled_center = get_window_center(scheduled_task.time_window)

    distance = abs(preferred_center - scheduled_center)

    return max(
        0,
        WEIGHT_PREFERENCE_TIME * (1 - distance / PREFERENCE_TIME_DISTANCE_SCALE)
    )


def score_tag_relation(
    scheduled_task: ScheduledTask,
    all_scheduled_tasks: list[ScheduledTask],
) -> float:
    """
    Rewards tasks placed near other tasks with the same tag.
    Example: Study task placed near lecture of same subject.
    Args: scheduled_task (ScheduledTask): Current task being evaluated.
        all_scheduled_tasks (list): All tasks in the schedule.
    Returns: float: Tag-based proximity score.
    Runtime: O(n) where n = number of scheduled tasks
    """
    score = 0

    for other in all_scheduled_tasks:
        if other.name == scheduled_task.name:
            continue

        if other.tag == scheduled_task.tag:
            time_gap = abs(
                scheduled_task.time_window.start_time
                - other.time_window.end_time
            )

            if time_gap <= TAG_RELATION_MAX_GAP:
                score += WEIGHT_TAG_RELATION

    return score


def score_spacing(all_scheduled_tasks: list[ScheduledTask]) -> float:
    """
    Rewards good spacing between tasks and penalizes poor scheduling.
    Good spacing: Breaks between tasks (within configured range)
    Bad spacing: Tasks scheduled back-to-back (no gap)
    Args: all_scheduled_tasks (list): Scheduled tasks for the day.
    Returns: float: Total spacing score.
    Runtime: O(n log n) due to sorting
    """
    if len(all_scheduled_tasks) <= 1:
        return 0

    sorted_tasks = sorted(
        all_scheduled_tasks,
        key=lambda task: task.time_window.start_time,
    )

    score = 0

    for i in range(len(sorted_tasks) - 1):
        current_task = sorted_tasks[i]
        next_task = sorted_tasks[i + 1]

        gap = (
            next_task.time_window.start_time
            - current_task.time_window.end_time
        )

        if MIN_GOOD_BREAK <= gap <= MAX_GOOD_BREAK:
            score += WEIGHT_SPACING

        elif gap == BACK_TO_BACK_GAP:
            score += WEIGHT_NO_BREAK_PENALTY

    return score


def score_single_task(
    task: Task,
    scheduled_task: ScheduledTask,
    all_scheduled_tasks: list[ScheduledTask],
) -> float:
    """
    Computes total score contribution for a single task.
    Combines:
        - Priority score
        - Preference time score
        - Tag relation score
    Args:
        task (Task): Original task.
        scheduled_task (ScheduledTask): Scheduled version.
        all_scheduled_tasks (list): All tasks for context scoring.
    Returns: float: Total score for this task.
    Runtime: O(n)
    """
    total = 0

    total += score_priority(task)
    total += score_preference_time(task, scheduled_task)
    total += score_tag_relation(scheduled_task, all_scheduled_tasks)

    return total


def score_day_schedule(
    original_tasks: list[Task],
    scheduled_tasks: list[ScheduledTask],
) -> float:
    """
    Computes total score for a full day schedule.
    Steps:
        1. Match scheduled tasks to original tasks
        2. Score each task individually
        3. Add spacing score
    Args:
        original_tasks (list[Task]): Input task definitions.
        scheduled_tasks (list[ScheduledTask]): Scheduled result.
    Returns:
        float: Total score of the schedule.
    Runtime:
        O(m + n^2)
        m = number of original tasks
        n = number of scheduled tasks
    """
    total_score = 0

    task_lookup = {
        task.name: task
        for task in original_tasks
    }

    for scheduled_task in scheduled_tasks:
        original_task = task_lookup.get(scheduled_task.name)

        if original_task is None:
            continue

        task_score = score_single_task(
            original_task,
            scheduled_task,
            scheduled_tasks,
        )

        scheduled_task.score = task_score
        total_score += task_score

    total_score += score_spacing(scheduled_tasks)

    return total_score