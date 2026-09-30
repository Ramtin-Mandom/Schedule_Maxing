"""The computer's clock and time zone (app/planning/system_clock.py): which IANA zone the
machine is set to (TZ, the Windows registry name, /etc/localtime, an offset fallback, never
raising), today's date in that zone -- including the Vancouver-near-midnight case a UTC
default got wrong -- and the settings default that uses it."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.planning.system_clock import (
    FALLBACK_TIMEZONE,
    WINDOWS_TO_IANA,
    _offset_zone,
    _posix_zone_name,
    date_status,
    detect_system_timezone,
    is_valid_timezone,
    local_today,
)
from config import settings


def _detect(**overrides) -> str:
    options = dict(environ={}, platform="linux", windows_key_name=lambda: None, posix_zone_name=lambda: None,
                   utc_offset_seconds=lambda: 0)
    options.update(overrides)
    return detect_system_timezone(**options)


# -----------------------------------------------------------------------------
# Today's date
# -----------------------------------------------------------------------------


def test_today_is_the_local_calendar_date_not_utcs() -> None:
    # 11:30 PM on Sep 27 in Vancouver is already 6:30 AM on Sep 28 in UTC.
    evening = datetime(2026, 9, 28, 6, 30, tzinfo=timezone.utc)
    assert local_today("America/Vancouver", evening) == date(2026, 9, 27)
    assert local_today("UTC", evening) == date(2026, 9, 28)
    # Just after local midnight it is the next day in Vancouver.
    assert local_today("America/Vancouver", datetime(2026, 9, 28, 7, 1, tzinfo=timezone.utc)) == date(2026, 9, 28)
    # Across the east of the date line, the local date is ahead of UTC's.
    assert local_today("Pacific/Auckland", datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)) == date(2026, 9, 28)


def test_today_uses_the_system_clock_and_refuses_naive_instants() -> None:
    assert local_today("UTC") == datetime.now(timezone.utc).date()
    with pytest.raises(ValueError):
        local_today("UTC", datetime(2026, 9, 27, 12, 0))


def test_dates_are_past_today_or_future() -> None:
    today = date(2026, 9, 27)
    assert [date_status(date(2026, 9, day), today) for day in (26, 27, 28)] == ["past", "today", "future"]


# -----------------------------------------------------------------------------
# Detecting the computer's zone
# -----------------------------------------------------------------------------


def test_the_tz_variable_wins_when_it_names_a_real_zone() -> None:
    assert _detect(environ={"TZ": "America/Vancouver"}, posix_zone_name=lambda: "Europe/Berlin") == "America/Vancouver"
    assert _detect(environ={"TZ": ":Europe/Paris"}) == "Europe/Paris"
    assert _detect(environ={"TZ": "not/a_zone"}, posix_zone_name=lambda: "Europe/Berlin") == "Europe/Berlin"


def test_windows_registry_names_map_to_iana_zones() -> None:
    assert _detect(platform="win32", windows_key_name=lambda: "Pacific Standard Time") == "America/Los_Angeles"
    assert _detect(platform="win32", windows_key_name=lambda: "Eastern Standard Time") == "America/New_York"
    assert _detect(platform="win32", windows_key_name=lambda: "W. Europe Standard Time") == "Europe/Berlin"
    # Every mapped zone really exists in the time zone database.
    assert [name for name in WINDOWS_TO_IANA.values() if not is_valid_timezone(name)] == []


def test_posix_zone_comes_from_the_localtime_link(tmp_path: Path) -> None:
    zoneinfo = tmp_path / "usr" / "share" / "zoneinfo" / "America"
    zoneinfo.mkdir(parents=True)
    (zoneinfo / "Vancouver").write_bytes(b"TZif")
    link = tmp_path / "localtime"
    try:
        link.symlink_to(zoneinfo / "Vancouver")
    except OSError:
        pytest.skip("symbolic links are not available here")
    assert _posix_zone_name(link, tmp_path / "missing") == "America/Vancouver"


def test_posix_zone_falls_back_to_etc_timezone(tmp_path: Path) -> None:
    (tmp_path / "timezone").write_text("America/Toronto\n", encoding="utf-8")
    assert _posix_zone_name(tmp_path / "no-localtime", tmp_path / "timezone") == "America/Toronto"


def test_unknown_systems_fall_back_to_the_current_offset_then_utc() -> None:
    # Windows with a name the table does not know: the current offset gives a correct "today".
    assert _detect(platform="win32", windows_key_name=lambda: "Mars Standard Time",
                   utc_offset_seconds=lambda: -7 * 3600) == "Etc/GMT+7"
    assert _offset_zone(2 * 3600) == "Etc/GMT-2" and _offset_zone(0) == FALLBACK_TIMEZONE
    assert _offset_zone(5 * 3600 + 1800) is None  # no whole-hour Etc zone for +05:30
    assert _detect(utc_offset_seconds=lambda: 5 * 3600 + 1800) == FALLBACK_TIMEZONE


def test_detection_never_raises() -> None:
    def broken():
        raise OSError("registry unavailable")

    assert _detect(platform="win32", windows_key_name=broken, utc_offset_seconds=broken) == FALLBACK_TIMEZONE
    assert is_valid_timezone(detect_system_timezone())  # this machine's real zone is a valid IANA zone


def test_the_default_planning_zone_is_the_computers_unless_overridden() -> None:
    assert settings.resolve_default_timezone({"SCHEDULE_MAXING_TIMEZONE": "Asia/Tokyo"}) == "Asia/Tokyo"
    assert settings.resolve_default_timezone({"TZ": "America/Vancouver"}) == "America/Vancouver"
    assert is_valid_timezone(settings.DEFAULT_TIMEZONE)
