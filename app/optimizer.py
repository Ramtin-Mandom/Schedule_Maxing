"""
optimizer.py

Greedy Optimizer v1: the frozen baseline scheduling algorithm. This is the
protected implementation for Milestone 0 -- behavior here should not change
except for the narrowly-scoped fixes documented below. This module also
hosts the canonical day engine added by Task 4 (see "Canonical day engine"
below) -- evolving this module in place, per that task's instruction, rather
than building a separate optimizer stack.

Important behavior (Greedy Optimizer v1, legacy):
    - Fixed blocks are validated (start < end, inside the day window, no
      overlaps with each other) via app.constraints.validate_fixed_blocks
      before scheduling begins; an invalid fixed block raises ValueError.
    - Fixed tasks are placed first.
    - Movable tasks are placed in the valid slot with the highest reward score.
    - Existing dependencies are respected.
    - Missing dependencies are ignored, as requested.
    - Reward weights/preferences come from reward.load_reward_settings(), which
      reads config/task_preference.yaml by default (see reward.py's module
      docstring for the full discovery precedence) unless an explicit
      config_path is passed.

combine_fixed_and_optimized_scheduled_tasks and optimize_day_schedule remain
exactly as before -- temporary compatibility adapters that the still-legacy
CLI (app/main.py) and desktop UI (app/app.py) keep calling unchanged.

Task 6 / Schedule Maxing v2 replaced *how* both this module's placement
search (_best_candidate_for_task) and the canonical engine's
(_best_candidate_for_canonical_task) find a task's best feasible placement:
neither scores every feasible minute anymore. See the "Shared event-search
machinery" section below for the analytic candidate derivation and its
rounding-aware tie-breaking, and each function's own
*_exhaustive counterpart (test/benchmark-only) for the independent oracle
that validates it. Greedy Optimizer v1's documented 30-minute lattice and
scoring behavior, and the canonical engine's documented precise_greedy/
adhd_friendly behavior, are unchanged by this -- only the search that finds
a placement within those rules is faster.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from app.constraints import validate_fixed_blocks
from app.models import DayScheduleOutput, ScheduledTask, TimeWindow

try:
    from app.models import UnscheduledTask
except ImportError:
    UnscheduledTask = None  # type: ignore

from app.pert import assert_pert_constraints, compute_required_closure, get_dependency_end_time, has_cycle_by_id
from app.planning.models import DaySchedule as CanonicalDaySchedule
from app.planning.models import DayScheduleOutput as CanonicalDayScheduleOutput
from app.planning.models import FixedBlock as CanonicalFixedBlock
from app.planning.models import ScheduledTask as CanonicalScheduledTask
from app.planning.models import Task as CanonicalTask
from app.planning.models import TaskRegistry, UnscheduledEntry, UnscheduledReasonCode, compute_total_score
from app.planning.preferences import DayPreferences, OptimizerMode, day_preferences_to_reward_settings, to_legacy_scoring_task
from app.planning.time import MINUTES_PER_DAY
from app.reward import RewardSettings, _preferred_window_for_task, calculate_task_score, load_reward_settings


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

    if not validate_fixed_blocks(day_schedule):
        raise ValueError(
            "Invalid fixed blocks: each fixed block must have start_time < end_time, "
            "fit inside the day window, and not overlap any other fixed block."
        )

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


def _best_candidate_for_task_exhaustive(
    task: Any,
    scheduled_tasks: list[Any],
    day_start: int,
    day_end: int,
    earliest_start: int,
    settings: RewardSettings,
) -> tuple[int, int, float] | None:
    """
    Exhaustive reference for Greedy Optimizer v1: probes every 30-minute
    lattice point _best_candidate_for_task itself would ever generate
    (identical snapping/skip-if-occupied logic), scored with the real,
    unmodified calculate_task_score.

    Test/benchmark-only: optimize_day_schedule never calls this (see
    _best_candidate_for_task, its production replacement, and
    tests/test_optimizer_differential.py's spy test asserting so). Kept
    deliberately independent of the shared event-search machinery -- see
    that module section's docstring -- so it cannot share a bug with the
    code it exists to validate.
    """
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


def _placed_from_legacy_scheduled_tasks(scheduled_tasks: list[Any]) -> list["_Placed"]:
    """
    Adapts Greedy Optimizer v1's legacy scheduled_tasks (fixed blocks and
    already-placed movable tasks, each duck-typed with
    .time_window.start_time/end_time/.category/.tag) into the same _Placed
    shape the canonical engine's free-interval/event-search machinery uses
    (see _Placed and _best_start_in_free_interval), so both call sites share
    that machinery instead of maintaining two implementations.
    """
    placed: list[_Placed] = []
    for item in scheduled_tasks:
        window = _get(item, "time_window", None)
        placed.append(
            _Placed(
                start=int(_get(window, "start_time", 0)),
                end=int(_get(window, "end_time", 0)),
                name=str(_get(item, "name", "")),
                category=str(_get(item, "category", "other")),
                tag=str(_get(item, "tag", "")),
                task_id=None,
                score=float(_get(item, "score", 0.0)),
                fixed=bool(_get(item, "fixed", False)),
            )
        )
    return placed


def _best_candidate_for_task(
    task: Any,
    scheduled_tasks: list[Any],
    day_start: int,
    day_end: int,
    earliest_start: int,
    settings: RewardSettings,
) -> tuple[int, int, float] | None:
    """
    Find this task's best-scoring valid 30-minute-lattice placement, via the
    shared free-interval/event-search machinery (_best_start_in_free_interval)
    instead of scoring every lattice point while skipping occupied ones
    inline. Greedy Optimizer v1's own 30-minute lattice, its snapping
    behavior, and its "no day_start/day_end passed to calculate_task_score"
    contract (short_gap_bonus/adhd_mode are never active here) are
    unchanged -- see _best_candidate_for_task_exhaustive, this function's
    independent reference.
    """
    duration = int(_get(task, "duration", 0))
    if duration <= 0:
        return None

    first_start = max(day_start, earliest_start)
    latest_start = day_end - duration
    if latest_start < first_start:
        return None

    placed = _placed_from_legacy_scheduled_tasks(scheduled_tasks)
    ordered = sorted(placed, key=lambda item: item.start)

    best: tuple[int, int, float] | None = None
    best_score = float("-inf")

    cursor = day_start
    previous_item: _Placed | None = None

    for item in [*ordered, None]:
        interval_end = item.start if item is not None else day_end
        interval_start = cursor

        candidate = _best_start_in_free_interval(
            interval_start=interval_start,
            interval_end=interval_end,
            duration=duration,
            first_start=first_start,
            latest_start=latest_start,
            previous_item=previous_item,
            next_item=item,
            scoring_task=task,
            settings=settings,
            adhd_mode=False,
            day_start=None,
            day_end=None,
            lattice_step=TIME_SLOT_MINUTES,
            lattice_anchor=0,
        )
        if candidate is not None and candidate[2] > best_score:
            best_score = candidate[2]
            best = candidate

        if item is not None:
            cursor = max(cursor, item.end)
            previous_item = item

    return best


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


# =============================================================================
# Canonical day engine (Task 4 / Schedule Maxing v2)
# =============================================================================
#
# generate_day_schedule is the new public entry point: it accepts a canonical
# DaySchedule (app.planning.models) and a resolved DayPreferences
# (app.planning.preferences), and returns a canonical DayScheduleOutput.
# Greedy Optimizer v1 above is untouched and remains the CLI/UI's compatibility
# path until Task 6 rewires them.
#
# Internally, everything is scheduled in integer minutes relative to the
# day's own local window start (offset 0 == day_window's start instant),
# converted back to aware UTC datetimes only when building the final
# ScheduledTask placements. This lets candidate generation and scoring reuse
# the same integer-minute machinery as Greedy Optimizer v1 (via the
# app.planning.preferences adapter) while the public contract stays fully
# canonical (real dates, aware instants, stable UUID identity).
#
# Mode-specific candidate generation (see _candidate_starts):
#   precise_greedy  -- one-minute resolution, no snapping.
#   adhd_friendly   -- tasks > ADHD_SHORT_TASK_THRESHOLD_MINUTES start only on
#                       local wall-clock quarter-hour boundaries; tasks at or
#                       under that threshold may start on any valid minute,
#                       exactly like precise_greedy. Durations are never
#                       rounded or split in either mode.
#
# Mandatory scheduling (see generate_day_schedule): fixed blocks, then every
# required task plus the full transitive closure of its dependencies
# (whether or not those prerequisites are themselves required) as one
# "essential" tier, then optional tasks. A successful return always contains
# every essential task_id exactly once; any other outcome raises
# MandatoryTaskSchedulingError with structured, per-task reasons -- this
# function never returns an apparently-successful partial schedule that is
# silently missing a required task.

ADHD_SHORT_TASK_THRESHOLD_MINUTES = 30
ADHD_QUARTER_HOUR_MINUTES = 15


class MandatoryTaskSchedulingError(ValueError):
    """
    Raised when generate_day_schedule cannot place every required task (and
    its full prerequisite closure) exactly once.

    `failures` gives one MandatoryTaskFailure per affected task_id. This is
    deliberately a ValueError subclass (matching Greedy Optimizer v1's own
    fixed-block-validation errors) so existing `except ValueError` callers
    still catch it, while `.failures` gives structured detail a caller that
    wants it can inspect.

    Distinct from a plain dependency-cycle ValueError (raised earlier, before
    any placement search begins -- see generate_day_schedule) and from an
    ordinary *optional*-task UnscheduledEntry (reported, not raised).
    """

    def __init__(self, failures: list["MandatoryTaskFailure"]) -> None:
        self.failures = failures
        summary = "; ".join(
            f"{failure.task_id} [{failure.reason_code.value}]: {failure.explanation}" for failure in failures
        )
        super().__init__(f"Could not schedule every required task: {summary}")


@dataclass(frozen=True)
class MandatoryTaskFailure:
    """One required (or prerequisite-of-required) task that could not be placed."""

    task_id: uuid.UUID
    reason_code: UnscheduledReasonCode
    explanation: str
    #: True only when a coarse, aggregate/individual capacity bound *proves*
    #: no placement of this task could ever succeed on this day, regardless
    #: of search order (e.g. its own duration exceeds every free interval,
    #: or required-task durations alone exceed total free capacity). False
    #: means this specific greedy run failed to place it -- a different
    #: task ordering or a smarter (non-greedy) search might still succeed;
    #: this is not an exact solver and never claims otherwise.
    proven_infeasible: bool


class _Placed:
    """One already-placed interval (fixed block or scheduled flexible task)
    inside the canonical engine's search, in day-relative integer minutes.
    Exposes the same `.time_window.start_time/end_time`-shaped attributes
    Greedy Optimizer v1's helpers use, plus canonical identity/score."""

    __slots__ = ("start", "end", "time_window", "name", "category", "tag", "task_id", "score", "fixed")

    def __init__(
        self,
        *,
        start: int,
        end: int,
        name: str,
        category: str,
        tag: str,
        task_id: uuid.UUID | None,
        score: float,
        fixed: bool,
    ) -> None:
        self.start = start
        self.end = end
        self.time_window = SimpleNamespace(start_time=start, end_time=end)
        self.name = name
        self.category = category
        self.tag = tag
        self.task_id = task_id
        self.score = score
        self.fixed = fixed


