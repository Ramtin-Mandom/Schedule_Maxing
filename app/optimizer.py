"""
optimizer.py

The canonical day engine: generate_day_schedule places one day's flexible
tasks around its fixed blocks under hard constraints (day window, fixed
blocks, durations, deadlines, required work), scored by app.reward, for every
scheduling mode (docs/scheduling-modes.md). evaluate_day_output reports a
generated day's final reward and mode metrics.

Placement search does not score every feasible minute: see the "Shared
event-search machinery" section for the analytic candidate derivation and
its rounding-aware tie-breaking, and _best_candidate_for_canonical_task_exhaustive
(test/benchmark-only) for the independent oracle that validates it.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Mapping
from typing import Callable

from app.planning.models import DaySchedule as CanonicalDaySchedule
from app.planning.models import DayScheduleOutput as CanonicalDayScheduleOutput
from app.planning.models import FixedBlock as CanonicalFixedBlock
from app.planning.models import ScheduledTask as CanonicalScheduledTask
from app.planning.models import Task as CanonicalTask
from app.planning.models import TaskRegistry, UnscheduledEntry, UnscheduledReasonCode, compute_total_score
from app.planning.models import preferred_third_bounds
from app.planning.preferences import (
    DayPreferences,
    OptimizerMode,
    day_preferences_to_reward_settings,
    effective_task_preferred_window,
)
from app.planning.time import MINUTES_PER_DAY
from app.reward import RewardSettings, ScoringTask, calculate_task_score


# =============================================================================
# Canonical day engine
# =============================================================================
#
# generate_day_schedule is the public entry point: it accepts a canonical
# DaySchedule (app.planning.models) and a resolved DayPreferences
# (app.planning.preferences), and returns a canonical DayScheduleOutput.
#
# Internally, everything is scheduled in integer minutes relative to the
# day's own local window start (offset 0 == day_window's start instant),
# converted back to aware UTC datetimes only when building the final
# ScheduledTask placements, so the public contract stays fully canonical
# (real dates, aware instants, stable UUID identity).
#
# Mode-specific candidate generation (see _candidate_starts):
#   precise_greedy, early_finish, night_owl, catch_up
#                   -- one-minute resolution, no snapping.
#   adhd_friendly   -- tasks > ADHD_SHORT_TASK_THRESHOLD_MINUTES start only on
#                       local wall-clock quarter-hour boundaries; tasks at or
#                       under that threshold may start on any valid minute,
#                       exactly like precise_greedy. Durations are never
#                       rounded or split in either mode.
#
# Mandatory scheduling (see generate_day_schedule): fixed blocks, then every
# required task as one "essential" tier, then optional tasks. A successful
# return always contains every required task_id exactly once; any other outcome raises
# MandatoryTaskSchedulingError with structured, per-task reasons -- this
# function never returns an apparently-successful partial schedule that is
# silently missing a required task.

ADHD_SHORT_TASK_THRESHOLD_MINUTES = 30
ADHD_QUARTER_HOUR_MINUTES = 15


class MandatoryTaskSchedulingError(ValueError):
    """
    Raised when generate_day_schedule cannot place every required task
    exactly once.

    `failures` gives one MandatoryTaskFailure per affected task_id. This is
    deliberately a ValueError subclass (matching the fixed-block validation
    errors) so existing `except ValueError` callers still catch it, while
    `.failures` gives structured detail a caller that wants it can inspect.

    Distinct from an ordinary *optional*-task UnscheduledEntry (reported,
    not raised).
    """

    def __init__(self, failures: list["MandatoryTaskFailure"]) -> None:
        self.failures = failures
        summary = "; ".join(
            f"{failure.task_id} [{failure.reason_code.value}]: {failure.explanation}" for failure in failures
        )
        super().__init__(f"Could not schedule every required task: {summary}")


@dataclass(frozen=True)
class MandatoryTaskFailure:
    """One required task that could not be placed."""

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
    inside the canonical engine's search, in day-relative integer minutes: a
    reward Neighbour (start, end, category, tag) plus canonical identity and
    score."""

    __slots__ = ("start", "end", "category", "tag", "task_id", "score", "fixed")

    def __init__(
        self,
        *,
        start: int,
        end: int,
        category: str,
        tag: str,
        task_id: uuid.UUID | None,
        score: float,
        fixed: bool,
    ) -> None:
        self.start = start
        self.end = end
        self.category = category
        self.tag = tag
        self.task_id = task_id
        self.score = score
        self.fixed = fixed


