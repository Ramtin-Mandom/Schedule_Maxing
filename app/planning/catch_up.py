"""
app/planning/catch_up.py

The history evidence of the Catch-Up mode (docs/scheduling-modes.md): per
historical category, how often the user skipped versus completed work in a
fixed lookback window, turned into a bounded per-task bonus.

Evidence (catch_up_evidence) is ONE owner-scoped bulk read taken before any
search -- PlanningService.schedule_history over [as_of - lookback, as_of),
the same read model analytics use -- never a query inside the scorer.

    - as_of is fixed per request: the current UTC day's midnight, so the
      evidence (and the inputs fingerprint that includes its digest) is
      stable within a day and changes only when outcomes change or the day
      does;
    - one immutable occurrence contributes at most one outcome: placements
      are grouped by occurrence key (app/planning/occurrence.py), and of an
      occurrence's planned placements (its lineage of moves/regenerations
      included) the latest terminal outcome counts;
    - completed -> a completion, skipped (an explicit user skip) -> a miss.
      Cancelled (by the user, a move or a regeneration), scheduled/pending,
      in-progress, paused, future placements and placements without an
      execution are not evidence: nothing is inferred from elapsed time;
    - the category is the historical snapshot (the execution's category, else
      the placement's task_category); a missing snapshot is "unknown" and
      earns no bonus. Current task categories and names are never
      substituted for history.

Bonus for a task of category k with m misses and c completions (n = m + c):
n < MIN_OUTCOMES -> 0 ("insufficient history"); otherwise
p = (m + 1) / (n + 4) (a Beta(1, 3) smoothed miss rate) and
bonus = W * (n / (n + 5)) * p, in [0, W], with W the mode weight
(app/mode_objectives.py). No history means a schedule exactly like Normal.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from app.execution.models import ExecutionStatus
from app.planning.occurrence import occurrence_key

LOOKBACK_DAYS = 90
MIN_OUTCOMES = 5


@dataclass(frozen=True)
class CategoryEvidence:
    misses: int = 0
    completions: int = 0

    @property
    def outcomes(self) -> int:
        return self.misses + self.completions


@dataclass(frozen=True)
class CatchUpEvidence:
    as_of: datetime
    lookback_days: int
    by_category: dict[str, CategoryEvidence] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        data = {"as_of": self.as_of.isoformat(), "lookback": self.lookback_days,
                "categories": {key: [value.misses, value.completions]
                               for key, value in sorted(self.by_category.items())}}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()

    def bonus(self, category: str | None, weight: float) -> tuple[float, dict]:
        """(bonus, a bounded explanation) for a task of `category`."""
        evidence = self.by_category.get(category) if category else None
        explanation = {"category": category or "unknown", "lookback_days": self.lookback_days,
                       "as_of": self.as_of.date().isoformat()}
        if evidence is None or evidence.outcomes < MIN_OUTCOMES:
            n = evidence.outcomes if evidence else 0
            return 0.0, {**explanation, "outcomes": n, "reason": "insufficient history"}
        m, c, n = evidence.misses, evidence.completions, evidence.outcomes
        smoothed = (m + 1) / (n + 4)
        reliability = n / (n + 5)
        bonus = min(weight, max(0.0, weight * reliability * smoothed))
        return bonus, {**explanation, "misses": m, "completions": c, "outcomes": n,
                       "smoothed_miss_rate": round(smoothed, 4), "reliability": round(reliability, 4),
                       "bonus": round(bonus, 4), "bound": weight}


def evidence_as_of(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(timezone.utc).date(), time(0), tzinfo=timezone.utc)


def catch_up_evidence(service, now: datetime, *, lookback_days: int = LOOKBACK_DAYS) -> CatchUpEvidence:
    """The evidence of the account `service` is scoped to (one bulk read; see the module docstring)."""
    as_of = evidence_as_of(now)
    history = service.schedule_history(as_of - timedelta(days=lookback_days), as_of)
    return summarize(history, as_of, lookback_days)


def summarize(history, as_of: datetime, lookback_days: int = LOOKBACK_DAYS) -> CatchUpEvidence:
    """Aggregate a ScheduleHistory (app/planning/history.py) into per-category evidence."""
    start = as_of - timedelta(days=lookback_days)
    # occurrence key -> (planned start, status, category) of its latest terminal outcome
    latest: dict[tuple, tuple[datetime, ExecutionStatus, str | None]] = {}
    for placement_id, record in history.executions.items():
        execution = record.execution
        if execution.deleted_at is not None or execution.status not in (ExecutionStatus.COMPLETED,
                                                                         ExecutionStatus.SKIPPED):
            continue
        placement = history.placements.get(placement_id)
        if placement is None or not start <= placement.planned_start < as_of:
            continue
        task = history.tasks.get(placement.task_id)
        key = _key(placement, task)
        category = execution.category or placement.task_category or None
        previous = latest.get(key)
        if previous is None or placement.planned_start > previous[0]:
            latest[key] = (placement.planned_start, execution.status, category)
    counts: dict[str, list[int]] = {}
    for _, status, category in latest.values():
        if not category:
            continue  # unknown snapshot: no evidence for any category
        bucket = counts.setdefault(category, [0, 0])
        bucket[0 if status == ExecutionStatus.SKIPPED else 1] += 1
    return CatchUpEvidence(as_of=as_of, lookback_days=lookback_days,
                           by_category={key: CategoryEvidence(m, c) for key, (m, c) in counts.items()})


def _key(placement, task) -> tuple:
    if task is None:
        return (placement.task_id, None)
    try:
        return occurrence_key(placement, task)
    except ValueError:
        return (placement.task_id, placement.planned_date)


def task_bonuses(evidence: CatchUpEvidence, tasks: Iterable, weight: float) -> tuple[dict[uuid.UUID, float], dict]:
    """(task id -> bonus, task id -> explanation) for the tasks of a date."""
    bonuses: dict[uuid.UUID, float] = {}
    explanations: dict[uuid.UUID, dict] = {}
    for task in tasks:
        bonus, explanation = evidence.bonus(task.category, weight)
        explanations[task.id] = explanation
        if bonus > 0:
            bonuses[task.id] = bonus
    return bonuses, explanations