def _to_offset(instant: datetime, day_start_utc: datetime) -> int:
    """
    Convert an aware instant to day-relative integer minutes (0 ==
    day_start_utc). Every public model feeding this (FixedBlock.planned_start/
    planned_end, Task.deadline, an external dependency's completion instant)
    is a plain aware datetime with no enforced whole-minute precision, but
    this engine's entire search operates in integer minutes -- silently
    round()-ing an instant with nonzero seconds/microseconds (nearest-minute,
    banker's rounding) could move a fixed block's reported boundary, permit
    an overlap that the sub-minute reality does not have, or let a dependent
    task start before its dependency instant / a deadline is actually met.
    Reject sub-minute precision explicitly instead of rounding it away.
    """
    delta = instant - day_start_utc
    if delta.seconds % 60 != 0 or delta.microseconds != 0:
        raise ValueError(
            f"{instant.isoformat()} is not aligned to a whole minute relative to the day "
            f"window start ({day_start_utc.isoformat()}); this engine only supports "
            "minute-precision instants for fixed blocks, deadlines, and external "
            "dependency completion times."
        )
    return delta // timedelta(minutes=1)


def _from_offset(offset: int, day_start_utc: datetime) -> datetime:
    return day_start_utc + timedelta(minutes=offset)


def _validate_and_place_canonical_fixed_blocks(
    fixed_blocks: list[CanonicalFixedBlock],
    day_start_utc: datetime,
    day_end_utc: datetime,
) -> list[_Placed]:
    day_total_minutes = _to_offset(day_end_utc, day_start_utc)
    placed: list[_Placed] = []

    for block in fixed_blocks:
        start = _to_offset(block.planned_start, day_start_utc)
        end = _to_offset(block.planned_end, day_start_utc)

        if end <= start:
            raise ValueError(f"Invalid fixed block {block.id}: planned_end must be after planned_start.")
        if start < 0 or end > day_total_minutes:
            raise ValueError(
                f"Invalid fixed block {block.id} ({block.label!r}): outside the day window "
                f"[{day_start_utc.isoformat()}, {day_end_utc.isoformat()}]."
            )

        placed.append(
            _Placed(start=start, end=end, name=block.label, category="fixed", tag="", task_id=None, score=0.0, fixed=True)
        )

    placed.sort(key=lambda item: item.start)
    for earlier, later in zip(placed, placed[1:]):
        if earlier.end > later.start:
            raise ValueError("Invalid fixed blocks: overlapping intervals.")

    return placed


