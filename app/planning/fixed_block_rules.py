"""
app/planning/fixed_block_rules.py

The write-time invariants of a fixed block as a *scheduling input*
(Milestone 4 preflight). A fixed block is a hard constraint the day engine
schedules around (app/optimizer.py, _validate_and_place_canonical_fixed_blocks);
a block the engine would refuse must not be storable in the first place.
Every write path that stores a live fixed block checks these rules at its
write boundary, inside the transaction that writes it:

    - local: PlanningService.create/update/save_fixed_block,
      set_fixed_blocks_for_date, the legacy CSV import (apply_import) and the
      canonical CSV import (apply_record_batch);
    - server: the fixed-block resource's validation in backend/resources.py,
      which both the REST endpoints and POST /sync/push go through (the
      shared Mutator, under the user's change-log lock).

Rules (pure: nothing here reads storage; callers pass what they loaded):

    invalid_interval       planned_end must be after planned_start.
    sub_minute_precision   both instants are whole minutes (the day engine
                           works in integer minutes and refuses to round).
    date_mismatch          the block starts on its planned_date in its own
                           timezone.
    outside_day_window     the block lies inside that date's *effective*
                           day window -- resolved with the same layers
                           scheduling uses (built-in defaults -> YAML template
                           -> user layer -> that date's layer; see
                           app/planning/preferences.py). Adjacent to the
                           window's edges is inside.
    unsupported_day_window an endpoint of the effective window is ambiguous or
                           nonexistent local time (DST), so containment cannot
                           be decided. A window that merely spans a DST change
                           is checked by its endpoint instants; generating that
                           date is still refused by the day engine.
    overlap                no other live fixed block of the same owner overlaps
                           it (half-open intervals by instant: a block ending
                           at 10:13 and one starting at 10:13 are fine). The
                           edited record itself is excluded, and blocks dated
                           the day before and after are compared too, so blocks
                           in different timezones cannot overlap unnoticed.

What is deliberately *not* re-checked (historical semantics, kept
explicitly rather than rounded or rewritten): a tombstone; an update that
leaves the block's interval (planned_date, timezone, planned_start,
planned_end) unchanged -- e.g. relabelling a block stored before these rules
existed; a record identical to the stored one in a canonical import; and a
pulled sync record, which the server already validated (app/sync applies
server state as-is). Changing preferences never invalidates stored blocks;
the day engine reports such a date when it is generated.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date as date_
from datetime import datetime, timedelta, timezone

from app.planning.errors import InvalidEntityError
from app.planning.models import FixedBlock
from app.planning.preferences import DayPreferences
from app.planning.time import AmbiguousLocalTimeError, UnsupportedSchedulingWindowError, local_date_of, minutes_to_hhmm

#: Blocks dated this many days before/after a block are also compared for overlap.
OVERLAP_NEIGHBORHOOD_DAYS = 1


class FixedBlockRuleViolation(InvalidEntityError):
    """A fixed block breaks one of this module's rules; `code` names which one."""

    def __init__(self, code: str, message: str, *, conflicting: FixedBlock | None = None) -> None:
        self.code = code
        self.conflicting = conflicting
        super().__init__(message)


def interval_changed(stored: FixedBlock, incoming: FixedBlock) -> bool:
    """Whether a write changes the block's scheduling interval (the only fields these rules judge)."""
    return (
        stored.planned_date != incoming.planned_date
        or stored.timezone != incoming.timezone
        or stored.planned_start != incoming.planned_start
        or stored.planned_end != incoming.planned_end
    )


def neighborhood(day: date_) -> tuple[date_, date_]:
    """The inclusive date range whose stored blocks a block on `day` is compared with."""
    span = timedelta(days=OVERLAP_NEIGHBORHOOD_DAYS)
    start = day - span if day > date_.min + span else date_.min
    end = day + span if day < date_.max - span else date_.max
    return start, end


def _whole_minute(instant: datetime) -> bool:
    utc = instant.astimezone(timezone.utc)
    return utc.second == 0 and utc.microsecond == 0


def _label(block: FixedBlock) -> str:
    return f"fixed block {block.label!r}"


def check_interval(block: FixedBlock, preferences: DayPreferences) -> None:
    """
    Raise FixedBlockRuleViolation unless `block`'s interval is a valid
    scheduling input on its date. `preferences` must be the effective
    preferences of block.planned_date resolved in block.timezone.
    """
    if preferences.date != block.planned_date or preferences.timezone != block.timezone:
        raise ValueError("check_interval needs the preferences of the block's own date and timezone")
    if block.planned_end <= block.planned_start:
        raise FixedBlockRuleViolation("invalid_interval", f"{_label(block)} must end after it starts.")
    if not (_whole_minute(block.planned_start) and _whole_minute(block.planned_end)):
        raise FixedBlockRuleViolation(
            "sub_minute_precision",
            f"{_label(block)} must start and end on whole minutes (no seconds); scheduling works in whole minutes "
            "and never rounds a stored time.",
        )
    starts_on = local_date_of(block.planned_start, block.timezone)
    if starts_on != block.planned_date:
        raise FixedBlockRuleViolation(
            "date_mismatch",
            f"{_label(block)} is dated {block.planned_date} but starts on {starts_on} in {block.timezone}.",
        )
    try:
        window_start, window_end = preferences.to_local_day_window().endpoint_instants()
    except (AmbiguousLocalTimeError, UnsupportedSchedulingWindowError) as error:
        raise FixedBlockRuleViolation(
            "unsupported_day_window",
            f"The day window of {block.planned_date} in {block.timezone} cannot be resolved ({error}); "
            f"{_label(block)} cannot be checked against it.",
        ) from error
    if block.planned_start < window_start or block.planned_end > window_end:
        window = preferences.day_window
        end_minute = 1440 if window.end_day_offset == 1 else window.end_minute
        raise FixedBlockRuleViolation(
            "outside_day_window",
            f"{_label(block)} lies outside the day window of {block.planned_date} "
            f"({minutes_to_hhmm(window.start_minute)}-{minutes_to_hhmm(end_minute)} in {block.timezone}).",
        )


def find_overlap(block: FixedBlock, others: Iterable[FixedBlock]) -> FixedBlock | None:
    """The first live block among `others` (ordered by start, then id) whose interval overlaps `block`'s; never itself."""
    candidates = sorted(
        (other for other in others if other.id != block.id and other.deleted_at is None),
        key=lambda other: (other.planned_start, str(other.id)),
    )
    for other in candidates:
        if block.planned_start < other.planned_end and other.planned_start < block.planned_end:
            return other
    return None


def check_no_overlap(block: FixedBlock, others: Iterable[FixedBlock], *, saved: bool = True) -> None:
    other = find_overlap(block, others)
    if other is not None:
        which = "the saved fixed block" if saved else "fixed block"
        raise FixedBlockRuleViolation(
            "overlap",
            f"{_label(block)} overlaps {which} {other.label!r} ({other.planned_date}, "
            f"{other.planned_start.isoformat()} to {other.planned_end.isoformat()}).",
            conflicting=other,
        )