def _to_offset(instant: datetime, day_start_utc: datetime) -> int:
    """
    Convert an aware instant to day-relative integer minutes (0 ==
    day_start_utc). Every public model feeding this (FixedBlock.planned_start/
    planned_end, Task.deadline) is a plain aware datetime with no enforced whole-minute precision, but
    this engine's entire search operates in integer minutes -- silently
    round()-ing an instant with nonzero seconds/microseconds (nearest-minute,
    banker's rounding) could move a fixed block's reported boundary, permit
    an overlap that the sub-minute reality does not have, or miss a deadline.
    Reject sub-minute precision explicitly instead of rounding it away.
    """
    delta = instant - day_start_utc
    if delta.seconds % 60 != 0 or delta.microseconds != 0:
        raise ValueError(
            f"{instant.isoformat()} is not aligned to a whole minute relative to the day "
            f"window start ({day_start_utc.isoformat()}); this engine only supports "
            "minute-precision instants for fixed blocks and deadlines."
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
            _Placed(start=start, end=end, category="fixed", tag="", task_id=None, score=0.0, fixed=True)
        )

    placed.sort(key=lambda item: item.start)
    for earlier, later in zip(placed, placed[1:]):
        if earlier.end > later.start:
            raise ValueError("Invalid fixed blocks: overlapping intervals.")

    return placed


def _normalize_preferred_window(
    preference_time: dict[str, int] | None,
    day_window_start_minute: int,
) -> tuple[int, int] | None:
    """
    Convert a preferred-time window expressed in local minutes-from-midnight
    (as produced by app.planning.preferences.effective_task_preferred_window
    -- see LocalTimeWindow's own docstring)
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
    return offset_start, offset_start + (local_end - local_start)


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

    if mode != OptimizerMode.ADHD_FRIENDLY or duration <= ADHD_SHORT_TASK_THRESHOLD_MINUTES:
        return list(range(first_start, latest_start + 1))

    start = _next_quarter_hour_offset(first_start, day_window_start_minute)
    return list(range(start, latest_start + 1, ADHD_QUARTER_HOUR_MINUTES))


# =============================================================================
# Shared event-search machinery
# =============================================================================
#
# _best_start_in_free_interval replaces "score every feasible minute" with a
# small, analytically-derived candidate set that is provably guaranteed to
# contain the true best-scoring, earliest-tied placement under the real
# (rounded) app.reward.calculate_task_score -- see _event_breakpoints'
# docstring for exactly which reward components drive which breakpoints, and
# _earliest_best_in_continuous_range for how rounding-aware ties are
# resolved without scanning every minute. The search runs unrestricted
# (one-minute) or on a lattice (adhd_friendly's quarter hour).
#
# _best_candidate_for_canonical_task_exhaustive is deliberately independent
# of this machinery (see its own docstring) -- it exists purely so
# tests/benchmarks have an oracle that cannot share a bug with the code it is
# meant to validate.


def _next_lattice_offset(first_start: int, step: int, anchor_local_minute: int) -> int:
    """
    Generalizes _next_quarter_hour_offset to an arbitrary grid step and
    anchor (the local wall-clock minute that offset 0 corresponds to):
    the smallest offset >= first_start that lands on the step-minute grid
    relative to that anchor. adhd_friendly's quarter-hour grid anchors at
    day_window_start_minute (local midnight-relative), via
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
    preferred_window: tuple[int, int] | None,
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
    weight_category_bonus is read by no component, so it needs no
    breakpoint):
      - weight (_weight_score): constant in start_time -- contributes no
        breakpoint.
      - time (_time_score): a full-bonus constant while
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
        preferred_start, preferred_end = preferred_window
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
    scoring_task: ScoringTask,
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

    preferred_window = scoring_task.preferred_window
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