def _normalize_preferred_window(
    preference_time: dict[str, int] | None,
    day_window_start_minute: int,
) -> dict[str, int] | None:
    """
    Convert a preferred-time window expressed in local minutes-from-midnight
    (as produced by app.planning.preferences.to_legacy_scoring_task /
    effective_task_preferred_window -- see LocalTimeWindow's own docstring)
    into day-window-relative offset minutes, i.e. the same coordinate system
    every candidate start/end, neighbor, and day bound already uses inside
    this engine's search (offset 0 == day_window_start_minute; see _to_offset
    and the module docstring). Without this conversion, a preferred window
    is compared against day-relative offsets as if it were already in that
    coordinate system -- silently correct only when the day happens to start
    at local midnight (day_window_start_minute == 0), and wrong by exactly
    that offset otherwise (e.g. an 08:00-18:00 day scoring a 09:00-10:00
    preference against offset 0-600 instead of 60-120).

    Preserves the window's exact duration by shifting both endpoints by the
    same resolved offset, rather than independently wrapping each endpoint
    modulo a day -- so a window is never silently lengthened/shortened by
    the conversion, even when it extends past the day's own end.
    """
    if preference_time is None:
        return None
    local_start = int(preference_time["start_time"])
    local_end = int(preference_time["end_time"])
    offset_start = (local_start - day_window_start_minute) % MINUTES_PER_DAY
    return {"start_time": offset_start, "end_time": offset_start + (local_end - local_start)}


def _next_quarter_hour_offset(first_start: int, day_window_start_minute: int) -> int:
    local_minute = (day_window_start_minute + first_start) % MINUTES_PER_DAY
    remainder = local_minute % ADHD_QUARTER_HOUR_MINUTES
    if remainder == 0:
        return first_start
    return first_start + (ADHD_QUARTER_HOUR_MINUTES - remainder)


def _candidate_starts(
    mode: OptimizerMode,
    duration: int,
    first_start: int,
    latest_start: int,
    day_window_start_minute: int,
) -> list[int]:
    """
    Every valid candidate start minute (day-relative offset) for one
    free-interval slice, per mode. precise_greedy (and any adhd_friendly
    task at or under ADHD_SHORT_TASK_THRESHOLD_MINUTES) uses one-minute
    resolution with no snapping; a longer adhd_friendly task is restricted
    to local wall-clock quarter-hour boundaries.
    """
    if latest_start < first_start:
        return []

    if mode == OptimizerMode.PRECISE_GREEDY or duration <= ADHD_SHORT_TASK_THRESHOLD_MINUTES:
        return list(range(first_start, latest_start + 1))

    start = _next_quarter_hour_offset(first_start, day_window_start_minute)
    return list(range(start, latest_start + 1, ADHD_QUARTER_HOUR_MINUTES))


# =============================================================================
# Shared event-search machinery (Task 6 / Schedule Maxing v2)
# =============================================================================
#
# _best_start_in_free_interval replaces "score every feasible minute" with a
# small, analytically-derived candidate set that is provably guaranteed to
# contain the true best-scoring, earliest-tied placement under the real
# (rounded) app.reward.calculate_task_score -- see _event_breakpoints'
# docstring for exactly which reward components drive which breakpoints, and
# _earliest_best_in_continuous_range for how rounding-aware ties are
# resolved without scanning every minute. Both the canonical engine
# (_best_candidate_for_canonical_task) and Greedy Optimizer v1
# (_best_candidate_for_task) share this machinery; only their lattice
# (precise/1-minute vs 30-minute) and neighbor-adapter plumbing differ.
#
# The _exhaustive counterparts kept alongside each production function are
# deliberately independent of this machinery (see their own docstrings) --
# they exist purely so tests/benchmarks have an oracle that cannot share a
# bug with the code it is meant to validate.


def _next_lattice_offset(first_start: int, step: int, anchor_local_minute: int) -> int:
    """
    Generalizes _next_quarter_hour_offset to an arbitrary grid step and
    anchor (the local wall-clock minute that offset 0 corresponds to):
    the smallest offset >= first_start that lands on the step-minute grid
    relative to that anchor. Legacy's 30-minute grid anchors at 0 (absolute
    minute 0, no wall-clock concept -- matches Greedy Optimizer v1's
    existing snapping exactly, since 1440 is an exact multiple of every
    supported step so the extra day-wraparound this performs is a no-op for
    legacy's already-unwrapped minute values). adhd_friendly's quarter-hour
    grid anchors at day_window_start_minute (local midnight-relative), via
    _next_quarter_hour_offset itself.
    """
    local_minute = (anchor_local_minute + first_start) % MINUTES_PER_DAY
    remainder = local_minute % step
    if remainder == 0:
        return first_start
    return first_start + (step - remainder)


