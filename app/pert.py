from collections import defaultdict, deque
from app.models import Task, ScheduledTask


def build_dependency_graph(tasks: list[Task]) -> dict[str, list[str]]:
    """
    Builds graph where:
    dependency -> task

    Example:
    Task B depends on Task A
    graph["A"] = ["B"]
    Runtime: O(n + e)
    """
    graph: dict[str, list[str]] = defaultdict(list)

    for task in tasks:
        if task.name not in graph:
            graph[task.name] = []

        for dependency in task.dependencies:
            graph[dependency].append(task.name)

    return dict(graph)


def has_cycle(tasks: list[Task]) -> bool:
    """
    Returns True if dependencies contain a cycle.
    A cycle means the schedule is impossible.
    Runtime: O(n + e)
    """
    graph = build_dependency_graph(tasks)
    visited = set()
    recursion_stack = set()

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
    Returns a valid dependency order.

    Example:
    Read Chapter -> Do Assignment -> Review
    Runtime: O(n + e)
    """
    graph = build_dependency_graph(tasks)
    in_degree = {task.name: 0 for task in tasks}

    for task in tasks:
        for dependency in task.dependencies:
            if dependency not in in_degree:
                in_degree[dependency] = 0

            in_degree[task.name] += 1

    queue = deque()

    for task_name, degree in in_degree.items():
        if degree == 0:
            queue.append(task_name)

    order = []

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


def validate_dependencies_exist(tasks: list[Task]) -> bool:
    """
    Checks that every dependency name actually exists as a task.
    Runtime: O(n + e)
    """
    task_names = {task.name for task in tasks}

    for task in tasks:
        for dependency in task.dependencies:
            if dependency not in task_names:
                return False

    return True


def respects_dependency_order(
    scheduled_tasks: list[ScheduledTask],
    original_tasks: list[Task],
) -> bool:
    """
    Checks if scheduled task times respect dependencies.

    If B depends on A:
    A must end before B starts.
    Runtime: O(n + e)
    """
    scheduled_lookup = {
        task.name: task
        for task in scheduled_tasks
    }

    for task in original_tasks:
        current_scheduled = scheduled_lookup.get(task.name)

        if current_scheduled is None:
            continue

        for dependency_name in task.dependencies:
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
    Main PERT hard constraint validator.

    It checks:
    1. All dependencies exist
    2. No dependency cycles exist
    3. Scheduled task order respects dependencies
    Runtime: O(n + e)
    """
    if not validate_dependencies_exist(tasks):
        return False

    if has_cycle(tasks):
        return False

    if scheduled_tasks is not None:
        if not respects_dependency_order(scheduled_tasks, tasks):
            return False

    return True