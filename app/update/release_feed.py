"""
app/update/release_feed.py

Which version is the latest published one: GitHub's "latest release" of the
application's repository (config.settings.UPDATE_REPOSITORY),

    GET https://api.github.com/repos/<owner>/<repo>/releases/latest

GitHub answers that request with the newest release that is neither a draft
nor a pre-release, so development builds, branches and pull requests never
reach users, and a release that is withdrawn (deleted, or re-marked as a
pre-release) stops being offered at once.

The answer is untrusted input. parse_release() accepts it only when:

- the tag is a stable `vMAJOR.MINOR.PATCH` version (app/update/versioning.py);
- it is not a draft or pre-release (checked again here);
- exactly one asset is named `ScheduleMaxing-Setup-<that version>.exe`, with
  a plausible size, and one is named `SHA256SUMS.txt`;
- both download URLs are HTTPS on github.com, inside this repository's
  release for that tag.

Anything else raises FeedError: the caller logs it and offers no update.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.update.transport import UpdateTransportError
from app.update.versioning import format_version, parse_version

API_URL = "https://api.github.com/repos/{repository}/releases/latest"
CHECKSUMS_NAME = "SHA256SUMS.txt"
INSTALLER_NAME = "ScheduleMaxing-Setup-{version}.exe"
MAX_FEED_BYTES = 1_000_000
#: An installer smaller or larger than this is not ours (the real one is roughly 60-120 MB).
MIN_INSTALLER_BYTES = 1_000_000
MAX_INSTALLER_BYTES = 1_000_000_000

_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")


class FeedError(Exception):
    """The release information was missing, malformed or not what a Schedule Maxing release looks like."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    installer_name: str
    installer_url: str
    installer_size: int
    checksums_url: str
    notes_url: str


def valid_repository(repository: object) -> bool:
    return isinstance(repository, str) and _REPOSITORY.fullmatch(repository) is not None


def _asset_url(asset: dict, repository: str, tag: str, name: str) -> str:
    url = asset.get("browser_download_url")
    expected = f"https://github.com/{repository}/releases/download/{tag}/{name}"
    if not isinstance(url, str) or url.lower() != expected.lower():
        raise FeedError(f"the download address of {name} is not this repository's release file")
    return url


def parse_release(payload: object, repository: str) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise FeedError("the release information is not an object")
    tag = payload.get("tag_name")
    version = parse_version(tag)
    if version is None or not isinstance(tag, str) or not tag.startswith("v"):
        raise FeedError(f"the release tag {tag!r} is not a stable version")
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise FeedError("the release is a draft or a pre-release")
    assets = payload.get("assets")
    if not isinstance(assets, list) or not all(isinstance(asset, dict) for asset in assets):
        raise FeedError("the release has no asset list")

    text = format_version(version)
    installer_name = INSTALLER_NAME.format(version=text)
    installers = [asset for asset in assets if asset.get("name") == installer_name]
    checksums = [asset for asset in assets if asset.get("name") == CHECKSUMS_NAME]
    if len(installers) != 1 or len(checksums) != 1:
        raise FeedError(f"the release must contain exactly one {installer_name} and one {CHECKSUMS_NAME}")
    size = installers[0].get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not MIN_INSTALLER_BYTES <= size <= MAX_INSTALLER_BYTES:
        raise FeedError("the installer's size is missing or implausible")
    return ReleaseInfo(
        version=text,
        installer_name=installer_name,
        installer_url=_asset_url(installers[0], repository, tag, installer_name),
        installer_size=size,
        checksums_url=_asset_url(checksums[0], repository, tag, CHECKSUMS_NAME),
        notes_url=f"https://github.com/{repository}/releases/tag/{tag}",
    )


def latest_release(repository: str, fetcher) -> ReleaseInfo:
    """The latest stable release; FeedError or UpdateTransportError when there is none to trust."""
    if not valid_repository(repository):
        raise FeedError("the update repository is not configured")
    data = fetcher.fetch(API_URL.format(repository=repository), max_bytes=MAX_FEED_BYTES,
                         accept="application/vnd.github+json")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise FeedError("the release information could not be read") from None
    return parse_release(payload, repository)


__all__ = ["FeedError", "ReleaseInfo", "UpdateTransportError", "latest_release", "parse_release", "valid_repository"]
