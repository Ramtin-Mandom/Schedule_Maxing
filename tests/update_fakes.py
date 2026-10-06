"""Stand-ins for the updater tests (tests/update): a fake GitHub -- no test uses the network -- and a fake Tk root."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from app.update.transport import DownloadCancelled, UpdateTransportError

REPOSITORY = "example-owner/Schedule_Maxing"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"


def installer_bytes(version: str) -> bytes:
    return (f"installer {version} ".encode() * 80_000)[:1_200_000]


def asset_url(version: str, name: str) -> str:
    return f"https://github.com/{REPOSITORY}/releases/download/v{version}/{name}"


def release_payload(version: str, *, size: int | None = None, **overrides) -> dict:
    name = f"ScheduleMaxing-Setup-{version}.exe"
    payload = {
        "tag_name": f"v{version}", "draft": False, "prerelease": False,
        "assets": [
            {"name": name, "size": len(installer_bytes(version)) if size is None else size,
             "browser_download_url": asset_url(version, name)},
            {"name": "SHA256SUMS.txt", "size": 100, "browser_download_url": asset_url(version, "SHA256SUMS.txt")},
        ],
    }
    payload.update(overrides)
    return payload


class FakeGitHub:
    """Answers the updater's two methods from a table of URL -> bytes or exception; records every request."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.requests: list[str] = []
        self.gate: threading.Event | None = None  # when set, fetch() waits for it (a slow server)
        self.cancel_after: int | None = None

    def publish(self, version: str, *, payload: dict | None = None, installer: bytes | None = None,
                checksums: str | None = None) -> None:
        data = installer_bytes(version) if installer is None else installer
        name = f"ScheduleMaxing-Setup-{version}.exe"
        self.responses[API] = json.dumps(release_payload(version) if payload is None else payload).encode()
        self.responses[asset_url(version, name)] = data
        listed = f"{hashlib.sha256(installer_bytes(version)).hexdigest()}  {name}\n" if checksums is None else checksums
        self.responses[asset_url(version, "SHA256SUMS.txt")] = listed.encode()

    def _answer(self, url: str) -> bytes:
        self.requests.append(url)
        answer = self.responses.get(url, UpdateTransportError("the update server answered HTTP 404"))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def fetch(self, url: str, *, max_bytes: int, accept: str = "application/octet-stream") -> bytes:
        if self.gate is not None:
            self.gate.wait(10)
        data = self._answer(url)
        if len(data) > max_bytes:
            raise UpdateTransportError("the update server sent an unexpectedly large response")
        return data

    def download(self, url: str, target: Path, *, max_bytes: int, cancelled=lambda: False, progress=None):
        data = self._answer(url)
        digest, written = hashlib.sha256(), 0
        with target.open("wb") as file:
            for start in range(0, len(data), 256 * 1024):
                if cancelled():
                    raise DownloadCancelled()
                block = data[start:start + 256 * 1024]
                written += len(block)
                if written > max_bytes:
                    raise UpdateTransportError("the download is larger than announced")
                digest.update(block)
                file.write(block)
                if progress is not None:
                    progress(written)
        return written, digest.hexdigest()


class FakeRoot:
    """What app.ui.background needs of a Tk widget: after() and winfo_exists()."""

    def __init__(self) -> None:
        self.scheduled: list = []
        self.lock = threading.Lock()

    def after(self, _ms, callback) -> None:
        with self.lock:
            self.scheduled.append(callback)

    def winfo_exists(self) -> bool:
        return True

    def run_pending(self) -> int:
        with self.lock:
            pending, self.scheduled = self.scheduled, []
        for callback in pending:
            callback()
        return len(pending)