def _preferred_start_range(task: CanonicalTask, day_total_minutes: int) -> tuple[int, int] | None:
    """
    The start offsets that put `task` inside its preferred third of the day
    (Task.preferred_time: Early / Mid / Late of this day's own window, offset
    0 .. day_total_minutes), or None when it has no preference. A placement
    is inside when it lies wholly within the third; a task longer than the
    third is inside only when it starts at the third's start.

    This range is a preference tier, not a hard constraint: every engine
    places the task inside it whenever a valid placement exists there, ranks
    the placements inside it with the ordinary reward, and looks outside it
    only when it holds no valid placement (see _best_candidate_for_canonical_task,
    _run_greedy_tier and _refine_time_objective).
    """
    if task.preferred_time is None:
        return None
    lo, hi = preferred_third_bounds(task.preferred_time, 0, day_total_minutes)
    return lo, max(lo, hi - task.estimated_duration_minutes)


def _in_preferred_range(task: CanonicalTask, start: int, day_total_minutes: int) -> bool:
    preferred = _preferred_start_range(task, day_total_minutes)
    return preferred is not None and preferred[0] <= start <= preferred[1]


def _canonical_scoring_task(
    task: CanonicalTask, day_preferences: DayPreferences, day_window_start_minute: int, day_total_minutes: int
) -> ScoringTask:
    """
    The reward's view of `task` in day-relative minutes. A task with a
    preferred third scores its time preference against that third (so, when
    it must fall outside, the closest placement to it scores best); any
    other task against its own legacy window, else its category's preferred
    window, if it has one. Neither its points nor anything else about its
    worth is scored: a placement's score is its quality, not the task's value.
    """
    if task.preferred_time is not None:
        window = preferred_third_bounds(task.preferred_time, 0, day_total_minutes)
    else:
        window = _normalize_preferred_window(
            effective_task_preferred_window(task, day_preferences), day_window_start_minute
        )
    return ScoringTask(category=task.category, duration=task.estimated_duration_minutes,
                       tag=task.tags[0] if task.tags else "", preferred_window=window)


def _prepare_canonical_task_search(
    task: CanonicalTask,
    day_total_minutes: int,
    mode: OptimizerMode,
    day_window_start_minute: int,
    day_preferences: DayPreferences,
    deadline_offset: int | None,
) -> tuple[int, int, int, dict, bool] | None:
    """
    Shared setup for both the production event search and the exhaustive
    reference: resolves duration/first_start/latest_start, builds the
    coordinate-normalized ScoringTask, and the adhd_mode flag.
    Returns None immediately when the feasible range is already empty.
    """
    duration = task.estimated_duration_minutes
    first_start = 0
    latest_start = day_total_minutes - duration
    if deadline_offset is not None:
        latest_start = min(latest_start, deadline_offset - duration)
    if latest_start < first_start:
        return None

    scoring_task = _canonical_scoring_task(task, day_preferences, day_window_start_minute, day_total_minutes)
    adhd_mode = mode == OptimizerMode.ADHD_FRIENDLY
    return duration, first_start, latest_start, scoring_task, adhd_mode


def _preferred_tier_first(search: Callable[[int, int], tuple[int, int, float] | None], task: CanonicalTask,
                          first_start: int, latest_start: int, day_total_minutes: int
                          ) -> tuple[int, int, float] | None:
    """
    `search(first_start, latest_start)` restricted to the task's preferred
    third first; the whole feasible range only when the third holds no valid
    placement (or the task has no preference).
    """
    preferred = _preferred_start_range(task, day_total_minutes)
    if preferred is not None:
        inside = search(max(first_start, preferred[0]), min(latest_start, preferred[1]))
        if inside is not None:
            return inside
    return search(first_start, latest_start)


