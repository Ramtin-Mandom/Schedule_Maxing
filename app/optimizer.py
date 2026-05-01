# optimizer.py

import random
import math
from copy import deepcopy

from app.models import Task, ScheduledTask, TimeWindow, DaySchedule, DayScheduleOutput
from app.reward import score_day_schedule
from app.constraints import is_valid_task_placement
from app.pert import validate_pert_constraints, get_topological_order
from config.settings import (
    TIME_SLOT_MINUTES,
    INITIAL_TEMPERATURE,
    MIN_TEMPERATURE,
    COOLING_RATE,
    MAX_ITERATIONS,
    NO_IMPROVEMENT_LIMIT,
)


def create_scheduled_task(task: Task, start_time: int) -> ScheduledTask:
    return ScheduledTask(
        name=task.name,
        category=task.category,
        tag=task.tag,
        time_window=TimeWindow(
            start_time=start_time,
            end_time=start_time + task.duration,
        ),
        score=0,
    )


def sort_tasks_by_dependency_order(tasks: list[Task]) -> list[Task]:
    """
    Sorts tasks so dependencies come before dependent tasks.

    If tasks have the same dependency level, higher priority tasks
    are preferred earlier.
    """
    dependency_order = get_topological_order(tasks)

    task_lookup = {
        task.name: task
        for task in tasks
    }

    ordered_tasks = [
        task_lookup[task_name]
        for task_name in dependency_order
        if task_name in task_lookup
    ]

    return sorted(
        ordered_tasks,
        key=lambda task: (
            dependency_order.index(task.name),
            -task.priority,
        ),
    )


def generate_initial_schedule(day_schedule: DaySchedule) -> list[ScheduledTask]:
    """
    Creates a valid starting schedule using dependency order.

    Dependencies are scheduled before tasks that depend on them.
    """
    scheduled_tasks: list[ScheduledTask] = []

    sorted_tasks = sort_tasks_by_dependency_order(day_schedule.tasks)

    for task in sorted_tasks:
        for start_time in range(
            day_schedule.time_window.start_time,
            day_schedule.time_window.end_time,
            TIME_SLOT_MINUTES,
        ):
            end_time = start_time + task.duration
            candidate = create_scheduled_task(task, start_time)

            temp_schedule = scheduled_tasks + [candidate]

            if not is_valid_task_placement(
                task=task,
                start_time=start_time,
                end_time=end_time,
                day_schedule=day_schedule,
                scheduled_tasks=scheduled_tasks,
            ):
                continue

            if not validate_pert_constraints(
                tasks=day_schedule.tasks,
                scheduled_tasks=temp_schedule,
            ):
                continue

            scheduled_tasks.append(candidate)
            break

    return scheduled_tasks


def generate_neighbor(
    current_schedule: list[ScheduledTask],
    original_tasks: list[Task],
    day_schedule: DaySchedule,
) -> list[ScheduledTask]:
    """
    Creates a neighboring schedule by moving one random task
    to another valid time slot.

    The neighbor must also respect dependency order.
    """
    if not current_schedule:
        return current_schedule

    neighbor = deepcopy(current_schedule)

    task_to_move = random.choice(neighbor)

    original_task_lookup = {
        task.name: task
        for task in original_tasks
    }

    original_task = original_task_lookup[task_to_move.name]

    other_scheduled_tasks = [
        task for task in neighbor
        if task.name != task_to_move.name
    ]

    possible_start_times = list(
        range(
            day_schedule.time_window.start_time,
            day_schedule.time_window.end_time,
            TIME_SLOT_MINUTES,
        )
    )

    random.shuffle(possible_start_times)

    for new_start_time in possible_start_times:
        new_end_time = new_start_time + original_task.duration

        if not is_valid_task_placement(
            task=original_task,
            start_time=new_start_time,
            end_time=new_end_time,
            day_schedule=day_schedule,
            scheduled_tasks=other_scheduled_tasks,
        ):
            continue

        task_to_move.time_window = TimeWindow(
            start_time=new_start_time,
            end_time=new_end_time,
        )

        if validate_pert_constraints(
            tasks=original_tasks,
            scheduled_tasks=neighbor,
        ):
            return neighbor

    return current_schedule