def _lattice_points(lo: int, hi: int, step: int, anchor_local_minute: int) -> list[int]:
    """Every grid-eligible integer start in [lo, hi] (inclusive), per
    _next_lattice_offset. Empty if the first eligible point already exceeds
    hi. Always small: at most MINUTES_PER_DAY // step points even for a
    full, unconstrained day."""
    first = _next_lattice_offset(lo, step, anchor_local_minute)
    if first > hi:
        return []
    return list(range(first, hi + 1, step))


def _short_gap_active_zone(
    edge: int | None,
    min_gap: int,
    duration: int,
    *,
    before: bool,
) -> tuple[int, int] | None:
    """
    The inclusive integer start_time range on one side (before/after) where
    _short_gap_bonus_score's pre_gap for that side lies in
    [0, min_gap_between_tasks_minutes) -- i.e. where that side's own
    contribution is actively sloped rather than pinned at 0. `edge` is the
    real neighbor's end (before side) / start (after side), or the day
    boundary when that side has no neighbor (matching
    _short_gap_bonus_score's own fallback) -- None means neither applies, so
    there is no active zone on this side at all.
    """
    if edge is None or min_gap <= 0:
        return None
    if before:
        return (edge, edge + min_gap - 1)
    return (edge - duration - min_gap + 1, edge - duration)


def _event_breakpoints(
    *,
    duration: int,
    clipped_start: int,
    clipped_latest: int,
    previous_item: "_Placed | None",
    next_item: "_Placed | None",
    preferred_window: dict[str, int] | None,
    settings: RewardSettings,
    adhd_mode: bool,
    day_start: int | None,
    day_end: int | None,
) -> tuple[list[int], list[tuple[int, int]]]:
    """
    Analytic candidate-start derivation for one free-interval slice.

    Returns (cuts, active_zones):
      cuts -- every integer start minute (clipped to
        [clipped_start, clipped_latest], which are themselves always
        included) where some reward component's closed form could change.
        Between any two consecutive cuts, every component below is either
        an exact constant or an exact affine (straight-line) function of
        start_time, so their *sum* is affine there too -- see
        _earliest_best_in_continuous_range for why that is exactly what
        makes the rounded total provably monotonic across that range.
      active_zones -- small (at most min_gap_between_tasks_minutes-wide)
        integer ranges where the ADHD short-gap bonus's own internal
        round(x, 2) (applied before calculate_task_score's final rounding)
        can make the combined score locally non-monotonic even though every
        other component is well-behaved there; every point in a zone is
        evaluated directly instead of being assumed monotonic.

    Reward components and their start-time dependence (see app/reward.py --
    this is the "document every active component" accounting Task 6 asks
    for; weight_category_bonus is inactive in _priority_score and
    contributes no scoring dependence at all, so it needs no breakpoint):
      - priority_score (_priority_score): constant in start_time --
        contributes no breakpoint.
      - time_score (_time_preference_score): a full-bonus constant while
        [preferred_start, preferred_end-duration] fully contains the
        placement (still well-defined, just an always-false condition, when
        the task is longer than its preferred window), else a decay that is
        affine in start_time on each side of its center (the
        |scheduled_center-preferred_center| kink) and clamped to 0 past
        +-max_time_distance_minutes -- breakpoints at preferred_start,
        preferred_end-duration, the center, and the two decay
        zero-crossings.
      - relation_score (_relation_score), including its same-category
        half-weight branch (gated by the same gap check, so it needs no
        separate breakpoint): a step function of each real neighbor's gap,
        constant on each side of gap == same_tag_window_minutes (an
        inclusive maximum per _relation_score) -- one breakpoint per real
        neighbor.
      - fragmentation_score (_fragmentation_score): a step function of each
        real neighbor's gap, constant outside (0, min_gap_between_tasks_minutes)
        and a fixed penalty strictly inside it -- two breakpoints per real
        neighbor (gap==0, gap==min_gap). Never synthesized from day bounds
        (matches _fragmentation_score's own "only actual neighbors"
        behavior).
      - short_gap_bonus (_short_gap_bonus_score, adhd_mode only): affine in
        pre_gap on each side while 0<=pre_gap<min_gap, using the real
        neighbor's edge or the day boundary when that side has no neighbor
        (the one place a day bound is a legitimate synthetic "neighbor",
        matching _short_gap_bonus_score itself), 0 elsewhere -- its active
        range on each side is returned as `active_zones` rather than being
        trusted to keep a region monotonic, because of its own internal
        rounding (see the cap/round in _short_gap_bonus_score).
    """
    cuts: set[int] = {clipped_start, clipped_latest}

    def add(value: float) -> None:
        lo = math.floor(value)
        hi = math.ceil(value)
        if lo == hi:
            cuts.update((lo - 1, lo, lo + 1))
        else:
            cuts.update((lo, hi))

    if preferred_window is not None:
        preferred_start = preferred_window["start_time"]
        preferred_end = preferred_window["end_time"]
        add(preferred_start)
        add(preferred_end - duration)
        center = (preferred_start + preferred_end - duration) / 2
        add(center)
        add(center - settings.max_time_distance_minutes)
        add(center + settings.max_time_distance_minutes)

    if previous_item is not None:
        add(previous_item.end + settings.same_tag_window_minutes)
        add(previous_item.end)
        add(previous_item.end + settings.min_gap_between_tasks_minutes)

    if next_item is not None:
        add(next_item.start - duration - settings.same_tag_window_minutes)
        add(next_item.start - duration)
        add(next_item.start - duration - settings.min_gap_between_tasks_minutes)

    active_zones: list[tuple[int, int]] = []
    short_gap_active = (
        adhd_mode
        and settings.short_gap_bonus_weight > 0
        and settings.min_gap_between_tasks_minutes > 0
        and duration <= settings.short_gap_bonus_max_minutes
    )
    if short_gap_active:
        min_gap = settings.min_gap_between_tasks_minutes
        before_edge = previous_item.end if previous_item is not None else day_start
        after_edge = next_item.start if next_item is not None else day_end

        for zone in (
            _short_gap_active_zone(before_edge, min_gap, duration, before=True),
            _short_gap_active_zone(after_edge, min_gap, duration, before=False),
        ):
            if zone is not None:
                active_zones.append(zone)
                add(zone[0])
                add(zone[1])

    clipped_cuts = sorted(c for c in cuts if clipped_start <= c <= clipped_latest)

    clipped_zones: list[tuple[int, int]] = []
    for zone_lo, zone_hi in active_zones:
        zone_lo = max(zone_lo, clipped_start)
        zone_hi = min(zone_hi, clipped_latest)
        if zone_lo <= zone_hi:
            clipped_zones.append((zone_lo, zone_hi))

    return clipped_cuts, clipped_zones