def _best_candidate_for_canonical_task_exhaustive(
    task: CanonicalTask,
    placed: list[_Placed],
    day_total_minutes: int,
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
        task, day_total_minutes, mode, day_window_start_minute, day_preferences, deadline_offset
    )
    if prepared is None:
        return None
    duration, full_first_start, full_latest_start, scoring_task, adhd_mode = prepared

    ordered = sorted(placed, key=lambda item: item.start)

    def search(first_start: int, latest_start: int) -> tuple[int, int, float] | None:
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
                    for start in _candidate_starts(mode, duration, clipped_start, clipped_latest,
                                                   day_window_start_minute):
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

    return _preferred_tier_first(search, task, full_first_start, full_latest_start, day_total_minutes)


def _best_candidate_for_canonical_task(
    task: CanonicalTask,
    placed: list[_Placed],
    day_total_minutes: int,
    mode: OptimizerMode,
    day_window_start_minute: int,
    settings: RewardSettings,
    day_preferences: DayPreferences,
    deadline_offset: int | None,
) -> tuple[int, int, float] | None:
    """
    Find this task's best-scoring valid placement, pruned to free intervals
    only (never probing a minute that is already occupied) and to the
    deadline-bound start range -- see the module docstring and
    _best_start_in_free_interval for how candidate starts are derived
    without scoring every feasible minute.
    """
    prepared = _prepare_canonical_task_search(
        task, day_total_minutes, mode, day_window_start_minute, day_preferences, deadline_offset
    )
    if prepared is None:
        return None
    duration, full_first_start, full_latest_start, scoring_task, adhd_mode = prepared

    grid_constrained = mode == OptimizerMode.ADHD_FRIENDLY and duration > ADHD_SHORT_TASK_THRESHOLD_MINUTES
    lattice_step = ADHD_QUARTER_HOUR_MINUTES if grid_constrained else None

    ordered = sorted(placed, key=lambda item: item.start)

    def search(first_start: int, latest_start: int) -> tuple[int, int, float] | None:
        if latest_start < first_start:
            return None
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

    return _preferred_tier_first(search, task, full_first_start, full_latest_start, day_total_minutes)


def _run_greedy_tier(
    task_ids: list[uuid.UUID],
    registry: dict[uuid.UUID, CanonicalTask],
    placed: list[_Placed],
    day_total_minutes: int,
    mode: OptimizerMode,
    day_window_start_minute: int,
    settings: RewardSettings,
    day_preferences: DayPreferences,
    deadline_offsets: dict[uuid.UUID, int],
    task_bonuses: Mapping[uuid.UUID, float] | None = None,
) -> tuple[dict[uuid.UUID, tuple[int, int, float]], list[uuid.UUID]]:
    """
    Repeatedly place the best-scoring feasible task from `task_ids`.
    `task_bonuses` (Catch-Up) is added to a task's candidate score only when
    choosing which task to place next -- a task-wide constant cannot change
    where a task goes -- and the stored score stays the insertion score.
    A task that can still be placed inside its preferred third (Early / Mid
    / Late) is chosen before any other, so weaker rewards of other tasks
    never take its place there; a task whose third is already full is
    chosen last. Without preferred thirds and bonuses the selection is
    exactly the baseline's. Mutates `placed` in place so a later tier sees
    every placement made here. Returns
    (task_id -> (start, end, score) for every placed task, and the task_ids
    that could not be placed).
    """
    remaining = list(task_ids)
    results: dict[uuid.UUID, tuple[int, int, float]] = {}

    while remaining:
        best_task_id: uuid.UUID | None = None
        best_candidate: tuple[int, int, float] | None = None
        best_score: tuple[int, float] = (-1, float("-inf"))

        for task_id in remaining:
            candidate = _best_candidate_for_canonical_task(
                registry[task_id],
                placed,
                day_total_minutes,
                mode,
                day_window_start_minute,
                settings,
                day_preferences,
                deadline_offsets.get(task_id),
            )
            if candidate is None:
                continue

            task = registry[task_id]
            tier = 1 if task.preferred_time is None else 2 * _in_preferred_range(task, candidate[0], day_total_minutes)
            selection = (tier, candidate[2] + (task_bonuses.get(task_id, 0.0) if task_bonuses else 0.0))
            if selection > best_score:
                best_task_id = task_id
                best_candidate = candidate
                best_score = selection

        if best_task_id is None or best_candidate is None:
            break

        start, end, score = best_candidate
        task = registry[best_task_id]
        tag = task.tags[0] if task.tags else ""

        placed.append(
            _Placed(
                start=start, end=end, category=task.category, tag=tag,
                task_id=best_task_id, score=score, fixed=False,
            )
        )
        results[best_task_id] = (start, end, score)
        remaining.remove(best_task_id)

    return results, remaining


