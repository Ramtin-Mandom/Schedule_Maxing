"""
app/sync/credentials.py

Where "Keep me signed in" keeps its one secret: the operating system's
credential store (Windows Credential Manager, macOS Keychain, Secret Service)
through the `keyring` package -- never the application database, a settings
file or a log.

What is kept is the session's refresh credential, not the password: it can
only renew this device's session, the server revokes it on sign-out or a
password reset, and it expires on its own (REFRESH_TOKEN_EXPIRE_DAYS there).
One entry per account (its account_key, which names the backend and user).

A vault never raises: without `keyring` or a usable store, system_vault()
is None and the option is simply not offered; a failing read is "nothing
kept" and a failing write is reported as False.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger(__name__)

SERVICE_NAME = "Schedule Maxing"


class CredentialVault(Protocol):
    def load(self, account_key: str) -> str | None: ...

    def save(self, account_key: str, secret: str) -> bool: ...

    def delete(self, account_key: str) -> None: ...


class KeyringVault:
    def __init__(self, backend) -> None:
        self._keyring = backend

    def load(self, account_key: str) -> str | None:
        try:
            return self._keyring.get_password(SERVICE_NAME, account_key) or None
        except Exception:  # noqa: BLE001 - an unreadable store is "nothing kept"
            logger.warning("The system credential store could not be read.")
            return None

    def save(self, account_key: str, secret: str) -> bool:
        try:
            self._keyring.set_password(SERVICE_NAME, account_key, secret)
        except Exception:  # noqa: BLE001 - the session still works; it just is not kept
            logger.warning("The system credential store could not be written.")
            return False
        return True

    def delete(self, account_key: str) -> None:
        try:
            self._keyring.delete_password(SERVICE_NAME, account_key)
        except Exception:  # noqa: BLE001 - nothing was kept, or the store is unavailable
            pass


class MemoryVault:
    """A vault for tests: the same contract, kept in a dict."""

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    def load(self, account_key: str) -> str | None:
        return self.secrets.get(account_key)

    def save(self, account_key: str, secret: str) -> bool:
        self.secrets[account_key] = secret
        return True

    def delete(self, account_key: str) -> None:
        self.secrets.pop(account_key, None)


def system_vault() -> CredentialVault | None:
    """The operating system's credential store, or None when it cannot be used on this computer."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as NoStore

        if isinstance(keyring.get_keyring(), NoStore):
            return None
        return KeyringVault(keyring)
    except Exception:  # noqa: BLE001 - not installed or not usable: the option is not offered
        return None


class LazyVault:
    """
    system_vault(), found the first time it is needed: keyring's backend
    discovery takes about 0.3 s, so the desktop does not pay for it while
    starting. `available` is whether a store exists (it finds out then);
    without one, load() is None, save() False and delete() does nothing --
    the same answers as having no vault. prefetch() finds it in a background
    thread (it touches no Tk object), e.g. once the first page is shown.
    Thread-safe: it is found once, whichever thread asks first.
    """

    def __init__(self, find: Callable[[], CredentialVault | None] | None = None) -> None:
        #: None: this module's system_vault, looked up when it is needed (so it can be replaced, e.g. in tests).
        self._find = find
        self._lock = threading.Lock()
        self._found = False
        self._vault: CredentialVault | None = None

    def _resolve(self) -> CredentialVault | None:
        with self._lock:
            if not self._found:
                self._vault, self._found = (self._find or system_vault)(), True
            return self._vault

    @property
    def available(self) -> bool:
        return self._resolve() is not None

    def prefetch(self) -> None:
        if not self._found:
            threading.Thread(target=self._resolve, name="schedule-maxing-credential-store", daemon=True).start()

    def load(self, account_key: str) -> str | None:
        vault = self._resolve()
        return vault.load(account_key) if vault is not None else None

    def save(self, account_key: str, secret: str) -> bool:
        vault = self._resolve()
        return vault.save(account_key, secret) if vault is not None else False

    def delete(self, account_key: str) -> None:
        vault = self._resolve()
        if vault is not None:
            vault.delete(account_key)