def _earliest_best_in_continuous_range(scorer: Callable[[int], float], lo: int, hi: int) -> tuple[int, float]:
    """
    lo..hi is a range in which round(raw_score(start_time), 2) is provably
    monotonic (non-decreasing or non-increasing) in start_time -- see
    _event_breakpoints' docstring for why every component is constant or
    affine there, hence their sum is affine, hence rounding it stays
    monotonic (round is a monotonic non-decreasing function of its input,
    and composing a monotonic function with a monotonic function preserves
    monotonicity). Returns the earliest start in [lo, hi] achieving this
    range's own maximum score, using O(log(hi-lo)) calls to `scorer` (the
    real calculate_task_score) instead of scoring every minute -- this is
    the "bounded interval-search method" used instead of a
    critical-point-only argument, so a long rounded-score plateau (e.g. from
    a very small slope) still resolves to its earliest minute exactly.
    """
    score_lo = scorer(lo)
    if hi == lo:
        return lo, score_lo

    score_hi = scorer(hi)
    if score_lo >= score_hi:
        # Non-increasing (or tied at both ends, which -- by the monotonic
        # squeeze argument above -- means constant throughout): lo is
        # already the earliest maximum.
        return lo, score_lo

    # Strictly ascending overall: binary-search the earliest start whose
    # score already equals the range's maximum (score_hi), i.e. the start
    # of the top rounded-score plateau.
    target = score_hi
    left, right = lo, hi
    while left < right:
        mid = (left + right) // 2
        if scorer(mid) >= target:
            right = mid
        else:
            left = mid + 1
    return left, target


def _earliest_best_among(scorer: Callable[[int], float], points: list[int]) -> tuple[int, float]:
    """
    Earliest point achieving the maximum score among an explicit, already-
    small list of candidate starts (lattice-restricted searches, and
    active-zone points where monotonicity is not assumed) -- a direct,
    real-scorer evaluation of every point, since these lists are bounded by
    construction (a grid step or min_gap_between_tasks_minutes), not by the
    free interval's length.
    """
    best_point = points[0]
    best_score = scorer(best_point)
    for point in points[1:]:
        score = scorer(point)
        if score > best_score:
            best_point, best_score = point, score
    return best_point, best_score


def _best_start_in_free_interval(
    *,
    interval_start: int,
    interval_end: int,
    duration: int,
    first_start: int,
    latest_start: int,
    previous_item: "_Placed | None",
    next_item: "_Placed | None",
    scoring_task: dict,
    settings: RewardSettings,
    adhd_mode: bool,
    day_start: int | None,
    day_end: int | None,
    lattice_step: int | None,
    lattice_anchor: int,
) -> tuple[int, int, float] | None:
    """
    This free interval's best-scoring valid placement (or None if the task
    does not fit here at all), via the event/lattice candidate set instead
    of scoring every feasible minute. lattice_step=None means unrestricted
    (precise) 1-minute resolution; otherwise every candidate is restricted
    to that grid (see _lattice_points), and the reduction this function
    otherwise performs is skipped -- a grid step already bounds the
    candidate count to at most MINUTES_PER_DAY // lattice_step, so the
    breakpoint/monotonic-range machinery below is only used for the
    genuinely large (up to 1440-point) unrestricted search space.
    """
    clipped_start = max(first_start, interval_start)
    clipped_latest = min(latest_start, interval_end - duration)
    if clipped_latest < clipped_start:
        return None

    def scorer(start: int) -> float:
        return calculate_task_score(
            scoring_task,
            start,
            previous_task=previous_item,
            next_task=next_item,
            settings=settings,
            adhd_mode=adhd_mode,
            day_start=day_start,
            day_end=day_end,
        )

    if lattice_step is not None:
        points = _lattice_points(clipped_start, clipped_latest, lattice_step, lattice_anchor)
        if not points:
            return None
        start, score = _earliest_best_among(scorer, points)
        return start, start + duration, score

    preferred_window = _preferred_window_for_task(scoring_task, settings)
    cuts, active_zones = _event_breakpoints(
        duration=duration,
        clipped_start=clipped_start,
        clipped_latest=clipped_latest,
        previous_item=previous_item,
        next_item=next_item,
        preferred_window=preferred_window,
        settings=settings,
        adhd_mode=adhd_mode,
        day_start=day_start,
        day_end=day_end,
    )

    def in_any_zone(range_lo: int, range_hi: int) -> bool:
        return any(range_lo <= zone_hi and zone_lo <= range_hi for zone_lo, zone_hi in active_zones)

    best_start: int | None = None
    best_score = float("-inf")

    def consider(start: int, score: float) -> None:
        nonlocal best_start, best_score
        if score > best_score:
            best_start, best_score = start, score

    if len(cuts) == 1:
        only = cuts[0]
        consider(only, scorer(only))
    else:
        for range_lo, range_hi in zip(cuts, cuts[1:]):
            if in_any_zone(range_lo, range_hi):
                for start in range(range_lo, range_hi + 1):
                    consider(start, scorer(start))
            else:
                start, score = _earliest_best_in_continuous_range(scorer, range_lo, range_hi)
                consider(start, score)

    assert best_start is not None
    return best_start, best_start + duration, best_score


def _prepare_canonical_task_search(
    task: CanonicalTask,
    day_total_minutes: int,
    earliest_start: int,
    mode: OptimizerMode,
    day_window_start_minute: int,
    day_preferences: DayPreferences,
    deadline_offset: int | None,
) -> tuple[int, int, int, dict, bool] | None:
    """
    Shared setup for both the production event search and the exhaustive
    reference: resolves duration/first_start/latest_start, builds the
    coordinate-normalized scoring_task dict (fix A), and the adhd_mode flag.
    Returns None immediately when the feasible range is already empty.
    """
    duration = task.estimated_duration_minutes
    first_start = max(0, earliest_start)
    latest_start = day_total_minutes - duration
    if deadline_offset is not None:
        latest_start = min(latest_start, deadline_offset - duration)
    if latest_start < first_start:
        return None

    scoring_task = to_legacy_scoring_task(task, day_preferences)
    scoring_task["preference_time"] = _normalize_preferred_window(
        scoring_task["preference_time"], day_window_start_minute
    )
    adhd_mode = mode == OptimizerMode.ADHD_FRIENDLY
    return duration, first_start, latest_start, scoring_task, adhd_mode


