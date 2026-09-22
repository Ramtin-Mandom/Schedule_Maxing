"""
stats.py

Pure, order-independent aggregate statistics over a list of Observation
records (see app/productivity/data_prep.py). Nothing here touches the
database.

Metric definitions:
    - completion_rate / skip_rate: computed over *terminal* observations
      (status completed, skipped, or cancelled). An execution still
      scheduled, in_progress, or paused is a pending outcome, not yet a
      final result, so it is excluded from both rates' denominators rather
      than silently counted as neither (or worse, as a success).
      cancelled is terminal -- it counts toward terminal_count (and
      therefore toward both rates' shared denominator) -- but is never
      counted as completed or skipped, so it contributes to neither
      numerator. A batch of cancellations therefore dilutes both
      completion_rate and skip_rate downward without moving either
      numerator; cancelled_count is exposed separately below precisely so a
      caller (e.g. insights.py) can explain a lowered rate instead of
      misreporting it as a drop in either successful completion or an
      active decision to skip.
    - on_schedule_start_rate: fraction of observations with a known
      start_delay_minutes (see data_prep._recover_start_delay) whose
      absolute delay is within thresholds.on_schedule_tolerance_minutes.
    - duration_mae_minutes / median_actual_duration_minutes /
      median_actual_to_planned_ratio: computed only over *completed*
      observations with a recorded actual_active_duration_minutes. Per
      requirement, skipped executions are excluded here rather than treated
      as a zero-minute completion (data_prep/M1 never populate a skipped
      execution's actual_active_duration_minutes, so this falls out
      naturally from filtering on is_completed).
    - median_duration_variance_minutes: the median of (actual - planned)
      duration, in minutes, signed and computed only over completed
      observations. This complements duration_mae_minutes (unsigned, mean)
      by reporting typical *direction* and magnitude -- e.g. "tasks in this
      segment usually run about 18 minutes long" -- and is what
      insights.py's duration-variance insight is built from.
    - median_planned_duration_minutes: the median of planned_duration over
      the same completed-observation basis as median_actual_duration_minutes,
      so a caller (e.g. a "planned vs. actual" chart) can compare the two
      medians directly rather than mixing a planned figure from a different,
      larger population (which would include never-completed observations).
    - avg_focus_rating / avg_energy_rating: mean of whatever ratings were
      actually recorded; None (not 0) when no ratings exist in the segment,
      so missing feedback is never silently treated as a low/zero rating.
    - productive_active_minutes: the *sum* (not average) of completed
      observations' actual_active_duration_minutes. Unlike the averages
      above, a sum over zero observations is legitimately 0.0 -- it is not
      an invented central-tendency value, so it does not need to be None.

Every metric here is order-independent by construction (statistics.median/
mean and sum do not depend on iteration order), so results never depend on
the order rows came back from the database.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel

from app.productivity.data_prep import Observation


class EvidenceLevel(str, Enum):
    """How much a caller should trust a statistic, based on its sample size."""

    INSUFFICIENT = "insufficient"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


@dataclass(frozen=True)
class ProductivityThresholds:
    """
    Configurable minimum-sample thresholds. Defaults are conservative
    placeholders for a personal-scale dataset; tests may pass smaller values
    to exercise each evidence level without huge fixtures.
    """

    low: int = 5
    moderate: int = 15
    high: int = 30
    on_schedule_tolerance_minutes: float = 15.0


def evidence_level_for_count(observation_count: int, thresholds: ProductivityThresholds) -> EvidenceLevel:
    if observation_count < thresholds.low:
        return EvidenceLevel.INSUFFICIENT
    if observation_count < thresholds.moderate:
        return EvidenceLevel.LOW
    if observation_count < thresholds.high:
        return EvidenceLevel.MODERATE
    return EvidenceLevel.HIGH


class SegmentStats(BaseModel):
    """
    Aggregate statistics for one segment (a category, a time bucket, all history, ...).

    observation_count is the raw segment size (every status). Several metrics
    below have a smaller effective denominator -- terminal_count (completed +
    skipped only, used by completion_rate/skip_rate) and
    completed_duration_count (completed observations with a known actual
    duration, used by duration_mae_minutes/median_actual_duration_minutes/
    median_actual_to_planned_ratio/median_duration_variance_minutes) are
    exposed explicitly so a caller (e.g. insights.py) can cite the correct
    sample size for a specific metric instead of overstating it with the
    segment's total observation count.
    """

    observation_count: int
    terminal_count: int
    cancelled_count: int = 0
    completed_duration_count: int
    evidence_level: EvidenceLevel

    completion_rate: float | None
    skip_rate: float | None
    on_schedule_start_rate: float | None
    median_start_delay_minutes: float | None
    duration_mae_minutes: float | None
    median_actual_duration_minutes: float | None
    median_planned_duration_minutes: float | None
    median_actual_to_planned_ratio: float | None
    median_duration_variance_minutes: float | None
    avg_focus_rating: float | None
    avg_energy_rating: float | None
    productive_active_minutes: float


def compute_segment_stats(
    observations: list[Observation],
    thresholds: ProductivityThresholds = ProductivityThresholds(),
) -> SegmentStats:
    """Compute every required statistic for one segment of observations."""
    observation_count = len(observations)
    evidence_level = evidence_level_for_count(observation_count, thresholds)

    terminal = [observation for observation in observations if observation.is_terminal]
    completed = [observation for observation in observations if observation.is_completed]

    completion_rate = len(completed) / len(terminal) if terminal else None
    skip_rate = (
        sum(1 for observation in terminal if observation.is_skipped) / len(terminal) if terminal else None
    )

    start_delays = [
        observation.start_delay_minutes
        for observation in observations
        if observation.start_delay_minutes is not None
    ]
    on_schedule_start_rate = (
        sum(1 for delay in start_delays if abs(delay) <= thresholds.on_schedule_tolerance_minutes)
        / len(start_delays)
        if start_delays
        else None
    )
    median_start_delay = round(statistics.median(start_delays), 2) if start_delays else None

    completed_durations = [
        observation.actual_active_duration_minutes
        for observation in completed
        if observation.actual_active_duration_minutes is not None
    ]
    planned_durations_for_completed = [
        observation.planned_duration
        for observation in completed
        if observation.actual_active_duration_minutes is not None
    ]
    errors = [
        abs(observation.actual_active_duration_minutes - observation.planned_duration)
        for observation in completed
        if observation.actual_active_duration_minutes is not None
    ]
    ratios = [
        observation.actual_active_duration_minutes / observation.planned_duration
        for observation in completed
        if observation.actual_active_duration_minutes is not None and observation.planned_duration > 0
    ]
    signed_variances = [
        observation.actual_active_duration_minutes - observation.planned_duration
        for observation in completed
        if observation.actual_active_duration_minutes is not None
    ]

    duration_mae = round(statistics.mean(errors), 2) if errors else None
    median_actual_duration = round(statistics.median(completed_durations), 2) if completed_durations else None
    median_planned_duration = (
        round(statistics.median(planned_durations_for_completed), 2) if planned_durations_for_completed else None
    )
    median_ratio = round(statistics.median(ratios), 3) if ratios else None
    median_variance = round(statistics.median(signed_variances), 2) if signed_variances else None

    focus_values = [
        observation.focus_rating for observation in observations if observation.focus_rating is not None
    ]
    energy_values = [
        observation.energy_rating for observation in observations if observation.energy_rating is not None
    ]
    avg_focus = round(statistics.mean(focus_values), 2) if focus_values else None
    avg_energy = round(statistics.mean(energy_values), 2) if energy_values else None

    productive_active_minutes = round(sum(completed_durations), 2) if completed_durations else 0.0

    return SegmentStats(
        observation_count=observation_count,
        terminal_count=len(terminal),
        cancelled_count=sum(1 for observation in observations if observation.is_cancelled),
        completed_duration_count=len(completed_durations),
        evidence_level=evidence_level,
        completion_rate=round(completion_rate, 4) if completion_rate is not None else None,
        skip_rate=round(skip_rate, 4) if skip_rate is not None else None,
        on_schedule_start_rate=round(on_schedule_start_rate, 4) if on_schedule_start_rate is not None else None,
        median_start_delay_minutes=median_start_delay,
        duration_mae_minutes=duration_mae,
        median_actual_duration_minutes=median_actual_duration,
        median_planned_duration_minutes=median_planned_duration,
        median_actual_to_planned_ratio=median_ratio,
        median_duration_variance_minutes=median_variance,
        avg_focus_rating=avg_focus,
        avg_energy_rating=avg_energy,
        productive_active_minutes=productive_active_minutes,
    )
