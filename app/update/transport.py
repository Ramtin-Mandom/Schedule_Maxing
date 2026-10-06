"""
app/update/transport.py

The updater's only network access: HTTPS requests to GitHub, standard
library only. Every URL that is requested -- and every redirect it leads to,
since GitHub serves release files from a second host -- must be HTTPS on a
host in ALLOWED_HOSTS; anything else is refused before a byte is read.
Responses are size-capped, and a download is streamed to disk with its
SHA-256 computed on the way.

Nothing here decides what to download or run: app/update/release_feed.py
chooses the URLs and app/update/downloader.py verifies the result. Tests
replace this class with a fake that has the same two methods.
"""

from __future__ import annotations

import hashlib
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from app.version import APP_ID, __version__

#: GitHub's API and website, and the hosts its release downloads redirect to.
ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
TIMEOUT_SECONDS = 10.0
CHUNK = 64 * 1024


class UpdateTransportError(Exception):
    """The update server could not be reached, answered with an error, or sent something unacceptable."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        #: The HTTP status, when the server answered with an error.
        self.status = status


class DownloadCancelled(Exception):
    """The user cancelled the download."""


def check_url(url: str) -> str:
    """`url` if it is an HTTPS URL on an allowed GitHub host; otherwise UpdateTransportError."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS or parts.username or parts.password:
        raise UpdateTransportError(f"refused a URL outside the trusted update hosts: {parts.scheme}://{parts.hostname}")
    if parts.port not in (None, 443):
        raise UpdateTransportError("refused a URL with an unexpected port")
    return url


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class HttpsFetcher:
    def __init__(self, *, timeout: float = TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        # No proxy authentication, cookies or other handlers beyond HTTPS and the checked redirects.
        self._opener = urllib.request.build_opener(_CheckedRedirects)

    def _open(self, url: str, accept: str):
        request = urllib.request.Request(check_url(url), headers={
            "User-Agent": f"{APP_ID}/{__version__}", "Accept": accept,
        })
        try:
            return self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            raise UpdateTransportError(f"the update server answered HTTP {error.code}", status=error.code) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as error:
            raise UpdateTransportError(f"could not reach the update server ({type(error).__name__})") from None

    def fetch(self, url: str, *, max_bytes: int, accept: str = "application/octet-stream") -> bytes:
        """The body of a small response; UpdateTransportError if it is larger than `max_bytes`."""
        try:
            with self._open(url, accept) as response:
                check_url(response.geturl())
                data = response.read(max_bytes + 1)
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as error:
            raise UpdateTransportError(f"the connection to the update server failed ({type(error).__name__})") from None
        if len(data) > max_bytes:
            raise UpdateTransportError("the update server sent an unexpectedly large response")
        return data

    def download(self, url: str, target: Path, *, max_bytes: int, cancelled: Callable[[], bool] = lambda: False,
                 progress: Callable[[int], None] | None = None) -> tuple[int, str]:
        """Stream `url` into `target`; returns (bytes written, SHA-256 hex). The caller removes a failed file."""
        digest, written = hashlib.sha256(), 0
        try:
            with self._open(url, "application/octet-stream") as response, target.open("wb") as file:
                check_url(response.geturl())
                while True:
                    if cancelled():
                        raise DownloadCancelled()
                    block = response.read(CHUNK)
                    if not block:
                        break
                    written += len(block)
                    if written > max_bytes:
                        raise UpdateTransportError("the download is larger than announced")
                    digest.update(block)
                    file.write(block)
                    if progress is not None:
                        progress(written)
        except (socket.timeout, TimeoutError, ConnectionError) as error:
            raise UpdateTransportError(f"the download was interrupted ({type(error).__name__})") from None
        return written, digest.hexdigest()