def _best_candidate_for_canonical_task_exhaustive(
    task: CanonicalTask,
    placed: list[_Placed],
    day_total_minutes: int,
    earliest_start: int,
    mode: OptimizerMode,
    day_window_start_minute: int,
    settings: RewardSettings,
    day_preferences: DayPreferences,
    deadline_offset: int | None,
) -> tuple[int, int, float] | None:
    """
    Exhaustive reference for the canonical engine: probes every candidate
    _candidate_starts itself would ever generate (every integer minute in
    precise_greedy, and every mode-eligible minute -- 1-minute for short
    adhd_friendly tasks, quarter-hour lattice for longer ones -- in
    adhd_friendly), scored with the real, unmodified calculate_task_score.

    Test/benchmark-only: never called by generate_day_schedule (see
    _best_candidate_for_canonical_task, its production replacement, and
    tests/test_optimizer_differential.py's spy test asserting so). Kept
    deliberately independent of the event-search machinery below -- it must
    not share bugs with the code it is meant to validate.
    """
    prepared = _prepare_canonical_task_search(
        task, day_total_minutes, earliest_start, mode, day_window_start_minute, day_preferences, deadline_offset
    )
    if prepared is None:
        return None
    duration, first_start, latest_start, scoring_task, adhd_mode = prepared

    ordered = sorted(placed, key=lambda item: item.start)

    best: tuple[int, int, float] | None = None
    best_score = float("-inf")

    cursor = 0
    previous_item: _Placed | None = None

    for item in [*ordered, None]:
        interval_end = item.start if item is not None else day_total_minutes
        interval_start = cursor

        if interval_end > interval_start:
            clipped_start = max(first_start, interval_start)
            clipped_latest = min(latest_start, interval_end - duration)

            if clipped_latest >= clipped_start:
                for start in _candidate_starts(mode, duration, clipped_start, clipped_latest, day_window_start_minute):
                    end = start + duration
                    score = calculate_task_score(
                        scoring_task,
                        start,
                        previous_task=previous_item,
                        next_task=item,
                        settings=settings,
                        adhd_mode=adhd_mode,
                        day_start=0,
                        day_end=day_total_minutes,
                    )
                    if score > best_score:
                        best_score = score
                        best = (start, end, score)

        if item is not None:
            cursor = max(cursor, item.end)
            previous_item = item

    return best


def _best_candidate_for_canonical_task(
    task: CanonicalTask,
    placed: list[_Placed],
    day_total_minutes: int,
    earliest_start: int,
    mode: OptimizerMode,
    day_window_start_minute: int,
    settings: RewardSettings,
    day_preferences: DayPreferences,
    deadline_offset: int | None,
) -> tuple[int, int, float] | None:
    """
    Find this task's best-scoring valid placement, pruned to free intervals
    only (never probing a minute that is already occupied) and to the
    dependency/deadline-bound start range -- see the module docstring and
    _best_start_in_free_interval for how candidate starts are derived
    without scoring every feasible minute.
    """
    prepared = _prepare_canonical_task_search(
        task, day_total_minutes, earliest_start, mode, day_window_start_minute, day_preferences, deadline_offset
    )
    if prepared is None:
        return None
    duration, first_start, latest_start, scoring_task, adhd_mode = prepared

    grid_constrained = mode == OptimizerMode.ADHD_FRIENDLY and duration > ADHD_SHORT_TASK_THRESHOLD_MINUTES
    lattice_step = ADHD_QUARTER_HOUR_MINUTES if grid_constrained else None

    ordered = sorted(placed, key=lambda item: item.start)

    best: tuple[int, int, float] | None = None
    best_score = float("-inf")

    cursor = 0
    previous_item: _Placed | None = None

    for item in [*ordered, None]:
        interval_end = item.start if item is not None else day_total_minutes
        interval_start = cursor

        candidate = _best_start_in_free_interval(
            interval_start=interval_start,
            interval_end=interval_end,
            duration=duration,
            first_start=first_start,
            latest_start=latest_start,
            previous_item=previous_item,
            next_item=item,
            scoring_task=scoring_task,
            settings=settings,
            adhd_mode=adhd_mode,
            day_start=0,
            day_end=day_total_minutes,
            lattice_step=lattice_step,
            lattice_anchor=day_window_start_minute,
        )
        if candidate is not None and candidate[2] > best_score:
            best_score = candidate[2]
            best = candidate

        if item is not None:
            cursor = max(cursor, item.end)
            previous_item = item

    return best


def _make_dependency_lookup(
    registry: dict[uuid.UUID, CanonicalTask],
    placed_by_task_id: dict[uuid.UUID, tuple[int, int]],
    external_dependency_end_offsets: dict[uuid.UUID, int],
):
    """
    Returns a function(task_id) -> int | None: the earliest start this
    task's dependencies allow (0 if none), or None if not yet ready.

    A dependency_id resolves, in order: already placed today (local) ->
    supplied external context (a known task_id outside today's registry,
    with its completion instant given by the caller) -> otherwise the task
    is BLOCKED indefinitely. This is the canonical counterpart of legacy's
    "missing dependency names are ignored": a canonical dependency_id is
    never silently ignored just because it is not present locally --
    ignoring only ever happens for legacy *name* references that generuinely
    do not exist (see app.planning.compat), not for a known but
    out-of-scope task_id.
    """

    def lookup(task_id: uuid.UUID) -> int | None:
        task = registry[task_id]
        if not task.dependency_ids:
            return 0

        latest = 0
        for dependency_id in task.dependency_ids:
            if dependency_id in placed_by_task_id:
                latest = max(latest, placed_by_task_id[dependency_id][1])
            elif dependency_id in external_dependency_end_offsets:
                latest = max(latest, external_dependency_end_offsets[dependency_id])
            else:
                # Either a real local dependency not yet scheduled, or an
                # unresolved/unknown external task_id -- either way, this
                # task must wait rather than proceed as if unblocked.
                return None

        return latest

    return lookup


