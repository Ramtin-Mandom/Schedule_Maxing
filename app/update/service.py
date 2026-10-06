"""
app/update/service.py

The update flow, free of Tk (the desktop drives it from worker threads
through app/ui/update_controller.py):

    check()             -> is a newer stable release published?
    download(release)   -> the installer, verified (app/update/downloader.py)
    launch_installer()  -> start it and return, so the application can close

Design (docs/windows-distribution.md, "Updates"): an update is the next
version's ordinary installer, run over the installed one. The installer
waits for this application to close, replaces the program's own files and
starts the new version (packaging/windows/ScheduleMaxing.iss, /RELAUNCH=1).
Nothing here replaces files, and nothing touches the data folder: the
database is migrated -- after a safety copy -- by the new version when it
first opens it (app/execution/db.py).

Safety:
- only a release strictly newer than the installed version is ever offered,
  so a re-published older release cannot downgrade anyone;
- launch_installer() runs only a file that download() verified in this
  process, that is still inside the updates folder, and whose SHA-256 still
  matches immediately before it is started;
- every failure leaves the installed version exactly as it was.

From a source checkout there is nothing to update: checking works (useful
while developing), installing is refused.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app import runtime
from app.update.downloader import VerifiedDownload, download_and_verify, sha256_of
from app.update.release_feed import FeedError, ReleaseInfo, latest_release, valid_repository
from app.update.transport import HttpsFetcher, UpdateTransportError
from app.update.versioning import is_newer, parse_version
from app.version import __version__

logger = logging.getLogger(__name__)

UP_TO_DATE, AVAILABLE, UNAVAILABLE, DISABLED = "up_to_date", "available", "unavailable", "disabled"
#: What the installer is started with: progress window only, no restart of Windows, start the new version afterwards.
INSTALLER_ARGUMENTS = ("/SILENT", "/NORESTART", "/RELAUNCH=1")


class UpdateInstallError(Exception):
    """The installer was not started; the installed version is unchanged."""


@dataclass(frozen=True)
class UpdateCheck:
    status: str
    installed_version: str
    release: ReleaseInfo | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.status == AVAILABLE


def start_installer(path: Path, arguments: tuple[str, ...]) -> None:
    """Start the installer as its own process (through the shell, so Windows can ask for elevation) and return."""
    if not sys.platform.startswith("win"):
        raise UpdateInstallError("updates can be installed on Windows only")
    os.startfile(str(path), "open", " ".join(arguments), str(path.parent))  # noqa: S606 - a verified local file


class UpdateService:
    def __init__(
        self,
        *,
        installed_version: str = __version__,
        repository: str | None = None,
        fetcher=None,
        updates_dir: Path | None = None,
        launcher: Callable[[Path, tuple[str, ...]], None] = start_installer,
        frozen: Callable[[], bool] = runtime.is_frozen,
    ) -> None:
        from config import settings

        self.installed_version = installed_version
        self.repository = settings.UPDATE_REPOSITORY if repository is None else repository
        self._fetcher = fetcher
        self._updates_dir = updates_dir
        self._launcher = launcher
        self._frozen = frozen
        #: The downloads verified by this process: path -> SHA-256. Only these may be launched.
        self._verified: dict[Path, str] = {}

    @property
    def enabled(self) -> bool:
        """False when no repository is configured (SCHEDULE_MAXING_UPDATE_CHECK=off, or an invalid name)."""
        return bool(self.repository) and valid_repository(self.repository)

    @property
    def can_install(self) -> bool:
        """Only the packaged application can be updated; a source checkout is updated with git."""
        return self._frozen()

    @property
    def updates_dir(self) -> Path:
        return Path(self._updates_dir) if self._updates_dir is not None else runtime.updates_dir()

    def _transport(self):
        if self._fetcher is None:
            self._fetcher = HttpsFetcher()
        return self._fetcher

    def check(self) -> UpdateCheck:
        """Never raises: an unreachable or untrustworthy answer is UNAVAILABLE with a reason."""
        installed = self.installed_version
        if not self.enabled:
            return UpdateCheck(DISABLED, installed, reason="Update checks are turned off for this installation.")
        try:
            release = latest_release(self.repository, self._transport())
        except UpdateTransportError as error:
            logger.info("Update check: %s", error)
            if error.status == 404:  # GitHub's answer while a repository has no published stable release
                return UpdateCheck(UNAVAILABLE, installed, reason="No released version is published yet.")
            return UpdateCheck(UNAVAILABLE, installed, reason=f"The update server could not be reached ({error}).")
        except FeedError as error:
            logger.warning("Update check: the release information was not usable: %s", error)
            return UpdateCheck(UNAVAILABLE, installed, reason="The release information could not be read.")
        except Exception:  # noqa: BLE001 - a check must never take the application down
            logger.exception("Update check failed unexpectedly.")
            return UpdateCheck(UNAVAILABLE, installed, reason="The update check failed unexpectedly.")
        if parse_version(installed) is None:
            logger.warning("Update check: the installed version %r is not a release version.", installed)
            return UpdateCheck(UNAVAILABLE, installed, reason="This is not a released version of Schedule Maxing.")
        if not is_newer(release.version, installed):
            return UpdateCheck(UP_TO_DATE, installed)  # the same, or older: never a downgrade
        logger.info("Update available: %s (installed: %s).", release.version, installed)
        return UpdateCheck(AVAILABLE, installed, release=release)

    def download(self, release: ReleaseInfo, *, cancelled: Callable[[], bool] = lambda: False,
                 progress: Callable[[int, int], None] | None = None) -> VerifiedDownload:
        """The verified installer (raises VerificationError, UpdateTransportError or DownloadCancelled)."""
        if not is_newer(release.version, self.installed_version):
            raise UpdateInstallError("that release is not newer than the installed version")
        result = download_and_verify(release, self.updates_dir, self._transport(), cancelled=cancelled, progress=progress)
        self._verified[result.path.resolve()] = result.sha256
        return result

    def launch_installer(self, download: VerifiedDownload) -> None:
        """Start the verified installer and return; the caller then closes the application. Raises UpdateInstallError."""
        if not self.can_install:
            raise UpdateInstallError("This copy runs from source code, so it is not updated by the installer.")
        path = Path(download.path).resolve()
        expected = self._verified.get(path)
        if expected is None or expected != download.sha256:
            raise UpdateInstallError("That file was not downloaded and verified by this session.")
        if self.updates_dir.resolve() not in path.parents:
            raise UpdateInstallError("The installer is outside the updates folder.")
        if not path.is_file() or sha256_of(path) != expected:
            self._verified.pop(path, None)
            raise UpdateInstallError("The downloaded installer changed after it was verified; it was not started.")
        try:
            self._launcher(path, INSTALLER_ARGUMENTS)
        except UpdateInstallError:
            raise
        except Exception as error:  # noqa: BLE001 - e.g. the elevation prompt was declined
            logger.warning("The installer could not be started: %s", error)
            raise UpdateInstallError(f"The installer could not be started ({type(error).__name__}).") from None
        logger.info("Started the installer for version %s; closing so it can update the program.", download.version)