def should_accept_neighbor(
    current_score: float,
    neighbor_score: float,
    temperature: float,
) -> bool:
    if neighbor_score > current_score:
        return True

    score_difference = current_score - neighbor_score

    acceptance_probability = math.exp(
        -score_difference / temperature
    )

    return random.random() < acceptance_probability


def optimize_day_schedule(
    date: int,
    day_schedule: DaySchedule,
) -> DayScheduleOutput:
    """
    Optimizes one day's schedule using simulated annealing,
    while enforcing PERT dependency constraints.
    """
    if not validate_pert_constraints(day_schedule.tasks):
        raise ValueError("Invalid task dependencies. Check for missing dependencies or cycles.")

    current_schedule = generate_initial_schedule(day_schedule)
    current_score = score_day_schedule(day_schedule.tasks, current_schedule)

    best_schedule = deepcopy(current_schedule)
    best_score = current_score

    temperature = INITIAL_TEMPERATURE
    no_improvement_count = 0

    for _ in range(MAX_ITERATIONS):
        if temperature <= MIN_TEMPERATURE:
            break

        if no_improvement_count >= NO_IMPROVEMENT_LIMIT:
            break

        neighbor_schedule = generate_neighbor(
            current_schedule=current_schedule,
            original_tasks=day_schedule.tasks,
            day_schedule=day_schedule,
        )

        if not validate_pert_constraints(
            tasks=day_schedule.tasks,
            scheduled_tasks=neighbor_schedule,
        ):
            no_improvement_count += 1
            temperature *= COOLING_RATE
            continue

        neighbor_score = score_day_schedule(
            day_schedule.tasks,
            neighbor_schedule,
        )

        if should_accept_neighbor(
            current_score=current_score,
            neighbor_score=neighbor_score,
            temperature=temperature,
        ):
            current_schedule = neighbor_schedule
            current_score = neighbor_score

        if current_score > best_score:
            best_schedule = deepcopy(current_schedule)
            best_score = current_score
            no_improvement_count = 0
        else:
            no_improvement_count += 1

        temperature *= COOLING_RATE

    scheduled_task_names = {
        task.name for task in best_schedule
    }

    unscheduled_tasks = [
        task for task in day_schedule.tasks
        if task.name not in scheduled_task_names
    ]

    return DayScheduleOutput(
        date=date,
        total_score=best_score,
        scheduled_tasks=best_schedule,
        unscheduled_tasks=unscheduled_tasks,
    )


def fixed_block_to_scheduled_task(fixed_block) -> ScheduledTask:
    return ScheduledTask(
        name=fixed_block.name,
        category=fixed_block.category,
        tag="fixed",
        time_window=fixed_block.time_window,
        score=0,
    )


def merge_scheduled_tasks_by_start_time(
    fixed_tasks: list[ScheduledTask],
    day_output: DayScheduleOutput,
) -> DayScheduleOutput:
    combined_schedule: list[ScheduledTask] = []

    optimized_tasks = sorted(
        day_output.scheduled_tasks,
        key=lambda task: task.time_window.start_time,
    )

    i = 0
    j = 0

    while i < len(fixed_tasks) and j < len(optimized_tasks):
        fixed_start = fixed_tasks[i].time_window.start_time
        optimized_start = optimized_tasks[j].time_window.start_time

        if fixed_start <= optimized_start:
            combined_schedule.append(fixed_tasks[i])
            i += 1
        else:
            combined_schedule.append(optimized_tasks[j])
            j += 1

    while i < len(fixed_tasks):
        combined_schedule.append(fixed_tasks[i])
        i += 1

    while j < len(optimized_tasks):
        combined_schedule.append(optimized_tasks[j])
        j += 1

    return DayScheduleOutput(
        date=day_output.date,
        total_score=day_output.total_score,
        scheduled_tasks=combined_schedule,
        unscheduled_tasks=day_output.unscheduled_tasks,
    )


def combine_fixed_and_optimized_scheduled_tasks(
    date: int,
    day_schedule: DaySchedule,
) -> DayScheduleOutput:
    day_output = optimize_day_schedule(
        date=date,
        day_schedule=day_schedule,
    )

    fixed_tasks = [
        fixed_block_to_scheduled_task(block)
        for block in day_schedule.fixed_blocks
    ]

    fixed_tasks.sort(key=lambda task: task.time_window.start_time)

    return merge_scheduled_tasks_by_start_time(
        fixed_tasks=fixed_tasks,
        day_output=day_output,
    )