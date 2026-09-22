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

import uuid
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Iterable

from app.models import ScheduledTask, Task

if TYPE_CHECKING:
    from app.planning.models import Task as CanonicalTask


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


# -----------------------------------------------------------------------------
# ID-based helpers (Task 4 / Schedule Maxing v2 canonical engine)
# -----------------------------------------------------------------------------
#
# These are stable-identity counterparts of the name-based helpers above, for
# app/optimizer.py's canonical day engine (generate_day_schedule). Name
# resolution (legacy CSV/UI dependency strings -> task_ids) happens once, at
# import boundaries (app/planning/compat.py) -- never inside scheduling.
# Everything below operates purely on uuid.UUID task_ids and a
# {task_id: Task} registry/subset, so duplicate task names never collide in
# the graph or in optimizer output.


def build_dependency_graph_by_id(
    tasks: dict[uuid.UUID, "CanonicalTask"],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """
    ID-based counterpart of build_dependency_graph.

    Graph direction: dependency_id -> task_id (same convention as the
    name-based graph). Only dependency_ids that are keys of `tasks` become
    edges here -- a dependency_id referencing a task outside this local set
    is a *known external* dependency (see app.planning.compat's resolution
    contract and Task 5's cross-day contract), not a missing/ignored name;
    it is simply not part of this local graph, and callers needing that
    wider context resolve it separately (see optimizer.py's
    external_dependency_ends).

    Runtime: O(n + e).
    """
    graph: dict[uuid.UUID, list[uuid.UUID]] = {task_id: [] for task_id in tasks}

    for task_id, task in tasks.items():
        for dependency_id in task.dependency_ids:
            if dependency_id in tasks:
                graph[dependency_id].append(task_id)

    return graph


def has_cycle_by_id(tasks: dict[uuid.UUID, "CanonicalTask"]) -> bool:
    """
    ID-based counterpart of has_cycle: True if the *local* dependency graph
    (edges only among task_ids present in `tasks`) contains a cycle.
    Detects cycles once, up front -- callers should call this before
    searching for placements, not repeatedly during the search.

    Runtime: O(n + e).
    """
    graph = build_dependency_graph_by_id(tasks)
    visited: set[uuid.UUID] = set()
    recursion_stack: set[uuid.UUID] = set()

    def dfs(task_id: uuid.UUID) -> bool:
        visited.add(task_id)
        recursion_stack.add(task_id)

        for neighbor in graph.get(task_id, []):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in recursion_stack:
                return True

        recursion_stack.remove(task_id)
        return False

    for task_id in graph:
        if task_id not in visited and dfs(task_id):
            return True

    return False


def get_topological_order_by_id(tasks: dict[uuid.UUID, "CanonicalTask"]) -> list[uuid.UUID]:
    """
    ID-based counterpart of get_topological_order, over the *local*
    dependency graph (see build_dependency_graph_by_id). Raises ValueError
    if a cycle exists.

    Runtime: O(n + e).
    """
    graph = build_dependency_graph_by_id(tasks)
    in_degree = {task_id: 0 for task_id in tasks}

    for task_id, task in tasks.items():
        for dependency_id in task.dependency_ids:
            if dependency_id in tasks:
                in_degree[task_id] += 1

    queue = deque(task_id for task_id, degree in in_degree.items() if degree == 0)
    order: list[uuid.UUID] = []

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


def compute_required_closure(
    task_ids: list[uuid.UUID],
    registry: dict[uuid.UUID, "CanonicalTask"],
) -> set[uuid.UUID]:
    """
    Every required task_id in `task_ids`, plus the full transitive closure
    of its dependency_ids that are present in `registry` -- an optional
    prerequisite of a required task is essential for scheduling/allocating
    it, without mutating the prerequisite's own stored `required` flag.

    Shared by app.optimizer's day engine (Task 4) and
    app.planning.allocation (Task 5), so "what counts as essential for this
    run" stays one definition.

    Runtime: O(n + e).
    """
    essential: set[uuid.UUID] = {task_id for task_id in task_ids if registry[task_id].required}
    stack = list(essential)

    while stack:
        current = stack.pop()
        for dependency_id in registry[current].dependency_ids:
            if dependency_id in registry and dependency_id not in essential:
                essential.add(dependency_id)
                stack.append(dependency_id)

    return essential


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
