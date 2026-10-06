"""
app/update/versioning.py

Application versions for the updater: `MAJOR.MINOR.PATCH`, compared as
numbers (1.10.0 is newer than 1.9.9; a string comparison would say the
opposite). A leading "v" is accepted, as in a release tag. Anything else --
a pre-release or build suffix ("1.3.0-rc1", "1.3.0+build5", "1.3"), text
around the number -- is not a stable version: parse_version() returns None
and such a release is never offered.
"""

from __future__ import annotations

import re

Version = tuple[int, int, int]

_STABLE = re.compile(r"v?(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})")


def parse_version(text: object) -> Version | None:
    """(major, minor, patch) of a stable version string, else None."""
    if not isinstance(text, str):
        return None
    match = _STABLE.fullmatch(text)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def is_newer(candidate: object, installed: object) -> bool:
    """True only when both are stable versions and `candidate` is strictly newer than `installed`."""
    candidate_version, installed_version = parse_version(candidate), parse_version(installed)
    if candidate_version is None or installed_version is None:
        return False
    return candidate_version > installed_version


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)
