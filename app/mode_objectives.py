"""
app/mode_objectives.py

Scheduling objectives of the five user-facing modes (docs/scheduling-modes.md),
on top of the one canonical day engine (app/optimizer.py). Modes are
objectives, not separate optimizers: every mode uses the same hard
constraints (window, fixed blocks and kept work, durations, deadlines,
required work) and the same greedy event-candidate search.

    Normal       B(S): the existing baseline reward, existing search and ties.
    ADHD         the existing search on the local quarter-hour lattice for
                 tasks over 30 minutes, with the existing (configurable,
                 default zero) short-gap bonus; nothing else.
    Early Finish B(S) + W*N*(0.50*E + 0.35*C + 0.15*(1-U))
    Night Owl    B(S) + W*N*(0.50*T + 0.35*C + 0.15*U)
    Catch-Up     B(S) + sum of per-task history bonuses
                 (app/planning/catch_up.py), applied where the greedy search
                 chooses which task to place next.

B(S), the final baseline reward, is recomputed after generation over the
actual chronological neighbors of every placement (final_baseline_scores);
the engine's insertion scores are computed against the neighbors present when
each task was inserted and can differ. For a nonempty flexible schedule, in
day-relative minutes: A=0 and Z the window end, H=max(1, Z-A); F/L the first
start / last finish of the flexible placements; N their count; G the
unoccupied minutes between F and L (fixed blocks and kept work are occupied,
counted once); E=(Z-L)/H, T=(F-A)/H, C=1-G/H, U=mean((start_i-A)/H), each
clamped to [0, 1]. An empty flexible schedule has every component 0.
W=max(1, |weight_importance|) of the date's reward settings, frozen for the
run; its unit is the baseline reward's own (one task's base weight).

Early Finish and Night Owl refine the baseline greedy result: the same
selected work set (no task is dropped or shortened; unscheduled work stays
reported) is repacked -- left (as early as possible) for Early, right (as
late as possible) for Night -- in a bounded set of orders: every order when
there are at most MAX_EXHAUSTIVE_ORDER_TASKS tasks, else the baseline's
chronological order and the input order. Every candidate keeps every hard constraint; the best
objective wins (ties: Early by earlier L, then smaller G; Night by later F,
then smaller G; then candidate order, the baseline first), and the baseline
itself always competes, so refinement never returns a worse objective. This
is a bounded heuristic: no global optimum is claimed.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

#: Repacking enumerates every order up to this many tasks (6! = 720 orders at most).
MAX_EXHAUSTIVE_ORDER_TASKS = 6

EARLY_WEIGHTS = (0.50, 0.35, 0.15)
NIGHT_WEIGHTS = (0.50, 0.35, 0.15)


@dataclass(frozen=True)
class ScheduleMetrics:
    """The time-shape of a day's flexible placements (see the module docstring)."""

    count: int
    first_start: int | None
    last_finish: int | None
    idle_minutes: int
    early: float
    late_start: float
    compactness: float
    mean_start: float

    @property
    def empty(self) -> bool:
        return self.count == 0


def _union_minutes(intervals: Iterable[tuple[int, int]], lo: int, hi: int) -> int:
    clipped = sorted((max(lo, start), min(hi, end)) for start, end in intervals if end > lo and start < hi)
    total, cursor = 0, lo
    for start, end in clipped:
        start = max(start, cursor)
        if end > start:
            total += end - start
            cursor = end
    return total


def schedule_metrics(flexible: list[tuple[int, int]], fixed: list[tuple[int, int]], day_total: int) -> ScheduleMetrics:
    if not flexible:
        return ScheduleMetrics(0, None, None, 0, 0.0, 0.0, 0.0, 0.0)
    horizon = max(1, day_total)
    first = min(start for start, _ in flexible)
    last = max(end for _, end in flexible)
    occupied = _union_minutes([*flexible, *fixed], first, last)
    idle = max(0, (last - first) - occupied)

    def clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    return ScheduleMetrics(
        count=len(flexible), first_start=first, last_finish=last, idle_minutes=idle,
        early=clamp((day_total - last) / horizon), late_start=clamp(first / horizon),
        compactness=clamp(1 - idle / horizon),
        mean_start=clamp(sum(start for start, _ in flexible) / len(flexible) / horizon),
    )


def time_bonus(mode: str, metrics: ScheduleMetrics, weight: float) -> float:
    """The Early Finish / Night Owl bonus of a schedule (0 for other modes and empty schedules)."""
    if metrics.empty:
        return 0.0
    if mode == "early_finish":
        a, b, c = EARLY_WEIGHTS
        return weight * metrics.count * (a * metrics.early + b * metrics.compactness + c * (1 - metrics.mean_start))
    if mode == "night_owl":
        a, b, c = NIGHT_WEIGHTS
        return weight * metrics.count * (a * metrics.late_start + b * metrics.compactness + c * metrics.mean_start)
    return 0.0


