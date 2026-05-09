"""
pert.py

Dependency / PERT-style hard-constraint helpers for the schedule optimizer.

This module does not score tasks and does not choose time slots. Its job is to
handle prerequisite relationships between flexible tasks.

Main behavior:
    - Dependencies are represented as a directed graph.
    - Graph direction is: dependency -> task.
    - Missing dependencies are ignored, as requested.
    - Real dependencies must be scheduled before the tasks that depend on them.
    - Cycles between real flexible tasks are treated as invalid.

Example:
    If "Math Review" depends on "Math Exam", then:
        graph["Math Exam"] = ["Math Review"]

    This means Math Exam must finish before Math Review can start.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

from app.models import ScheduledTask, Task


# -----------------------------------------------------------------------------
# Basic dependency helpers
# -----------------------------------------------------------------------------


def get_task_dependencies(task: Task) -> list[str]:
    """
    Safely read a task's dependency names.

    Returns an empty list if the task has no dependencies or if the field is
    missing/None. Whitespace-only dependency names are removed.

    Runtime:
        O(d), where d is the number of dependency names on this task.
    """
    raw_dependencies = getattr(task, "dependencies", None) or []

    if isinstance(raw_dependencies, str):
        raw_dependencies = [raw_dependencies]

    return [
        str(dependency).strip()
        for dependency in raw_dependencies
        if str(dependency).strip()
    ]


def get_task_name_set(tasks: Iterable[Task]) -> set[str]:
    """
    Return the set of task names in a task collection.

    Runtime:
        O(n)
    """
    return {task.name for task in tasks}


def get_existing_dependency_names(
    task: Task,
    all_tasks: list[Task],
) -> list[str]:
    """
    Return only the dependency names that actually exist in all_tasks.

    Missing dependencies are intentionally ignored.

    Example:
        Task dependencies: ["Math Exam", "Missing Task"]
        Existing tasks:    ["Math Exam", "Math Review"]

        Returns:
            ["Math Exam"]

    This helper is useful for the optimizer because it separates:
        - missing dependencies, which should be ignored
        - real dependencies, which must be respected

    Runtime:
        O(n + d)
    """
    task_names = get_task_name_set(all_tasks)

    return [
        dependency
        for dependency in get_task_dependencies(task)
        if dependency in task_names
    ]


# -----------------------------------------------------------------------------
# Graph construction and graph validation
# -----------------------------------------------------------------------------


def build_dependency_graph(tasks: list[Task]) -> dict[str, list[str]]:
    """
    Build a dependency graph using only dependencies that exist as flexible tasks.

    Graph direction:
        dependency -> task

    Example:
        Task B depends on Task A
        graph["A"] = ["B"]

    Missing dependencies are ignored.

    Runtime:
        O(n + e)
        n = number of tasks
        e = number of existing dependency edges
    """
    graph: dict[str, list[str]] = defaultdict(list)

    for task in tasks:
        graph[task.name] = []

    for task in tasks:
        for dependency in get_existing_dependency_names(task, tasks):
            graph[dependency].append(task.name)

    return dict(graph)


def has_cycle(tasks: list[Task]) -> bool:
    """
    Return True if the dependency graph contains a cycle.

    Missing dependencies are ignored because they are not part of the actual
    schedulable task graph.

    Example of a cycle:
        A depends on B
        B depends on A

    Runtime:
        O(n + e)
    """
    graph = build_dependency_graph(tasks)
    visited: set[str] = set()
    recursion_stack: set[str] = set()

    def dfs(task_name: str) -> bool:
        visited.add(task_name)
        recursion_stack.add(task_name)

        for neighbor in graph.get(task_name, []):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in recursion_stack:
                return True

        recursion_stack.remove(task_name)
        return False

    for task_name in graph:
        if task_name not in visited:
            if dfs(task_name):
                return True

    return False


def get_topological_order(tasks: list[Task]) -> list[str]:
    """
    Return a valid dependency order for flexible tasks.

    Missing dependencies are ignored.

    Example:
        Read Chapter -> Do Assignment -> Review

    A returned order could be:
        ["Read Chapter", "Do Assignment", "Review"]

    Raises:
        ValueError: if a dependency cycle exists.

    Runtime:
        O(n + e)
    """
    graph = build_dependency_graph(tasks)

    in_degree = {task.name: 0 for task in tasks}

    for task in tasks:
        for dependency in get_existing_dependency_names(task, tasks):
            in_degree[task.name] += 1

    queue = deque(
        task_name
        for task_name, degree in in_degree.items()
        if degree == 0
    )

    order: list[str] = []

    while queue:
        current = queue.popleft()
        order.append(current)

        for neighbor in graph.get(current, []):
            in_degree[neighbor] -= 1

            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(order) != len(in_degree):
        raise ValueError("Dependency cycle detected. No valid PERT order exists.")

    return order


# -----------------------------------------------------------------------------
# Optimizer-facing dependency helpers
# -----------------------------------------------------------------------------


def get_dependency_end_time(
    task: Task,
    all_tasks: list[Task],
    scheduled_tasks: list[ScheduledTask],
) -> int | None:
    """
    Return the earliest start time required by this task's real dependencies.

    Missing dependencies are ignored.

    Returns:
        int:
            The maximum end_time among this task's scheduled real dependencies.
            If the task has no real dependencies, returns 0.

        None:
            At least one real dependency exists but has not been scheduled yet.
            The optimizer should wait before scheduling this task.

    Example:
        Math Review depends on Math Exam.

        If Math Exam is scheduled from 300 to 360:
            returns 360

        If Math Exam exists but has not been scheduled yet:
            returns None

        If dependency name does not exist in all_tasks:
            ignored

    Runtime:
        O(n + s + d)
    """
    existing_dependencies = get_existing_dependency_names(task, all_tasks)

    if not existing_dependencies:
        return 0

    scheduled_lookup = {
        scheduled.name: scheduled
        for scheduled in scheduled_tasks
    }

    latest_dependency_end = 0

    for dependency_name in existing_dependencies:
        dependency_scheduled = scheduled_lookup.get(dependency_name)

        if dependency_scheduled is None:
            return None

        latest_dependency_end = max(
            latest_dependency_end,
            dependency_scheduled.time_window.end_time,
        )

    return latest_dependency_end


def get_ready_tasks(
    remaining_tasks: list[Task],
    all_tasks: list[Task],
    scheduled_tasks: list[ScheduledTask],
) -> list[Task]:
    """
    Return remaining tasks whose real dependencies are already scheduled.

    Missing dependencies are ignored.

    This is useful for optimizers that repeatedly choose the best currently
    schedulable task.

    Runtime:
        O(r * (n + s + d))
    """
    ready_tasks: list[Task] = []

    for task in remaining_tasks:
        dependency_end_time = get_dependency_end_time(
            task=task,
            all_tasks=all_tasks,
            scheduled_tasks=scheduled_tasks,
        )

        if dependency_end_time is not None:
            ready_tasks.append(task)

    return ready_tasks


# -----------------------------------------------------------------------------
# Compatibility and final validation
# -----------------------------------------------------------------------------


def validate_dependencies_exist(tasks: list[Task]) -> bool:
    """
    Backward-compatible helper for older optimizer code.

    Missing dependencies are now allowed and ignored, so this always returns True.

    Runtime:
        O(1)
    """
    return True


def respects_dependency_order(
    scheduled_tasks: list[ScheduledTask],
    original_tasks: list[Task],
) -> bool:
    """
    Check if scheduled task times respect real dependencies.

    If B depends on A:
        A must end before B starts.

    Missing dependencies are ignored.

    Important:
        This function only enforces dependencies when both the task and its real
        dependency appear in scheduled_tasks.

    Runtime:
        O(n + s + e)
    """
    scheduled_lookup = {
        task.name: task
        for task in scheduled_tasks
    }

    for task in original_tasks:
        current_scheduled = scheduled_lookup.get(task.name)

        if current_scheduled is None:
            continue

        for dependency_name in get_existing_dependency_names(task, original_tasks):
            dependency_scheduled = scheduled_lookup.get(dependency_name)

            if dependency_scheduled is None:
                continue

            dependency_end = dependency_scheduled.time_window.end_time
            current_start = current_scheduled.time_window.start_time

            if dependency_end > current_start:
                return False

    return True


def validate_pert_constraints(
    tasks: list[Task],
    scheduled_tasks: list[ScheduledTask] | None = None,
) -> bool:
    """
    Main PERT hard-constraint validator.

    Checks:
        1. No dependency cycles between existing flexible tasks.
        2. If scheduled_tasks is provided, scheduled task times respect real
           dependency order.

    Missing dependencies are ignored.

    Runtime:
        O(n + e), without scheduled_tasks
        O(n + s + e), with scheduled_tasks
    """
    if has_cycle(tasks):
        return False

    if scheduled_tasks is not None:
        if not respects_dependency_order(
            scheduled_tasks=scheduled_tasks,
            original_tasks=tasks,
        ):
            return False

    return True


def assert_pert_constraints(
    tasks: list[Task],
    scheduled_tasks: list[ScheduledTask] | None = None,
) -> None:
    """
    Raise a clear error if PERT constraints are invalid.

    This is useful for UI or optimizer code when you want an explanatory failure
    instead of only True/False.

    Raises:
        ValueError: if the dependency graph has a cycle or the final schedule
        violates dependency timing.
    """
    if has_cycle(tasks):
        raise ValueError("Dependency cycle detected between flexible tasks.")

    if scheduled_tasks is not None and not respects_dependency_order(
        scheduled_tasks=scheduled_tasks,
        original_tasks=tasks,
    ):
        raise ValueError("Scheduled tasks do not respect dependency order.")
