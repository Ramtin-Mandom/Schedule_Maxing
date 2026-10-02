"""The calendar of a recurring series (app/planning/recurrence.py, docs/recurrence.md): daily/weekly/monthly
intervals and selectors, Monday-anchored weeks, skipped (never clamped) month days, leap years, inclusive end
dates, counts from the anchor independent of the requested window, arithmetic seeking far into the future
(checked against brute force), the work budget, deterministic identity, cadence compatibility and DST days."""

from __future__ import annotations

import random
import uuid
from datetime import date, timedelta

import pytest

from app.planning.models import RecurrenceSpec
from app.planning.recurrence import (
    MAX_SLOT_SCAN,
    RecurrenceBudgetError,
    SeriesNotConfiguredError,
    SeriesRule,
    cadence_problem,
    occurrence_task_id,
    offset_transition,
)


def rule(frequency: str, start: date, tz: str = "UTC", **fields) -> SeriesRule:
    return SeriesRule.of(RecurrenceSpec(frequency=frequency, start_date=start, timezone=tz, **fields))


def slots(series: SeriesRule, first: date, last: date) -> list[date]:
    return list(series.slots(first, last))


def brute_force(series: SeriesRule, first: date, last: date) -> list[date]:
    """Day by day from the anchor: the definition, without any arithmetic seeking."""
    found, index, day = [], 0, series.start
    while day <= last and (series.end is None or day <= series.end):
        if series.matches_cadence(day):
            if series.count is not None and index >= series.count:
                break
            if day >= first:
                found.append(day)
            index += 1
        day += timedelta(days=1)
    return found


def test_daily_every_n_days_from_the_anchor_never_before_it() -> None:
    series = rule("daily", date(2026, 1, 3), interval=3)
    assert slots(series, date(2025, 12, 25), date(2026, 1, 13)) == [
        date(2026, 1, 3), date(2026, 1, 6), date(2026, 1, 9), date(2026, 1, 12)]


def test_weekly_weeks_are_monday_anchored_and_the_anchor_week_starts_at_the_anchor() -> None:
    # Anchor Wednesday 2026-01-07; every 2 weeks on Mon, Wed, Fri: Monday 5 Jan precedes the anchor.
    series = rule("weekly", date(2026, 1, 7), interval=2, weekdays=[0, 2, 4])
    assert slots(series, date(2026, 1, 1), date(2026, 1, 31)) == [
        date(2026, 1, 7), date(2026, 1, 9),  # the anchor's week, from the anchor on
        date(2026, 1, 19), date(2026, 1, 21), date(2026, 1, 23),  # two Monday-weeks later
    ]
    # No weekdays selected: the anchor's own weekday.
    assert slots(rule("weekly", date(2026, 1, 7)), date(2026, 1, 1), date(2026, 1, 22)) == [
        date(2026, 1, 7), date(2026, 1, 14), date(2026, 1, 21)]


def test_monthly_skips_months_without_the_day_instead_of_clamping() -> None:
    series = rule("monthly", date(2026, 1, 31))
    assert slots(series, date(2026, 1, 1), date(2026, 12, 31)) == [
        date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31), date(2026, 7, 31), date(2026, 8, 31),
        date(2026, 10, 31), date(2026, 12, 31)]
    every_quarter = rule("monthly", date(2026, 2, 10), interval=3, day_of_month=30)
    # February's month is a cadence month, but has no 30th; an explicit day before the anchor's day is skipped.
    assert slots(every_quarter, date(2026, 1, 1), date(2026, 12, 31)) == [
        date(2026, 5, 30), date(2026, 8, 30), date(2026, 11, 30)]


def test_february_29_exists_only_in_leap_years() -> None:
    series = rule("monthly", date(2023, 1, 29), interval=12)  # every 12 months from January 29
    assert slots(series, date(2023, 1, 1), date(2030, 12, 31)) == [date(2023 + n, 1, 29) for n in range(8)]
    leap = rule("monthly", date(2024, 2, 29), interval=12)
    assert slots(leap, date(2024, 1, 1), date(2040, 12, 31)) == [
        date(2024, 2, 29), date(2028, 2, 29), date(2032, 2, 29), date(2036, 2, 29), date(2040, 2, 29)]
    century = rule("monthly", date(2096, 2, 29), interval=12)
    assert slots(century, date(2096, 1, 1), date(2104, 12, 31)) == [date(2096, 2, 29), date(2104, 2, 29)]  # not 2100


def test_count_counts_valid_slots_from_the_anchor_whatever_window_is_asked() -> None:
    series = rule("monthly", date(2026, 1, 31), count=4)  # Jan, Mar, May, Jul 31 -- Feb/Apr/Jun have no slot
    assert slots(series, date(2026, 1, 1), date(2027, 12, 31)) == [
        date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31), date(2026, 7, 31)]
    assert slots(series, date(2026, 5, 1), date(2026, 12, 31)) == [date(2026, 5, 31), date(2026, 7, 31)]
    assert slots(series, date(2026, 8, 1), date(2026, 12, 31)) == []  # the count was used up before the window
    assert series.slot_index(date(2026, 7, 31)) == 3 and not series.is_slot(date(2026, 8, 31))


