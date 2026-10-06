"""
app/ui/update_controller.py

The desktop's use of the update service (app/update/service.py), free of Tk
so it is tested headlessly. Its three operations run in worker threads
(app.ui.background.run_in_background) and return ControllerResults; the
widgets in app/ui/update_view.py only show what they return. The
preferences (app/ui/ui_settings.py) are read and written by the caller on
the Tk thread, through the pure helpers at the end.

- An automatic check runs at most once a day, only while "Automatically
  check for updates" is on, and says nothing unless a newer version that the
  user has not skipped exists.
- A manual check ("Check now") always runs and always answers, including a
  version that was skipped.
- Downloading and installing happen only after the user chose "Update now".
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from app.ui.background import ControllerResult
from app.ui.ui_settings import UISettings
from app.update.downloader import VerificationError, VerifiedDownload
from app.update.release_feed import ReleaseInfo
from app.update.service import AVAILABLE, DISABLED, UP_TO_DATE, UpdateInstallError, UpdateService
from app.update.transport import DownloadCancelled, UpdateTransportError
from app.update.versioning import parse_version

AUTOMATIC_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True)
class UpdateView:
    """What a check found, in words for the user."""

    state: str  # "available" | "up_to_date" | "unavailable" | "disabled" | "skipped"
    message: str
    installed_version: str
    release: ReleaseInfo | None = None

    @property
    def offer(self) -> bool:
        """Whether to offer the update now."""
        return self.state == AVAILABLE and self.release is not None


def megabytes(size: int) -> str:
    return f"{size / 1_000_000:.0f} MB"


class UpdateController:
    def __init__(self, service: UpdateService | None = None) -> None:
        self.service = service or UpdateService()
        self._cancel = threading.Event()

    @property
    def installed_version(self) -> str:
        return self.service.installed_version

    @property
    def enabled(self) -> bool:
        return self.service.enabled

    @property
    def can_install(self) -> bool:
        return self.service.can_install

    # -- worker-thread operations -----------------------------------------------------------

    def check(self, *, skipped_version: str | None = None) -> ControllerResult[UpdateView]:
        """Look for a newer version. Pass the skipped version for an automatic check; None for "Check now"."""
        result = self.service.check()
        installed = result.installed_version
        if result.status == AVAILABLE:
            release = result.release
            if skipped_version is not None and parse_version(skipped_version) == parse_version(release.version):
                return ControllerResult.success(UpdateView("skipped", f"Version {release.version} was skipped.", installed))
            return ControllerResult.success(UpdateView(
                AVAILABLE, f"Schedule Maxing {release.version} is available (you have {installed}; "
                           f"{megabytes(release.installer_size)} download).", installed, release))
        if result.status == UP_TO_DATE:
            return ControllerResult.success(UpdateView(UP_TO_DATE, f"You are up to date (version {installed}).", installed))
        if result.status == DISABLED:
            return ControllerResult.success(UpdateView(DISABLED, result.reason or "Update checks are off.", installed))
        return ControllerResult.success(UpdateView(
            "unavailable", f"Could not check for updates. {result.reason or ''}".strip() + " Nothing was changed.",
            installed))

    def download(self, release: ReleaseInfo, progress: Callable[[int, int], None] | None = None
                 ) -> ControllerResult[VerifiedDownload]:
        """Download and verify the installer; a failure leaves the installed version untouched."""
        self._cancel.clear()
        try:
            return ControllerResult.success(self.service.download(release, cancelled=self._cancel.is_set, progress=progress))
        except DownloadCancelled as error:
            return ControllerResult.failure("The download was cancelled. Nothing was changed.", error)
        except VerificationError as error:
            return ControllerResult.failure(
                f"The downloaded update could not be verified, so it was deleted and not installed ({error}). "
                "Nothing was changed.", error)
        except (UpdateTransportError, UpdateInstallError, OSError) as error:
            return ControllerResult.failure(f"The update could not be downloaded ({error}). Nothing was changed.", error)

    def cancel_download(self) -> None:
        self._cancel.set()

    def install(self, download: VerifiedDownload) -> ControllerResult[VerifiedDownload]:
        """Start the verified installer. On success the caller closes the application so it can be replaced."""
        try:
            self.service.launch_installer(download)
        except UpdateInstallError as error:
            return ControllerResult.failure(f"{error} Nothing was changed.", error)
        return ControllerResult.success(download)


# -- preferences (pure; the caller saves the returned settings on the Tk thread) -------------------


def automatic_check_due(settings: UISettings, now: datetime, *, enabled: bool = True) -> bool:
    """Whether an automatic check should run now: the option is on and the last one was a day or more ago."""
    if not enabled or not settings.check_for_updates:
        return False
    try:
        last = datetime.fromisoformat(settings.last_update_check) if settings.last_update_check else None
    except ValueError:
        last = None
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= AUTOMATIC_INTERVAL or last > now  # a clock set back never silences checks for good


def with_check_recorded(settings: UISettings, now: datetime) -> UISettings:
    return replace(settings, last_update_check=now.astimezone(timezone.utc).isoformat(timespec="seconds"))


def with_automatic_checks(settings: UISettings, enabled: bool) -> UISettings:
    return replace(settings, check_for_updates=bool(enabled))


def with_skipped_version(settings: UISettings, version: str | None) -> UISettings:
    return replace(settings, skipped_version=version if parse_version(version) is not None else None)