def _mandatory_failure_reason(
    task_id: uuid.UUID,
    deadline_offsets: dict[uuid.UUID, int],
) -> tuple[UnscheduledReasonCode, str]:
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
    task_bonuses: Mapping[uuid.UUID, float] | None = None,
) -> CanonicalDayScheduleOutput:
    """
    Generate one day's canonical schedule.

    The day's mode (preferences.optimizer_mode, app/mode_objectives.py):
    Normal and ADHD run exactly the baseline search; Catch-Up adds
    `task_bonuses` to the greedy task choice; Early Finish and Night Owl
    repack the baseline's selected work afterwards (same tasks, same hard
    constraints) and store each placement's final baseline reward as its
    score.

    Order: validate/place fixed blocks, schedule every required task
    ("essential" tier), then schedule optional tasks. Raises
    MandatoryTaskSchedulingError if any required task cannot be placed
    (see that class and MandatoryTaskFailure). Optional tasks that cannot be
    placed are reported in the result's `unscheduled` list instead.

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

    # A To Do is a checklist item: never placed, and never reported as unscheduled.
    registry: dict[uuid.UUID, CanonicalTask] = {
        task_id: task for task_id in day_schedule.task_ids
        if not (task := day_schedule.tasks.get(task_id)).is_todo
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

    # Tiers keep movable_ids' input order, so equal-scoring tasks tie-break in input order.
    essential_ids = [task_id for task_id in movable_ids if registry[task_id].required]
    optional_ids = [task_id for task_id in movable_ids if not registry[task_id].required]

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
                        f"required tasks need {essential_total_duration} "
                        f"minutes in total, but only {free_capacity} minutes of free capacity remain after "
                        "fixed blocks."
                    ),
                    proven_infeasible=True,
                )
                for task_id in essential_ids
            ]
        )

    bonuses = task_bonuses if mode == OptimizerMode.CATCH_UP else None
    essential_results, essential_remaining = _run_greedy_tier(
        essential_ids, registry, placed, day_total_minutes,
        mode, day_window_start_minute, settings, preferences, deadline_offsets, bonuses,
    )

    if essential_remaining:
        raise MandatoryTaskSchedulingError(
            [
                MandatoryTaskFailure(task_id=task_id, reason_code=reason_code, explanation=explanation, proven_infeasible=False)
                for task_id in essential_remaining
                for reason_code, explanation in [_mandatory_failure_reason(task_id, deadline_offsets)]
            ]
        )

    optional_results, optional_remaining = _run_greedy_tier(
        optional_ids, registry, placed, day_total_minutes,
        mode, day_window_start_minute, settings, preferences, deadline_offsets, bonuses,
    )

    unscheduled_entries = []
    for task_id in optional_remaining:
        reason_code, explanation = _mandatory_failure_reason(task_id, deadline_offsets)
        unscheduled_entries.append(UnscheduledEntry(task_id=task_id, reason_code=reason_code, explanation=explanation))

    previous_placement_ids: dict[tuple[uuid.UUID, datetime, datetime], uuid.UUID] = {}
    if previous_result is not None:
        for placement in previous_result.placements:
            previous_placement_ids[(placement.task_id, placement.planned_start, placement.planned_end)] = placement.id

    all_results = {**essential_results, **optional_results}
    if mode in (OptimizerMode.EARLY_FINISH, OptimizerMode.NIGHT_OWL) and all_results:
        all_results = _refine_time_objective(
            mode, all_results, registry, movable_ids, placed, day_total_minutes, settings, preferences, deadline_offsets,
        )
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


# -----------------------------------------------------------------------------
# Final evaluation and the time objectives (app/mode_objectives.py)
# -----------------------------------------------------------------------------


def _scoring_tasks(registry: Mapping[uuid.UUID, CanonicalTask], preferences: DayPreferences,
                   day_total_minutes: int) -> dict[uuid.UUID, ScoringTask]:
    return {task_id: _canonical_scoring_task(task, preferences, preferences.day_window.start_minute, day_total_minutes)
            for task_id, task in registry.items()}


def _final_scores(
    starts: Mapping[uuid.UUID, int],
    registry: Mapping[uuid.UUID, CanonicalTask],
    scoring_tasks: Mapping[uuid.UUID, ScoringTask],
    fixed: list[_Placed],
    day_total_minutes: int,
    settings: RewardSettings,
    *,
    adhd_mode: bool,
) -> dict[uuid.UUID, float]:
    """
    The final reward of every flexible placement, against its actual
    chronological neighbors (fixed blocks included) -- unlike the insertion
    score, which saw only the neighbors placed before it.
    """
    items = list(fixed)
    for task_id, start in starts.items():
        task = registry[task_id]
        items.append(_Placed(start=start, end=start + task.estimated_duration_minutes,
                             category=task.category, tag=task.tags[0] if task.tags else "", task_id=task_id,
                             score=0.0, fixed=False))
    items.sort(key=lambda item: (item.start, item.end))
    scores: dict[uuid.UUID, float] = {}
    for index, item in enumerate(items):
        if item.fixed:
            continue
        scores[item.task_id] = calculate_task_score(
            scoring_tasks[item.task_id], item.start,
            previous_task=items[index - 1] if index > 0 else None,
            next_task=items[index + 1] if index + 1 < len(items) else None,
            settings=settings, adhd_mode=adhd_mode, day_start=0, day_end=day_total_minutes,
        )
    return scores


def _refine_time_objective(
    mode: OptimizerMode,
    results: dict[uuid.UUID, tuple[int, int, float]],
    registry: Mapping[uuid.UUID, CanonicalTask],
    input_order: list[uuid.UUID],
    placed: list[_Placed],
    day_total_minutes: int,
    settings: RewardSettings,
    preferences: DayPreferences,
    deadline_offsets: Mapping[uuid.UUID, int],
) -> dict[uuid.UUID, tuple[int, int, float]]:
    """Repack the baseline's selected work for Early Finish / Night Owl; scores become final baseline rewards."""
    from app.mode_objectives import RepackTask, mode_weight, refine

    fixed = [item for item in placed if item.fixed]
    scoring_tasks = _scoring_tasks({task_id: registry[task_id] for task_id in results}, preferences,
                                   day_total_minutes)
    position = {task_id: index for index, task_id in enumerate(input_order)}
    ranked = sorted(results, key=lambda task_id: position.get(task_id, 0))
    tasks = []
    for rank, task_id in enumerate(ranked):
        task = registry[task_id]
        earliest = 0
        latest = min(day_total_minutes, deadline_offsets.get(task_id, day_total_minutes))
        if _in_preferred_range(task, results[task_id][0], day_total_minutes):
            # Placed inside its preferred third: repacking may move it, but only within that third.
            lo, hi = _preferred_start_range(task, day_total_minutes)
            earliest, latest = lo, min(latest, hi + task.estimated_duration_minutes)
        tasks.append(RepackTask(
            task_id=task_id, duration=task.estimated_duration_minutes, earliest=earliest, latest_finish=latest,
            rank=rank,
        ))

    def baseline_reward(starts: Mapping[uuid.UUID, int]) -> float:
        return sum(_final_scores(starts, registry, scoring_tasks, fixed, day_total_minutes, settings,
                                 adhd_mode=False).values())

    refinement = refine(
        mode.value, tasks, {task_id: start for task_id, (start, _, _) in results.items()},
        [(item.start, item.end) for item in fixed], day_total_minutes, mode_weight(settings.weight_importance),
        baseline_reward,
    )
    scores = _final_scores(refinement.starts, registry, scoring_tasks, fixed, day_total_minutes, settings,
                           adhd_mode=False)
    return {task_id: (start, start + registry[task_id].estimated_duration_minutes, scores[task_id])
            for task_id, start in refinement.starts.items()}


