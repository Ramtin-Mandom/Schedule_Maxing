"""
filters.py

Dimension filters for Observation lists, on top of the day-window filtering
already done by app/productivity/data_prep.py's build_observations. Kept as
a separate, pure step so the UI can offer category/tag/day-of-week/time-
bucket filters without the analytics layer (stats.py/segments.py/insights.py)
needing to know anything about filtering.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.productivity.buckets import TimeBucket
from app.productivity.data_prep import Observation


@dataclass(frozen=True)
class ObservationFilters:
    """
    All fields are optional; a None field means "no filter on this
    dimension". `days` (a day-window override) is applied earlier, at
    build_observations time -- see app/productivity/reporting.py -- since it
    needs the repository's day-window cutoff logic, not just the already-
    built Observation list.
    """

    days: int | None = None
    category: str | None = None
    tag: str | None = None
    day_of_week: str | None = None
    time_bucket: TimeBucket | None = None


def apply_filters(observations: list[Observation], filters: ObservationFilters | None) -> list[Observation]:
    """Return the subset of observations matching every set dimension filter. None (default) is a no-op."""
    if filters is None:
        return observations

    result = observations
    if filters.category is not None:
        result = [observation for observation in result if observation.category == filters.category]
    if filters.tag is not None:
        result = [observation for observation in result if observation.tag == filters.tag]
    if filters.day_of_week is not None:
        result = [observation for observation in result if observation.day_of_week == filters.day_of_week]
    if filters.time_bucket is not None:
        result = [observation for observation in result if observation.time_bucket == filters.time_bucket]

    return result