def test_end_date_is_inclusive_and_an_end_before_the_start_retires_the_series() -> None:
    assert slots(rule("daily", date(2026, 1, 1), end_date=date(2026, 1, 3)), date(2026, 1, 1), date(2026, 1, 9)) == [
        date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    assert slots(rule("daily", date(2026, 1, 5), end_date=date(2026, 1, 4)), date(2026, 1, 1), date(2026, 1, 31)) == []


def test_count_and_end_date_stay_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        RecurrenceSpec(frequency="daily", start_date=date(2026, 1, 1), timezone="UTC", count=2,
                       end_date=date(2026, 2, 1))


def test_a_rule_without_start_date_and_time_zone_needs_configuration() -> None:
    spec = RecurrenceSpec(frequency="weekly")
    assert not spec.configured
    with pytest.raises(SeriesNotConfiguredError):
        SeriesRule.of(spec)
    with pytest.raises(ValueError):  # set together or not at all
        RecurrenceSpec(frequency="daily", start_date=date(2026, 1, 1))


def test_sparse_far_future_requests_seek_arithmetically_and_agree_with_brute_force() -> None:
    ancient = rule("monthly", date(1990, 1, 31), interval=7, count=500)
    window = (date(2215, 1, 1), date(2215, 12, 31))
    assert slots(ancient, *window) == brute_force(ancient, *window)  # 225 years of months, never scanned per day
    daily = rule("daily", date(1990, 6, 1), interval=13)
    assert slots(daily, date(2090, 3, 1), date(2090, 4, 30)) == brute_force(daily, date(2090, 3, 1), date(2090, 4, 30))


def test_slot_indexes_and_windows_agree_with_brute_force_for_many_rules() -> None:
    rng = random.Random(1234)
    for _ in range(300):
        frequency = rng.choice(["daily", "weekly", "monthly"])
        start = date(2000, 1, 1) + timedelta(days=rng.randrange(9000))
        fields: dict = {"interval": rng.randint(1, 5)}
        if frequency == "weekly" and rng.random() < 0.7:
            fields["weekdays"] = rng.sample(range(7), rng.randint(1, 4))
        if frequency == "monthly" and rng.random() < 0.6:
            fields["day_of_month"] = rng.randint(1, 31)
        if rng.random() < 0.4:
            fields["count"] = rng.randint(1, 40)
        elif rng.random() < 0.4:
            fields["end_date"] = start + timedelta(days=rng.randrange(1500))
        series = rule(frequency, start, **fields)
        first = start + timedelta(days=rng.randrange(-30, 1200))
        last = first + timedelta(days=rng.randrange(62))
        assert slots(series, first, last) == brute_force(series, first, last), (frequency, start, fields, first)
        everything = brute_force(series, start, start + timedelta(days=1600))
        for index, day in enumerate(everything[:15]):
            assert series.slot_index(day) == index and series.is_slot(day)


def test_enumeration_is_bounded_by_the_work_budget() -> None:
    every_day = rule("daily", date(2000, 1, 1))
    with pytest.raises(RecurrenceBudgetError):
        list(every_day.slots(date(2000, 1, 1), date(2000, 1, 1) + timedelta(days=MAX_SLOT_SCAN + 5)))


def test_occurrence_ids_derive_from_series_and_original_slot_only() -> None:
    series_id = uuid.uuid4()
    assert occurrence_task_id(series_id, date(2026, 5, 1)) == occurrence_task_id(series_id, date(2026, 5, 1))
    assert occurrence_task_id(series_id, date(2026, 5, 1)) != occurrence_task_id(series_id, date(2026, 5, 2))
    assert occurrence_task_id(series_id, date(2026, 5, 1)) != occurrence_task_id(uuid.uuid4(), date(2026, 5, 1))


def test_recurring_dependencies_need_the_same_zone_and_slot_compatible_cadences() -> None:
    weekly = rule("weekly", date(2026, 1, 5), weekdays=[0, 2], tz="Europe/Berlin")
    assert cadence_problem(weekly, rule("daily", date(2025, 12, 1), tz="Europe/Berlin")) is None
    assert cadence_problem(weekly, rule("weekly", date(2026, 1, 12), weekdays=[0, 2, 4], tz="Europe/Berlin")) is None
    assert "time zone" in cadence_problem(weekly, rule("daily", date(2025, 12, 1), tz="UTC"))
    assert cadence_problem(weekly, rule("monthly", date(2026, 1, 5), tz="Europe/Berlin")) is not None
    assert cadence_problem(weekly, rule("weekly", date(2026, 1, 5), weekdays=[0], tz="Europe/Berlin")) is not None
    biweekly = rule("weekly", date(2026, 1, 5), interval=2, weekdays=[0], tz="UTC")
    assert cadence_problem(biweekly, rule("weekly", date(2026, 1, 12), interval=2, weekdays=[0], tz="UTC")) is not None
    assert cadence_problem(rule("daily", date(2026, 1, 3), interval=4), rule("daily", date(2026, 1, 1), interval=2)) is None


def test_slots_are_local_dates_and_dst_days_are_detectable() -> None:
    assert offset_transition(date(2026, 3, 8), "America/New_York")  # spring forward
    assert offset_transition(date(2026, 11, 1), "America/New_York")  # fall back
    assert not offset_transition(date(2026, 3, 9), "America/New_York")
    series = rule("daily", date(2026, 3, 7), tz="America/New_York")
    assert slots(series, date(2026, 3, 7), date(2026, 3, 9)) == [date(2026, 3, 7), date(2026, 3, 8), date(2026, 3, 9)]