def mode_weight(weight_importance: float) -> float:
    return max(1.0, abs(weight_importance))


@dataclass(frozen=True)
class RepackTask:
    task_id: uuid.UUID
    duration: int
    #: Earliest start (the start of its preferred third when it was placed inside it), else 0.
    earliest: int
    #: Latest finish (deadline / window end).
    latest_finish: int
    #: Stable rank (lower first) (input order) for the ranked order and ties.
    rank: int


@dataclass
class Refinement:
    starts: dict[uuid.UUID, int]
    objective: float
    baseline_reward: float
    bonus: float
    metrics: ScheduleMetrics
    candidates_tried: int = 0
    used_baseline: bool = True
    notes: list[str] = field(default_factory=list)


def _first_fit(task: RepackTask, earliest: int, latest_finish: int, busy: list[tuple[int, int]]) -> int | None:
    start = earliest
    for lo, hi in sorted(busy):
        if start + task.duration <= lo:
            break
        if hi > start and lo < start + task.duration:
            start = max(start, hi)
    return start if start + task.duration <= latest_finish else None


def _last_fit(task: RepackTask, earliest: int, latest_finish: int, busy: list[tuple[int, int]]) -> int | None:
    end = latest_finish
    for lo, hi in sorted(busy, reverse=True):
        if end - task.duration >= hi:
            break
        if hi > end - task.duration and lo < end:
            end = min(end, lo)
    start = end - task.duration
    return start if start >= earliest else None


def _pack(order: list[RepackTask], fixed: list[tuple[int, int]], *, left: bool) -> dict[uuid.UUID, int] | None:
    """Pack `order` (reversed for right packing) around `fixed`; None if any task fails to fit."""
    busy = list(fixed)
    starts: dict[uuid.UUID, int] = {}
    for task in (order if left else list(reversed(order))):
        if left:
            start = _first_fit(task, task.earliest, task.latest_finish, busy)
        else:
            start = _last_fit(task, task.earliest, task.latest_finish, busy)
        if start is None:
            return None
        starts[task.task_id] = start
        busy.append((start, start + task.duration))
    return starts


def _orders(tasks: list[RepackTask], baseline: Mapping[uuid.UUID, int]) -> list[list[RepackTask]]:
    chronological = sorted(tasks, key=lambda task: (baseline[task.task_id], task.rank))
    by_rank = sorted(tasks, key=lambda task: task.rank)
    if len(tasks) <= MAX_EXHAUSTIVE_ORDER_TASKS:
        return [chronological, *(list(order) for order in itertools.permutations(by_rank)
                                 if list(order) != chronological)]
    return [chronological, by_rank]


def refine(
    mode: str,
    tasks: list[RepackTask],
    baseline_starts: Mapping[uuid.UUID, int],
    fixed: list[tuple[int, int]],
    day_total: int,
    weight: float,
    baseline_reward: Callable[[Mapping[uuid.UUID, int]], float],
) -> Refinement:
    """
    The Early Finish / Night Owl refinement of a baseline greedy result (see
    the module docstring). `baseline_reward(starts)` is the final B(S) of a
    candidate; the baseline candidate always competes.
    """
    durations = {task.task_id: task.duration for task in tasks}

    def evaluate(starts: Mapping[uuid.UUID, int]) -> tuple[float, float, float, ScheduleMetrics]:
        metrics = schedule_metrics([(s, s + durations[t]) for t, s in starts.items()], fixed, day_total)
        reward = baseline_reward(starts)
        bonus = time_bonus(mode, metrics, weight)
        return reward + bonus, reward, bonus, metrics

    def key(objective: float, metrics: ScheduleMetrics, index: int) -> tuple:
        if mode == "early_finish":
            return (-round(objective, 9), metrics.last_finish or 0, metrics.idle_minutes, index)
        return (-round(objective, 9), -(metrics.first_start or 0), metrics.idle_minutes, index)

    objective, reward, bonus, metrics = evaluate(baseline_starts)
    best = Refinement(dict(baseline_starts), objective, reward, bonus, metrics)
    best_key = key(objective, metrics, 0)
    if not tasks:
        return best
    tried = 0
    for index, order in enumerate(_orders(tasks, baseline_starts), start=1):
        starts = _pack(order, fixed, left=mode == "early_finish")
        tried += 1
        if starts is None:
            continue
        objective, reward, bonus, metrics = evaluate(starts)
        candidate_key = key(objective, metrics, index)
        if candidate_key < best_key:
            best = Refinement(starts, objective, reward, bonus, metrics, used_baseline=False)
            best_key = candidate_key
    best.candidates_tried = tried
    if best.used_baseline:
        best.notes.append("no repacking improved the objective; the baseline placement was kept")
    return best
