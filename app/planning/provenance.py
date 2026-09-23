"""
app/planning/provenance.py

Restart-safe provenance of a saved schedule (Milestone 3): enough persisted
metadata to tell, after the app is closed and reopened, whether the saved
schedule of a date is still *current* for today's inputs or *stale*.

Before this, "current vs. out of date" lived only in memory
(app.planning.service.SelectedDayState, keyed by an in-memory
AllocationResult.id), so every restored schedule had to be shown as out of
date. Now every successful generation of a date -- including one that
placed nothing -- saves a GenerationRecord atomically with that date's
placements (PlanningService.reschedule_range).

The record stores two digests:

    - `fingerprint` (inputs_fingerprint): a SHA-256 over a canonical JSON
      document of everything the generation read -- the allocation range and
      its scope, the timezone, every task in that range's scope (content
      only: audit fields such as updated_at/version are excluded, so saving
      unchanged content never makes a schedule stale), their dependency ids,
      the fixed blocks of every date of the range, the fully resolved
      DayPreferences of every date of the range (YAML -> user -> date,
      including optimizer_mode, i.e. the engine mode), and the resolved state
      of every dependency outside the range (see
      app/planning/external_dependencies.py). The whole range is included,
      not just the one date, because allocation decides which date each task
      lands on from the range as a whole: an edit to any task of the range
      can move work onto or off any of its dates. This deliberately
      over-invalidates (like the in-memory policy it replaces) but can never
      leave a stale schedule looking current. FINGERPRINT_VERSION is part of
      the digest, so changing this recipe invalidates old records instead of
      silently comparing different recipes.
    - `placements_digest`: a digest of the date's saved placements (ids,
      task ids, exact instants, versions) as committed with the record, so
      any later change to that date's placements (a reset, an import, a
      rescheduling of another range superseding one of them) also marks it
      stale.

classify_generation compares a record with the current inputs/placements:
    - no record, but saved placements -> STALE / NO_PROVENANCE (placements
      saved before v4, or by something other than a generation run;
      explicitly unknown, never assumed current);
    - fingerprint differs -> STALE / INPUTS_CHANGED;
    - placement digest differs -> STALE / PLACEMENTS_CHANGED;
    - otherwise -> GENERATED (current), including a successful empty day.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import date as date_
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from app.planning.models import FixedBlock, ScheduledTask, Task
from app.planning.preferences import DayPreferences, OptimizerMode

#: Bump whenever the fingerprint recipe below changes.
FINGERPRINT_VERSION = 1

#: Fields that describe a record's bookkeeping rather than its scheduling content.
_NON_CONTENT_FIELDS = {"created_at", "updated_at", "version", "deleted_at", "user_id"}


class StaleReason(str, Enum):
    #: Saved placements exist but no generation record describes them.
    NO_PROVENANCE = "no_provenance"
    #: Something the generation depended on changed since it ran.
    INPUTS_CHANGED = "inputs_changed"
    #: The saved placements of the date changed since the generation saved them.
    PLACEMENTS_CHANGED = "placements_changed"
    #: A newer in-memory allocation run superseded the one it came from.
    SUPERSEDED_ALLOCATION = "superseded_allocation"


class GenerationRecord(BaseModel):
    """The persisted provenance of one date's saved schedule (one live record per date)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: uuid.UUID | None = None
    planned_date: date_
    timezone: str
    engine_mode: OptimizerMode
    range_start: date_
    range_end: date_
    #: The app.planning.application.RangeScope value the range's tasks were loaded with.
    range_scope: str
    allocation_id: uuid.UUID
    fingerprint: str
    fingerprint_version: int = FINGERPRINT_VERSION
    placements_digest: str
    placement_count: int = Field(ge=0)
    unscheduled_count: int = Field(ge=0)
    total_score: float
    generated_at: datetime

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @field_validator("generated_at", "created_at", "updated_at", "deleted_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be aware datetimes (include a UTC offset)")
        return value.astimezone(timezone.utc)


def _content(model: Task | FixedBlock) -> dict:
    return model.model_dump(mode="json", exclude=_NON_CONTENT_FIELDS)


def _digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inputs_fingerprint(
    *,
    start_date: date_,
    end_date: date_,
    scope: str,
    timezone_name: str,
    tasks: Iterable[Task],
    fixed_blocks: Iterable[FixedBlock],
    preferences_by_date: Mapping[date_, DayPreferences],
    external_dependencies: Mapping[uuid.UUID, object],
) -> str:
    """
    The documented inputs fingerprint (see the module docstring). Order
    independent: collections are sorted by id/date. `external_dependencies`
    values are app.planning.external_dependencies.ExternalDependency (typed
    loosely here to keep this module free of that import).
    """
    payload = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "range": [start_date.isoformat(), end_date.isoformat(), scope],
        "timezone": timezone_name,
        "tasks": sorted((_content(task) for task in tasks), key=lambda item: item["id"]),
        "fixed_blocks": sorted(
            (_content(block) for block in fixed_blocks), key=lambda item: (item["planned_date"], item["id"])
        ),
        "preferences": {
            day.isoformat(): preferences.model_dump(mode="json")
            for day, preferences in sorted(preferences_by_date.items())
        },
        "external_dependencies": {
            str(task_id): dependency.fingerprint_payload()
            for task_id, dependency in sorted(external_dependencies.items(), key=lambda item: str(item[0]))
        },
    }
    return _digest(payload)


def placements_digest(placements: Iterable[ScheduledTask]) -> str:
    """A digest of one date's saved placements (id, task, exact instants, version)."""
    rows = sorted(
        [
            str(placement.id),
            str(placement.task_id),
            placement.planned_start.astimezone(timezone.utc).isoformat(),
            placement.planned_end.astimezone(timezone.utc).isoformat(),
            placement.version,
        ]
        for placement in placements
    )
    return _digest(rows)


def classify_generation(
    record: GenerationRecord | None,
    *,
    has_placements: bool,
    current_fingerprint: str | None,
    current_placements_digest: str,
) -> tuple[bool | None, StaleReason | None]:
    """
    (is_current, stale_reason) for one date. is_current is None when there
    is nothing to classify (no record and no placements). current_fingerprint
    may be None when the record's inputs could not be recomputed (e.g. its
    range can no longer be resolved); that is treated as INPUTS_CHANGED.
    """
    if record is None:
        return (False, StaleReason.NO_PROVENANCE) if has_placements else (None, None)
    if record.fingerprint_version != FINGERPRINT_VERSION or current_fingerprint != record.fingerprint:
        return False, StaleReason.INPUTS_CHANGED
    if current_placements_digest != record.placements_digest:
        return False, StaleReason.PLACEMENTS_CHANGED
    return True, None