def _run_greedy_tier(
    task_ids: list[uuid.UUID],
    registry: dict[uuid.UUID, CanonicalTask],
    placed: list[_Placed],
    placed_by_task_id: dict[uuid.UUID, tuple[int, int]],
    day_total_minutes: int,
    mode: OptimizerMode,
    day_window_start_minute: int,
    settings: RewardSettings,
    day_preferences: DayPreferences,
    lookup,
    deadline_offsets: dict[uuid.UUID, int],
) -> tuple[dict[uuid.UUID, tuple[int, int, float]], list[uuid.UUID]]:
    """
    Repeatedly place the best-scoring currently-feasible task from
    `task_ids` (mirrors Greedy Optimizer v1's own repeated-best-pick loop),
    mutating `placed`/`placed_by_task_id` in place so later tiers and
    dependency lookups see every placement made here. Returns
    (task_id -> (start, end, score) for every placed task, and the task_ids
    that could not be placed).
    """
    remaining = list(task_ids)
    results: dict[uuid.UUID, tuple[int, int, float]] = {}

    while remaining:
        best_task_id: uuid.UUID | None = None
        best_candidate: tuple[int, int, float] | None = None
        best_score = float("-inf")

        for task_id in remaining:
            earliest_start = lookup(task_id)
            if earliest_start is None:
                continue

            candidate = _best_candidate_for_canonical_task(
                registry[task_id],
                placed,
                day_total_minutes,
                earliest_start,
                mode,
                day_window_start_minute,
                settings,
                day_preferences,
                deadline_offsets.get(task_id),
            )
            if candidate is None:
                continue

            if candidate[2] > best_score:
                best_task_id = task_id
                best_candidate = candidate
                best_score = candidate[2]

        if best_task_id is None or best_candidate is None:
            break

        start, end, score = best_candidate
        task = registry[best_task_id]
        tag = task.tags[0] if task.tags else ""

        placed.append(
            _Placed(
                start=start, end=end, name=task.name, category=task.category, tag=tag,
                task_id=best_task_id, score=score, fixed=False,
            )
        )
        placed_by_task_id[best_task_id] = (start, end)
        results[best_task_id] = (start, end, score)
        remaining.remove(best_task_id)

    return results, remaining


def _mandatory_failure_reason(
    task_id: uuid.UUID,
    lookup,
    deadline_offsets: dict[uuid.UUID, int],
) -> tuple[UnscheduledReasonCode, str]:
    earliest = lookup(task_id)
    if earliest is None:
        return (
            UnscheduledReasonCode.DEPENDENCY_UNRESOLVED,
            "a required dependency was never scheduled, or references unresolved external context.",
        )
    if task_id in deadline_offsets:
        return (
            UnscheduledReasonCode.WINDOW_UNSUPPORTED,
            "no valid slot was found before this task's deadline given the current schedule.",
        )
    return (
        UnscheduledReasonCode.NO_VALID_SLOT,
        "no valid slot was found for this task given the current schedule and mode's grid.",
    )


