"""
optimizer.py

Greedy schedule optimizer that now uses `task_prefrence.yaml`
through reward.py.

Important behavior:
    - Fixed tasks are placed first.
    - Movable tasks are placed in the valid slot with the highest reward score.
    - Existing dependencies are respected.
    - Missing dependencies are ignored, as requested.
    - Reward weights/preferences come from task_prefrence.yaml when available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models import DayScheduleOutput, ScheduledTask, TimeWindow

try:
    from app.models import UnscheduledTask
except ImportError:
    UnscheduledTask = None  # type: ignore

from app.pert import assert_pert_constraints, get_dependency_end_time
from app.reward import RewardSettings, calculate_task_score, load_reward_settings


TIME_SLOT_MINUTES = 30


def optimize_day_schedule(
    day_schedule: Any,
    date: int | None = None,
    config_path: str | Path | None = None,
) -> DayScheduleOutput:
    """
    Optimize one day of scheduling.

    Parameters
    ----------
    day_schedule:
        Your DaySchedule object. Expected fields:
            - time_window
            - fixed_blocks
            - tasks

    date:
        Optional date/day number. If not provided, this function tries to read
        `day_schedule.date`, then falls back to 1.

    config_path:
        Optional direct path to task_prefrence.yaml.

    Returns
    -------
    DayScheduleOutput
        Final scheduled tasks, total score, and unscheduled tasks when supported
        by your model.
    """

    settings = load_reward_settings(config_path)

    day_start = int(_get(_get(day_schedule, "time_window"), "start_time", 0))
    day_end = int(_get(_get(day_schedule, "time_window"), "end_time", 1440))

    fixed_tasks = _build_fixed_scheduled_tasks(day_schedule)
    movable_tasks = [
        task
        for task in list(_get(day_schedule, "tasks", []))
        if not bool(_get(task, "fixed", False))
    ]

    scheduled_tasks: list[Any] = sorted(
        fixed_tasks,
        key=lambda task: _get(_get(task, "time_window"), "start_time", 0),
    )

    unscheduled: list[Any] = []
    remaining = movable_tasks[:]

    # Validate the dependency graph before scheduling. Missing dependency names
    # are ignored inside pert.py, but real dependency cycles are invalid.
    assert_pert_constraints(movable_tasks)

    # Repeatedly schedule the best currently feasible task.
    # This lets dependencies become available as their prerequisite tasks are placed.
    while remaining:
        best_task = None
        best_candidate = None
        best_score = float("-inf")

        made_progress = False

        for task in remaining:
            dependency_end = get_dependency_end_time(
                task=task,
                all_tasks=movable_tasks,
                scheduled_tasks=scheduled_tasks,
            )

            # If real dependencies exist but are not scheduled yet, wait.
            # Missing dependency names are ignored inside pert.py.
            if dependency_end is None:
                continue

            candidate = _best_candidate_for_task(
                task=task,
                scheduled_tasks=scheduled_tasks,
                day_start=day_start,
                day_end=day_end,
                earliest_start=dependency_end,
                settings=settings,
            )

            if candidate is None:
                continue

            candidate_start, candidate_end, candidate_score = candidate

            if candidate_score > best_score:
                best_task = task
                best_candidate = (candidate_start, candidate_end)
                best_score = candidate_score

        if best_task is not None and best_candidate is not None:
            start_time, end_time = best_candidate
            scheduled = _make_scheduled_task(best_task, start_time, end_time, best_score)
            scheduled_tasks.append(scheduled)
            scheduled_tasks.sort(key=lambda item: _get(_get(item, "time_window"), "start_time", 0))
            remaining.remove(best_task)
            made_progress = True

        if made_progress:
            continue

        # If no task can progress, either there is no space or there are dependency cycles.
        # Missing dependencies are ignored, but cycles between real tasks cannot be resolved.
        for task in remaining:
            reason = "not enough valid space or unresolved dependency cycle"
            unscheduled.append(_make_unscheduled_task(task, reason))
        break

    # Final safety check: the produced schedule should still respect real
    # dependencies. This catches dependency-order bugs without changing scoring.
    assert_pert_constraints(movable_tasks, scheduled_tasks)

    total_score = round(sum(float(_get(task, "score", 0.0)) for task in scheduled_tasks), 2)
    output_date = date if date is not None else int(_get(day_schedule, "date", 1))

    return _make_day_schedule_output(
        date=output_date,
        total_score=total_score,
        scheduled_tasks=scheduled_tasks,
        unscheduled_tasks=unscheduled,
    )


def combine_fixed_and_optimized_scheduled_tasks(
    *,
    date: int,
    day_schedule: Any,
    config_path: str | Path | None = None,
) -> DayScheduleOutput:
    """Backward-compatible entry point used by ``app.main`` and ``app.app``."""
    return optimize_day_schedule(
        day_schedule,
        date=date,
        config_path=config_path,
    )


def _best_candidate_for_task(
    task: Any,
    scheduled_tasks: list[Any],
    day_start: int,
    day_end: int,
    earliest_start: int,
    settings: RewardSettings,
) -> tuple[int, int, float] | None:
    duration = int(_get(task, "duration", 0))

    if duration <= 0:
        return None

    first_start = max(day_start, earliest_start)

    # Snap to the next 30-minute slot.
    if first_start % TIME_SLOT_MINUTES != 0:
        first_start += TIME_SLOT_MINUTES - (first_start % TIME_SLOT_MINUTES)

    latest_start = day_end - duration

    best_candidate = None
    best_score = float("-inf")

    for start_time in range(first_start, latest_start + 1, TIME_SLOT_MINUTES):
        end_time = start_time + duration

        if _overlaps_any(start_time, end_time, scheduled_tasks):
            continue

        previous_task, next_task = _neighbors_for_candidate(start_time, end_time, scheduled_tasks)

        score = calculate_task_score(
            task,
            start_time,
            previous_task=previous_task,
            next_task=next_task,
            settings=settings,
        )

        if score > best_score:
            best_score = score
            best_candidate = (start_time, end_time, score)

    return best_candidate


def _dependency_end_time(task: Any, scheduled_tasks: list[Any]) -> int | None:
    """
    Return the earliest start requirement caused by already-known dependencies.

    Missing dependencies are ignored.

    Returns:
        - max dependency end time if all real dependencies are already scheduled
        - 0 if there are no dependencies or all dependency names are missing
        - None if a real dependency exists but has not been scheduled yet
    """

    dependency_names = _normalize_dependencies(_get(task, "dependencies", []))

    if not dependency_names:
        return 0

    all_scheduled_names = {str(_get(item, "name", "")) for item in scheduled_tasks}

    # These are all task names visible in the current scheduled list.
    # If a dependency is not found here yet, it might either be missing or unscheduled.
    dependency_end = 0

    for dependency_name in dependency_names:
        matching = [
            item
            for item in scheduled_tasks
            if str(_get(item, "name", "")) == dependency_name
        ]

        if matching:
            dependency_window = _get(matching[0], "time_window", None)
            dependency_end = max(dependency_end, int(_get(dependency_window, "end_time", 0)))
            continue

        # Missing dependency is ignored.
        # A real-but-unscheduled dependency will be handled by the optimizer naturally
        # if it appears later in remaining tasks. Since this helper does not know
        # remaining task names, the caller chooses task order by repeated passes.
        if dependency_name not in all_scheduled_names:
            continue

    return dependency_end


def _build_fixed_scheduled_tasks(day_schedule: Any) -> list[Any]:
    fixed_scheduled_tasks: list[Any] = []

    for fixed_block in list(_get(day_schedule, "fixed_blocks", [])):
        time_window = _get(fixed_block, "time_window", None)
        fixed_scheduled_tasks.append(
            _make_scheduled_task(
                source=fixed_block,
                start_time=int(_get(time_window, "start_time", 0)),
                end_time=int(_get(time_window, "end_time", 0)),
                score=0.0,
            )
        )

    for task in list(_get(day_schedule, "tasks", [])):
        if not bool(_get(task, "fixed", False)):
            continue

        start_time = int(_get(task, "start_time", 0))
        end_time = int(_get(task, "end_time", start_time + int(_get(task, "duration", 0))))

        fixed_scheduled_tasks.append(
            _make_scheduled_task(
                source=task,
                start_time=start_time,
                end_time=end_time,
                score=0.0,
            )
        )

    fixed_scheduled_tasks.sort(
        key=lambda task: _get(_get(task, "time_window"), "start_time", 0)
    )
    return fixed_scheduled_tasks


def _make_scheduled_task(source: Any, start_time: int, end_time: int, score: float) -> Any:
    return ScheduledTask(
        name=str(_get(source, "name", "")),
        category=str(_get(source, "category", "other")),
        tag=str(_get(source, "tag", "")),
        time_window=TimeWindow(start_time=start_time, end_time=end_time),
        score=round(float(score), 2),
    )


def _make_unscheduled_task(task: Any, reason: str) -> Any:
    if UnscheduledTask is None:
        return {
            "name": str(_get(task, "name", "")),
            "reason": reason,
        }

    try:
        return UnscheduledTask(
            name=str(_get(task, "name", "")),
            reason=reason,
        )
    except TypeError:
        return {
            "name": str(_get(task, "name", "")),
            "reason": reason,
        }


def _make_day_schedule_output(
    date: int,
    total_score: float,
    scheduled_tasks: list[Any],
    unscheduled_tasks: list[Any],
) -> DayScheduleOutput:
    try:
        return DayScheduleOutput(
            date=date,
            total_score=total_score,
            scheduled_tasks=scheduled_tasks,
            unscheduled_tasks=unscheduled_tasks,
        )
    except TypeError:
        # Backward compatibility if your DayScheduleOutput model does not
        # have `unscheduled_tasks` yet.
        return DayScheduleOutput(
            date=date,
            total_score=total_score,
            scheduled_tasks=scheduled_tasks,
        )


def _neighbors_for_candidate(
    start_time: int,
    end_time: int,
    scheduled_tasks: list[Any],
) -> tuple[Any | None, Any | None]:
    previous_task = None
    next_task = None

    for task in sorted(
        scheduled_tasks,
        key=lambda item: _get(_get(item, "time_window"), "start_time", 0),
    ):
        task_window = _get(task, "time_window", None)
        task_start = int(_get(task_window, "start_time", 0))
        task_end = int(_get(task_window, "end_time", 0))

        if task_end <= start_time:
            previous_task = task
        elif task_start >= end_time and next_task is None:
            next_task = task
            break

    return previous_task, next_task


def _overlaps_any(start_time: int, end_time: int, scheduled_tasks: list[Any]) -> bool:
    for task in scheduled_tasks:
        task_window = _get(task, "time_window", None)
        other_start = int(_get(task_window, "start_time", 0))
        other_end = int(_get(task_window, "end_time", 0))

        if start_time < other_end and other_start < end_time:
            return True

    return False


def _normalize_dependencies(dependencies: Any) -> list[str]:
    if dependencies is None:
        return []

    if isinstance(dependencies, str):
        if not dependencies.strip():
            return []

        # Supports "A;B", "A,B", or "A-B".
        separators = [";", ",", "-"]
        result = [dependencies]

        for separator in separators:
            if separator in dependencies:
                result = dependencies.split(separator)
                break

        return [item.strip() for item in result if item.strip()]

    if isinstance(dependencies, list):
        return [str(item).strip() for item in dependencies if str(item).strip()]

    return []


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default

    if isinstance(obj, dict):
        return obj.get(key, default)

    return getattr(obj, key, default)
