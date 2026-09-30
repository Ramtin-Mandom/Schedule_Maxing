"""
app/planning/system_clock.py

The computer's own clock and time zone, standard library only: which IANA
time zone this machine is set to, and today's real calendar date in a zone.

The desktop app plans in one IANA zone (config/settings.DEFAULT_TIMEZONE).
Before this module that default was hard-coded to UTC, so a Vancouver user
at 6 PM saw tomorrow's date. detect_system_timezone() finds the zone the
operating system is set to instead:

    1. the TZ environment variable, when it names an IANA zone;
    2. Windows: the registry's TimeZoneKeyName (e.g. "Pacific Standard
       Time"), mapped to IANA with CLDR's windowsZones table (territory 001);
    3. elsewhere: /etc/localtime's link target (.../zoneinfo/America/Vancouver),
       else /etc/timezone;
    4. a whole-hour "Etc/GMT+N" zone from the current UTC offset (correct
       today, without daylight-saving rules), else "UTC".

Every candidate is validated with zoneinfo before it is returned. Nothing
here reads calendar events; it only knows the date and the zone.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FALLBACK_TIMEZONE = "UTC"

#: Windows time zone key names -> IANA zones (CLDR windowsZones.xml, territory "001").
WINDOWS_TO_IANA: dict[str, str] = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Alaskan Standard Time": "America/Anchorage",
    "UTC-09": "Etc/GMT+9",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "UTC-08": "Etc/GMT+8",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver",
    "Yukon Standard Time": "America/Whitehorse",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Easter Island Standard Time": "Pacific/Easter",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Cuba Standard Time": "America/Havana",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Paraguay Standard Time": "America/Asuncion",
    "Atlantic Standard Time": "America/Halifax",
    "Venezuela Standard Time": "America/Caracas",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Buenos_Aires",
    "Greenland Standard Time": "America/Godthab",
    "Montevideo Standard Time": "America/Montevideo",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Saint Pierre Standard Time": "America/Miquelon",
    "Bahia Standard Time": "America/Bahia",
    "UTC-02": "Etc/GMT+2",
    "Mid-Atlantic Standard Time": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "Etc/UTC",
    "Coordinated Universal Time": "Etc/UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "Jordan Standard Time": "Asia/Amman",
    "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Syria Standard Time": "Asia/Damascus",
    "West Bank Standard Time": "Asia/Hebron",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kiev",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Sudan Standard Time": "Africa/Juba",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Sudan Standard Time": "Africa/Khartoum",
    "Libya Standard Time": "Africa/Tripoli",
    "Namibia Standard Time": "Africa/Windhoek",
    "Arabic Standard Time": "Asia/Baghdad",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arab Standard Time": "Asia/Riyadh",
    "Belarus Standard Time": "Europe/Minsk",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Volgograd Standard Time": "Europe/Volgograd",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Astrakhan Standard Time": "Europe/Astrakhan",
    "Azerbaijan Standard Time": "Asia/Baku",
    "Russia Time Zone 3": "Europe/Samara",
    "Mauritius Standard Time": "Indian/Mauritius",
    "Saratov Standard Time": "Europe/Saratov",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Caucasus Standard Time": "Asia/Yerevan",
    "Afghanistan Standard Time": "Asia/Kabul",
    "West Asia Standard Time": "Asia/Tashkent",
    "Qyzylorda Standard Time": "Asia/Qyzylorda",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Calcutta",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Katmandu",
    "Central Asia Standard Time": "Asia/Almaty",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Omsk Standard Time": "Asia/Omsk",
    "Myanmar Standard Time": "Asia/Rangoon",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Altai Standard Time": "Asia/Barnaul",
    "W. Mongolia Standard Time": "Asia/Hovd",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Tomsk Standard Time": "Asia/Tomsk",
    "China Standard Time": "Asia/Shanghai",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth",
    "Taipei Standard Time": "Asia/Taipei",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar",
    "Aus Central W. Standard Time": "Australia/Eucla",
    "Transbaikal Standard Time": "Asia/Chita",
    "Tokyo Standard Time": "Asia/Tokyo",
    "North Korea Standard Time": "Asia/Pyongyang",
    "Korea Standard Time": "Asia/Seoul",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "Tasmania Standard Time": "Australia/Hobart",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "Bougainville Standard Time": "Pacific/Bougainville",
    "Russia Time Zone 10": "Asia/Srednekolymsk",
    "Magadan Standard Time": "Asia/Magadan",
    "Norfolk Standard Time": "Pacific/Norfolk",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "Russia Time Zone 11": "Asia/Kamchatka",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC+12": "Etc/GMT-12",
    "Fiji Standard Time": "Pacific/Fiji",
    "Chatham Islands Standard Time": "Pacific/Chatham",
    "UTC+13": "Etc/GMT-13",
    "Tonga Standard Time": "Pacific/Tongatapu",
    "Samoa Standard Time": "Pacific/Apia",
    "Line Islands Standard Time": "Pacific/Kiritimati",
}

_WINDOWS_TZ_KEY = r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation"


def is_valid_timezone(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _windows_key_name() -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_TZ_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except OSError:
        return None
    # Some Windows versions pad the value with NUL characters.
    return str(value).split("\x00", 1)[0].strip() or None


def _posix_zone_name(localtime: Path = Path("/etc/localtime"), timezone_file: Path = Path("/etc/timezone")) -> str | None:
    try:
        target = os.path.realpath(localtime)
    except OSError:
        target = ""
    marker = "zoneinfo" + os.sep
    if marker in target:
        name = target.split(marker, 1)[1].replace(os.sep, "/")
        # Some distributions link into zoneinfo/posix/... or zoneinfo/right/...
        for prefix in ("posix/", "right/"):
            name = name.removeprefix(prefix)
        return name
    try:
        return timezone_file.read_text(encoding="utf-8").strip().split()[0]
    except (OSError, IndexError):
        return None


def _offset_zone(utc_offset_seconds: int) -> str | None:
    """A whole-hour UTC offset as an Etc/GMT zone (whose sign is inverted by POSIX convention)."""
    if utc_offset_seconds % 3600:
        return None
    hours = utc_offset_seconds // 3600
    if hours == 0:
        return FALLBACK_TIMEZONE
    return f"Etc/GMT{'-' if hours > 0 else '+'}{abs(hours)}"


def detect_system_timezone(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    windows_key_name: Callable[[], str | None] = _windows_key_name,
    posix_zone_name: Callable[[], str | None] = _posix_zone_name,
    utc_offset_seconds: Callable[[], int] = lambda: -time.timezone if not time.localtime().tm_isdst else -time.altzone,
) -> str:
    """The IANA zone this computer is set to (see the module docstring); never raises."""
    environ = os.environ if environ is None else environ
    platform = platform or sys.platform

    tz_variable = (environ.get("TZ") or "").strip().lstrip(":")
    if is_valid_timezone(tz_variable):
        return tz_variable

    candidate = None
    try:
        if platform.startswith("win"):
            key_name = windows_key_name()
            candidate = WINDOWS_TO_IANA.get(key_name or "")
        else:
            candidate = posix_zone_name()
    except Exception:  # noqa: BLE001 - detection must never stop the app from starting
        candidate = None
    if is_valid_timezone(candidate):
        return candidate

    try:
        offset_zone = _offset_zone(utc_offset_seconds())
    except Exception:  # noqa: BLE001
        offset_zone = None
    return offset_zone if is_valid_timezone(offset_zone) else FALLBACK_TIMEZONE


def local_today(tz_name: str, now: datetime | None = None) -> date:
    """Today's calendar date in `tz_name` (from the system clock unless `now`, an aware instant, is given)."""
    zone = ZoneInfo(tz_name)
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    return instant.astimezone(zone).date()


DateStatus = Literal["past", "today", "future"]


def date_status(day: date, today: date) -> DateStatus:
    """Whether `day` is before, on or after `today`."""
    if day < today:
        return "past"
    return "today" if day == today else "future"