def generate_day_schedule(
    day_schedule: CanonicalDaySchedule,
    preferences: DayPreferences,
    *,
    previous_result: CanonicalDayScheduleOutput | None = None,
    external_dependency_ends: dict[uuid.UUID, datetime] | None = None,
) -> CanonicalDayScheduleOutput:
    """
    Generate one day's canonical schedule.

    Order: validate/place fixed blocks, detect dependency cycles once,
    schedule every required task plus its full prerequisite closure
    ("essential" tier), then schedule optional tasks. Raises
    MandatoryTaskSchedulingError if any essential task cannot be placed
    (see that class and MandatoryTaskFailure). Optional tasks that cannot be
    placed are reported in the result's `unscheduled` list instead.

    external_dependency_ends: task_id -> aware instant, for a dependency
    known (from outside this call, e.g. a wider allocation) to have already
    been satisfied at that instant. A dependency_id that is neither in
    day_schedule's own task registry nor in this map blocks its dependent
    task rather than being silently ignored (see _make_dependency_lookup).

    previous_result: when supplied, an unchanged (same task_id, same
    planned_start, same planned_end) placement reuses its previous
    ScheduledTask.id; any other placement (new, or the same task at a
    different time) receives a brand-new id -- never rewriting an id an
    existing execution record might already reference.
    """
    if day_schedule.date != preferences.date:
        raise ValueError(
            f"day_schedule.date ({day_schedule.date}) does not match preferences.date ({preferences.date})."
        )
    if day_schedule.timezone != preferences.timezone:
        raise ValueError(
            f"day_schedule.timezone ({day_schedule.timezone!r}) does not match "
            f"preferences.timezone ({preferences.timezone!r})."
        )

    day_start_utc, day_end_utc = preferences.to_local_day_window().to_utc_instants()
    day_total_minutes = _to_offset(day_end_utc, day_start_utc)

    placed = _validate_and_place_canonical_fixed_blocks(day_schedule.fixed_blocks, day_start_utc, day_end_utc)
    placed_by_task_id: dict[uuid.UUID, tuple[int, int]] = {}

    registry: dict[uuid.UUID, CanonicalTask] = {
        task_id: day_schedule.tasks.get(task_id) for task_id in day_schedule.task_ids
    }

    # Detect cycles once, before any placement search begins (distinct from
    # -- and raised earlier than -- a MandatoryTaskSchedulingError).
    if has_cycle_by_id(registry):
        raise ValueError("Dependency cycle detected between canonical tasks.")

    external_dependency_end_offsets = {
        task_id: _to_offset(instant, day_start_utc) for task_id, instant in (external_dependency_ends or {}).items()
    }

    settings = day_preferences_to_reward_settings(preferences)
    mode = preferences.optimizer_mode
    day_window_start_minute = preferences.day_window.start_minute

    deadline_offsets: dict[uuid.UUID, int] = {
        task_id: _to_offset(task.deadline, day_start_utc) for task_id, task in registry.items() if task.deadline is not None
    }

    # A required task pinned (required_date) to a different date cannot be
    # scheduled today at all -- proven infeasible for *this* call. An
    # optional task pinned elsewhere is simply excluded from today's set,
    # not reported as unscheduled (it was never meant for today).
    movable_ids: list[uuid.UUID] = []
    mismatched_required: list[MandatoryTaskFailure] = []
    for task_id, task in registry.items():
        if task.required_date is not None and task.required_date != day_schedule.date:
            if task.required:
                mismatched_required.append(
                    MandatoryTaskFailure(
                        task_id=task_id,
                        reason_code=UnscheduledReasonCode.REQUIRED_DATE_CONFLICT,
                        explanation=(
                            f"required_date {task.required_date} does not match this day's date "
                            f"({day_schedule.date})."
                        ),
                        proven_infeasible=True,
                    )
                )
            continue
        movable_ids.append(task_id)

    if mismatched_required:
        raise MandatoryTaskSchedulingError(mismatched_required)

    essential_ids = compute_required_closure(movable_ids, registry)
    # compute_required_closure returns a set (no defined iteration order);
    # derive tier order from movable_ids (the original input order) instead
    # of iterating the set directly, so two equal-scoring essential tasks
    # tie-break in input order rather than UUID/set-hash order.
    essential_ordered = [task_id for task_id in movable_ids if task_id in essential_ids]
    optional_ids = [task_id for task_id in movable_ids if task_id not in essential_ids]

    # Coarse, provable infeasibility checks -- see MandatoryTaskFailure's
    # proven_infeasible field. Neither check is a full bin-packing proof
    # (this is not an exact solver); both are sound necessary conditions.
    initial_free_intervals = _free_intervals(placed, day_total_minutes)
    largest_free_interval = max((end - start for start, end in initial_free_intervals), default=0)

    individually_impossible = [
        MandatoryTaskFailure(
            task_id=task_id,
            reason_code=UnscheduledReasonCode.NO_VALID_SLOT,
            explanation=(
                f"this task's duration ({registry[task_id].estimated_duration_minutes} min) exceeds the "
                f"largest single free interval on this day ({largest_free_interval} min), even ignoring "
                "every other movable task."
            ),
            proven_infeasible=True,
        )
        for task_id in essential_ids
        if registry[task_id].estimated_duration_minutes > largest_free_interval
    ]
    if individually_impossible:
        raise MandatoryTaskSchedulingError(individually_impossible)

    free_capacity = day_total_minutes - sum(item.end - item.start for item in placed)
    essential_total_duration = sum(registry[task_id].estimated_duration_minutes for task_id in essential_ids)
    if essential_total_duration > free_capacity:
        raise MandatoryTaskSchedulingError(
            [
                MandatoryTaskFailure(
                    task_id=task_id,
                    reason_code=UnscheduledReasonCode.NO_VALID_SLOT,
                    explanation=(
                        f"required tasks (including prerequisite closure) need {essential_total_duration} "
                        f"minutes in total, but only {free_capacity} minutes of free capacity remain after "
                        "fixed blocks."
                    ),
                    proven_infeasible=True,
                )
                for task_id in essential_ids
            ]
        )

    lookup = _make_dependency_lookup(registry, placed_by_task_id, external_dependency_end_offsets)

    essential_results, essential_remaining = _run_greedy_tier(
        essential_ordered, registry, placed, placed_by_task_id, day_total_minutes,
        mode, day_window_start_minute, settings, preferences, lookup, deadline_offsets,
    )

    if essential_remaining:
        raise MandatoryTaskSchedulingError(
            [
                MandatoryTaskFailure(task_id=task_id, reason_code=reason_code, explanation=explanation, proven_infeasible=False)
                for task_id in essential_remaining
                for reason_code, explanation in [_mandatory_failure_reason(task_id, lookup, deadline_offsets)]
            ]
        )

    optional_results, optional_remaining = _run_greedy_tier(
        optional_ids, registry, placed, placed_by_task_id, day_total_minutes,
        mode, day_window_start_minute, settings, preferences, lookup, deadline_offsets,
    )

    unscheduled_entries = []
    for task_id in optional_remaining:
        reason_code, explanation = _mandatory_failure_reason(task_id, lookup, deadline_offsets)
        unscheduled_entries.append(UnscheduledEntry(task_id=task_id, reason_code=reason_code, explanation=explanation))

    previous_placement_ids: dict[tuple[uuid.UUID, datetime, datetime], uuid.UUID] = {}
    if previous_result is not None:
        for placement in previous_result.placements:
            previous_placement_ids[(placement.task_id, placement.planned_start, placement.planned_end)] = placement.id

    all_results = {**essential_results, **optional_results}
    placements: list[CanonicalScheduledTask] = []
    for task_id, (start, end, score) in all_results.items():
        planned_start = _from_offset(start, day_start_utc)
        planned_end = _from_offset(end, day_start_utc)
        reused_id = previous_placement_ids.get((task_id, planned_start, planned_end))

        placements.append(
            CanonicalScheduledTask(
                id=reused_id if reused_id is not None else uuid.uuid4(),
                task_id=task_id,
                planned_date=day_schedule.date,
                timezone=day_schedule.timezone,
                planned_start=planned_start,
                planned_end=planned_end,
                score=score,
            )
        )

    used_task_ids = set(all_results) | {entry.task_id for entry in unscheduled_entries}
    output_registry = TaskRegistry(tasks={task_id: registry[task_id] for task_id in used_task_ids})

    return CanonicalDayScheduleOutput(
        date=day_schedule.date,
        timezone=day_schedule.timezone,
        fixed_blocks=list(day_schedule.fixed_blocks),
        tasks=output_registry,
        placements=placements,
        unscheduled=unscheduled_entries,
        total_score=compute_total_score(placements),
    )


def _free_intervals(placed: list[_Placed], day_total_minutes: int) -> list[tuple[int, int]]:
    ordered = sorted(placed, key=lambda item: item.start)
    intervals: list[tuple[int, int]] = []
    cursor = 0

    for item in ordered:
        if item.start > cursor:
            intervals.append((cursor, item.start))
        cursor = max(cursor, item.end)

    if cursor < day_total_minutes:
        intervals.append((cursor, day_total_minutes))

    return intervals
