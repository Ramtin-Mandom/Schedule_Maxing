"""
pert.py

Dependency graph helpers over canonical task ids (app.planning.models.Task):
cycle detection and the required-task closure shared by the day engine
(app.optimizer) and allocation (app.planning.allocation). Edges are only
taken among task ids present in the given registry, so duplicate task names
never collide.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.planning.models import Task as CanonicalTask


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
