"""
app/update/downloader.py

Downloads a release's installer into the per-user updates folder
(app.runtime.updates_dir()) and accepts it only when it is exactly the file
the release published:

1. SHA256SUMS.txt is fetched and must list the installer's exact filename;
2. the installer is streamed to "<name>.partial" -- never more bytes than the
   release announced -- with its SHA-256 computed on the way;
3. its size must equal the announced size and its digest the listed one
   (compared in constant time);
4. only then is it renamed to its final name.

On any failure or cancellation the partial file is deleted and nothing is
left that could be run. Files of earlier downloads are removed first, so the
folder holds at most one installer.

Trust model (docs/windows-distribution.md): HTTPS to GitHub plus the
checksum protect against a corrupted or truncated download and against a
file swapped on the way. They do not prove who built it -- the checksum
comes from the same release. That proof is a code signature;
verify_signature() is where it is checked once releases are signed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.update.release_feed import CHECKSUMS_NAME, ReleaseInfo
from app.update.transport import DownloadCancelled, UpdateTransportError

logger = logging.getLogger(__name__)

MAX_CHECKSUMS_BYTES = 64 * 1024
PARTIAL_SUFFIX = ".partial"
_OURS = ("ScheduleMaxing-Setup-*.exe", "ScheduleMaxing-Setup-*.exe" + PARTIAL_SUFFIX)


class VerificationError(Exception):
    """The downloaded installer is not the file the release published; it was deleted."""


@dataclass(frozen=True)
class VerifiedDownload:
    version: str
    path: Path
    sha256: str
    size: int


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_digest(checksums_text: str, filename: str) -> str:
    """The digest SHA256SUMS.txt lists for exactly `filename`; VerificationError if it lists none or several."""
    found = []
    for line in checksums_text.splitlines():
        digest, _, name = line.strip().partition(" ")
        if name.strip().lstrip("*") == filename and len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest):
            found.append(digest.lower())
    if len(set(found)) != 1:
        raise VerificationError(f"{CHECKSUMS_NAME} does not list exactly one checksum for {filename}")
    return found[0]


def remove_stale_downloads(directory: Path, keep: Path | None = None) -> None:
    """Delete installers left by earlier downloads (only files this module names; nothing else in the folder)."""
    if not directory.is_dir():
        return
    for pattern in _OURS:
        for path in directory.glob(pattern):
            if path != keep and path.is_file():
                try:
                    path.unlink()
                except OSError as error:
                    logger.warning("Could not remove the old update download %s: %s", path, error)


def verify_signature(path: Path) -> None:
    """
    The place for Authenticode verification. Releases are not signed yet, so
    there is nothing to check; once they are, this must reject an installer
    that is unsigned or signed by anyone but the publisher.
    """


def download_and_verify(release: ReleaseInfo, directory: Path, fetcher, *, cancelled: Callable[[], bool] = lambda: False,
                        progress: Callable[[int, int], None] | None = None) -> VerifiedDownload:
    """
    The verified installer of `release` in `directory`. Raises
    VerificationError, UpdateTransportError or DownloadCancelled, leaving no
    file behind.
    """
    directory.mkdir(parents=True, exist_ok=True)
    remove_stale_downloads(directory)
    target = directory / release.installer_name
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    try:
        try:
            checksums = fetcher.fetch(release.checksums_url, max_bytes=MAX_CHECKSUMS_BYTES).decode("utf-8")
        except UnicodeDecodeError:
            raise VerificationError(f"{CHECKSUMS_NAME} could not be read") from None
        expected = expected_digest(checksums, release.installer_name)

        report = (lambda written: progress(written, release.installer_size)) if progress is not None else None
        size, digest = fetcher.download(release.installer_url, partial, max_bytes=release.installer_size,
                                        cancelled=cancelled, progress=report)
        if size != release.installer_size:
            raise VerificationError(f"the download is incomplete ({size} of {release.installer_size} bytes)")
        if not hmac.compare_digest(digest.lower(), expected):
            raise VerificationError("the downloaded installer does not match its published checksum")
        verify_signature(partial)
        partial.replace(target)
    except BaseException as error:
        for leftover in (partial, target):
            try:
                leftover.unlink()
            except OSError:
                pass
        if not isinstance(error, (VerificationError, UpdateTransportError, DownloadCancelled)):
            logger.exception("The update download failed unexpectedly.")
        raise
    logger.info("Downloaded and verified %s (%d bytes).", target.name, size)
    return VerifiedDownload(version=release.version, path=target, sha256=expected, size=size)