@dataclass(frozen=True)
class DayEvaluation:
    """A saved or generated day's objective, recomputed (nothing stored is changed)."""

    mode: str
    #: The sum of stored placement scores (insertion scores for Normal/ADHD/Catch-Up).
    stored_score: float
    #: B(S): every flexible placement's final baseline reward over its actual neighbors.
    baseline_reward: float
    #: ADHD's short-gap component (0 unless ADHD and configured).
    adhd_component: float
    #: Early Finish / Night Owl bonus, or the Catch-Up bonuses of the placed tasks.
    mode_bonus: float
    objective: float
    first_start_minute: int | None
    last_finish_minute: int | None
    idle_minutes: int
    fixed_minutes: int
    scheduled_count: int
    scheduled_minutes: int


def evaluate_day_output(
    output: CanonicalDayScheduleOutput,
    preferences: DayPreferences,
    *,
    task_bonuses: Mapping[uuid.UUID, float] | None = None,
) -> DayEvaluation:
    """
    The objective of a day's output under its mode, recomputed with the final
    evaluator -- for reports and comparisons. Kept work given to the engine as
    busy time is part of the output's fixed blocks; placements whose task is
    missing from the output's registry are counted as busy time.
    """
    from app.mode_objectives import mode_weight, schedule_metrics, time_bonus

    day_start_utc, day_end_utc = preferences.to_local_day_window().to_utc_instants()
    total = _to_offset(day_end_utc, day_start_utc)
    settings = day_preferences_to_reward_settings(preferences)
    fixed = [
        _Placed(start=_to_offset(block.planned_start, day_start_utc), end=_to_offset(block.planned_end, day_start_utc),
                category=block.category, tag="", task_id=None, score=0.0, fixed=True)
        for block in output.fixed_blocks
    ]
    registry: dict[uuid.UUID, CanonicalTask] = {}
    starts: dict[uuid.UUID, int] = {}
    for placement in output.placements:
        task = output.tasks.get(placement.task_id)
        start, end = _to_offset(placement.planned_start, day_start_utc), _to_offset(placement.planned_end, day_start_utc)
        if task is None or placement.task_id in starts:
            fixed.append(_Placed(start=start, end=end, category="", tag="", task_id=None, score=0.0,
                                 fixed=True))
            continue
        registry[placement.task_id] = task.model_copy(update={"estimated_duration_minutes": end - start})
        starts[placement.task_id] = start
    scoring_tasks = _scoring_tasks(registry, preferences, total)
    base = _final_scores(starts, registry, scoring_tasks, fixed, total, settings, adhd_mode=False)
    mode = preferences.optimizer_mode
    adhd = 0.0
    if mode == OptimizerMode.ADHD_FRIENDLY:
        with_bonus = _final_scores(starts, registry, scoring_tasks, fixed, total, settings, adhd_mode=True)
        adhd = sum(with_bonus.values()) - sum(base.values())
    flexible = [(start, start + registry[task_id].estimated_duration_minutes) for task_id, start in starts.items()]
    fixed_intervals = [(item.start, item.end) for item in fixed]
    metrics = schedule_metrics(flexible, fixed_intervals, total)
    if mode == OptimizerMode.CATCH_UP:
        bonus = sum((task_bonuses or {}).get(task_id, 0.0) for task_id in starts)
    else:
        bonus = time_bonus(mode.value, metrics, mode_weight(settings.weight_importance))
    baseline = sum(base.values())
    return DayEvaluation(
        mode=mode.value, stored_score=sum(p.score for p in output.placements), baseline_reward=baseline,
        adhd_component=adhd, mode_bonus=bonus, objective=baseline + adhd + bonus,
        first_start_minute=metrics.first_start, last_finish_minute=metrics.last_finish,
        idle_minutes=metrics.idle_minutes, fixed_minutes=sum(end - start for start, end in fixed_intervals),
        scheduled_count=len(flexible), scheduled_minutes=sum(end - start for start, end in flexible),
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
