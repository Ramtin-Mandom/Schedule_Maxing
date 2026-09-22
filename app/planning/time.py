"""
app/planning/time.py

Time contract for the canonical planning package.

Rules:
    - Calendar dates use datetime.date.
    - Instants (a specific moment in time) use aware datetime objects.
    - Local scheduling (a task's wall-clock placement on a specific day) is
      expressed with an explicit IANA timezone identifier, validated through
      zoneinfo -- never assumed or silently treated as UTC.
    - Audit timestamps (created_at/updated_at, etc.) are normalized to UTC.
      Elapsed durations must be computed by subtracting aware UTC instants,
      never naive local timestamps.
    - A local day window ends unambiguously: `end_day_offset` says whether
      the window ends on the same calendar day or at the following
      midnight. "24:00" and a same-day midnight endpoint both mean
      "the following date at 00:00" -- there is no separate, ambiguous
      encoding for that instant.
    - For this milestone, only windows that stay within one local calendar
      day -- including ending exactly at the following midnight -- are
      supported. A window that would need to extend further into a
      following day, or that crosses a timezone-offset transition (e.g. a
      DST change), is rejected explicitly rather than silently mishandled.
    - Ambiguous (DST fall-back) and nonexistent (DST spring-forward) local
      times are rejected explicitly; nothing here guesses which instant was
      intended.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MINUTES_PER_DAY = 1440


class UnsupportedSchedulingWindowError(ValueError):
    """A local time window cannot yet be safely represented/scheduled."""


class AmbiguousLocalTimeError(ValueError):
    """A local wall-clock time is ambiguous (DST fall-back) or nonexistent (DST spring-forward gap)."""


def validate_timezone(tz_name: str) -> ZoneInfo:
    """
    Validate that `tz_name` is a real IANA timezone identifier.

    Raises ValueError (not KeyError/ZoneInfoNotFoundError directly) so
    callers -- including pydantic field validators -- get one consistent
    exception type for "this timezone identifier is not usable".
    """
    if not isinstance(tz_name, str) or not tz_name.strip():
        raise ValueError("timezone identifier must be a non-empty string")

    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone identifier: {tz_name!r}") from exc


def to_utc(instant: datetime) -> datetime:
    """
    Normalize an aware datetime to UTC.

    Raises ValueError for a naive datetime -- naive local input is never
    silently interpreted as UTC.
    """
    if instant.tzinfo is None:
        raise ValueError(
            "to_utc requires an aware datetime; naive datetimes are never "
            "silently treated as UTC"
        )
    return instant.astimezone(timezone.utc)


def elapsed_minutes(start: datetime, end: datetime) -> float:
    """Elapsed minutes between two aware instants, computed in UTC."""
    return (to_utc(end) - to_utc(start)).total_seconds() / 60.0


@dataclass(frozen=True)
class LocalDayWindow:
    """
    A local scheduling window anchored to one calendar day.

    `end_day_offset` disambiguates the window's end instant:
        0 -> ends on `day` itself; `end_minute` must be in (start_minute, 1440).
        1 -> ends at the following midnight (`day + 1` at 00:00); the only
             valid `end_minute` in that case is 0 (i.e. the offset already
             carries the "next day" meaning -- do not also set end_minute
             to a nonzero value, which would describe a later overnight
             endpoint).

    A caller with a legacy "minutes from midnight" end value of 1440 should
    pass end_minute=0, end_day_offset=1 (see
    app.planning.compat._window_from_legacy_minutes), so that 1440 and a
    same-day midnight both resolve to the identical, unambiguous instant.

    Only end_day_offset in (0, 1) is supported in this milestone. Anything
    needing a later overnight endpoint is rejected with
    UnsupportedSchedulingWindowError rather than silently truncated or
    misinterpreted.
    """

    day: date
    tz_name: str
    start_minute: int
    end_minute: int
    end_day_offset: int = 0

    def __post_init__(self) -> None:
        validate_timezone(self.tz_name)

        if not (0 <= self.start_minute < MINUTES_PER_DAY):
            raise ValueError(f"start_minute must be within [0, {MINUTES_PER_DAY}); got {self.start_minute}")

        if not (0 <= self.end_minute <= MINUTES_PER_DAY):
            raise ValueError(f"end_minute must be within [0, {MINUTES_PER_DAY}]; got {self.end_minute}")

        if self.end_day_offset not in (0, 1):
            raise UnsupportedSchedulingWindowError(
                "a window ending more than one day after `day` is not supported yet "
                f"(end_day_offset must be 0 or 1, got {self.end_day_offset})"
            )

        # A same-day end_minute of exactly 1440 ("24:00") means the same
        # instant as end_day_offset=1 with end_minute=0 (the following
        # midnight). Normalize to that canonical form so both encodings of
        # "the following midnight" behave identically -- there is no
        # separate, ambiguous representation for it.
        if self.end_day_offset == 0 and self.end_minute == MINUTES_PER_DAY:
            object.__setattr__(self, "end_day_offset", 1)
            object.__setattr__(self, "end_minute", 0)

        if self.end_day_offset == 0 and self.end_minute <= self.start_minute:
            raise ValueError(
                "end_minute must be after start_minute when end_day_offset is 0; "
                "use end_day_offset=1 with end_minute=0 (or end_minute=1440) to "
                "express a window ending at the following midnight"
            )

        if self.end_day_offset == 1 and self.end_minute != 0:
            raise UnsupportedSchedulingWindowError(
                "a window ending after the following midnight is not supported yet "
                "(end_day_offset=1 requires end_minute=0)"
            )

    def to_utc_instants(self) -> tuple[datetime, datetime]:
        """
        Resolve this local window to a pair of aware UTC instants.

        Raises:
            AmbiguousLocalTimeError: either endpoint falls in a DST fold
                (ambiguous) or gap (nonexistent).
            UnsupportedSchedulingWindowError: the window crosses a UTC
                offset transition (e.g. a DST change happens between the
                start and end instant), so elapsed local minutes would not
                equal elapsed real minutes. This milestone does not resolve
                that case, so it is rejected explicitly.
        """
        tz = ZoneInfo(self.tz_name)

        end_day = self.day + timedelta(days=self.end_day_offset)
        end_minute = 0 if self.end_day_offset == 1 else self.end_minute

        start_dt = _local_datetime(self.day, self.start_minute, tz)
        end_dt = _local_datetime(end_day, end_minute, tz)

        if start_dt.utcoffset() != end_dt.utcoffset():
            raise UnsupportedSchedulingWindowError(
                "this local window crosses a timezone offset transition (e.g. a "
                "daylight-saving change); scheduling across an offset change is "
                "not supported yet"
            )

        return start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc)


def _local_datetime(day: date, minute_of_day: int, tz: ZoneInfo) -> datetime:
    hour, minute = divmod(minute_of_day, 60)
    naive = datetime.combine(day, time(hour=hour, minute=minute))
    _reject_if_ambiguous_or_nonexistent(naive, tz)
    return naive.replace(tzinfo=tz)


def _reject_if_ambiguous_or_nonexistent(naive: datetime, tz: ZoneInfo) -> None:
    """
    zoneinfo resolves an ambiguous local time (DST fall-back) using `fold`
    (0 = first/earlier occurrence, 1 = second/later one) rather than
    raising, and will silently pick *some* instant for a nonexistent local
    time (DST spring-forward gap). Reject both explicitly: we were given no
    fold/offset to disambiguate, and there is no correct instant for a
    nonexistent local time.

    fold=0 and fold=1 land on different UTC offsets in *both* the
    ambiguous and the nonexistent case, so the offsets alone cannot tell
    them apart. The distinguishing test is round-trip validity: for an
    ambiguous (fall-back) time, both fold interpretations convert to UTC
    and back to the same wall-clock local time; for a nonexistent
    (spring-forward gap) time, neither fold interpretation round-trips
    back to the original wall-clock time at all.
    """
    fold0 = naive.replace(tzinfo=tz, fold=0)
    fold1 = naive.replace(tzinfo=tz, fold=1)

    if fold0.utcoffset() == fold1.utcoffset():
        return  # unambiguous, existing local time

    wall = (naive.hour, naive.minute)
    valid0 = _roundtrips_to(fold0, tz, wall)
    valid1 = _roundtrips_to(fold1, tz, wall)

    if valid0 and valid1:
        raise AmbiguousLocalTimeError(
            f"local time {naive.isoformat()} in {tz.key} is ambiguous (DST "
            "fall-back); an explicit UTC offset is required to disambiguate it"
        )

    if not valid0 and not valid1:
        raise AmbiguousLocalTimeError(
            f"local time {naive.isoformat()} in {tz.key} does not exist "
            "(DST spring-forward gap)"
        )


def _roundtrips_to(aware: datetime, tz: ZoneInfo, wall: tuple[int, int]) -> bool:
    roundtrip = aware.astimezone(timezone.utc).astimezone(tz)
    return (roundtrip.hour, roundtrip.minute) == wall


def local_minutes(instant: datetime, day: date, tz_name: str) -> int:
    """Minutes from local midnight of `day` (in `tz_name`) to `instant`, clamped to 0..1440 -- for display/export."""
    tz = ZoneInfo(tz_name)
    midnight = datetime.combine(day, time(0), tzinfo=tz)
    minutes = round((instant.astimezone(tz) - midnight).total_seconds() / 60)
    return max(0, min(MINUTES_PER_DAY, minutes))


def minutes_to_hhmm(minutes: int) -> str:
    """0..1440 minutes as "HH:MM" (1440 is "24:00", the following midnight)."""
    minutes = max(0, min(MINUTES_PER_DAY, minutes))
    if minutes == MINUTES_PER_DAY:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
