"""Shared test setup: isolation of the data location, backend and credential store, test tiers and Tk cleanup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from config import settings
from tests import window_placement
from tests.test_tiers import tiers
from tests.tk_cleanup import purge_destroyed_roots

# Windows opened by the tests appear on the monitor left of the primary one, when there is one
# (tests/window_placement.py); the child-process probes inherit the same placement.
window_placement.install()


@pytest.fixture(autouse=True)
def _isolate_default_data_location(tmp_path_factory, monkeypatch):
    """
    Safety net: any code path that opens the *default* database location
    (no explicit path) during tests gets a throwaway directory instead of the
    real per-user data folder or the checkout's legacy data/ folder.
    """
    isolated = tmp_path_factory.mktemp("default_data_dir")
    monkeypatch.setattr(settings, "DATA_DIR", isolated / "data")
    monkeypatch.setattr(settings, "LEGACY_DATA_DIR", isolated / "legacy_repo_data")
    monkeypatch.setattr(settings, "DATA_DIR_OVERRIDDEN", False)


@pytest.fixture(autouse=True)
def _no_real_backend_or_credential_store(monkeypatch):
    """
    Tests never reach the built-in API address or the operating system's
    credential store: no backend is configured unless a test names one, and
    "Keep me signed in" has no vault unless a test injects one.
    """
    from app.sync import credentials

    monkeypatch.setattr(settings, "BACKEND_URL", None)
    monkeypatch.setattr(settings, "UPDATE_REPOSITORY", None)  # no update check reaches GitHub from a test
    monkeypatch.setattr(settings, "GENERATION_PROCESS", False)  # no engine process per test window (tested apart)
    monkeypatch.setattr(credentials, "system_vault", lambda: None)


@pytest.fixture(autouse=True)
def _cheap_test_password_hashing(request, monkeypatch):
    """
    Test-only, explicit: Argon2id with minimal cost parameters (still Argon2id, still verified for real), so the
    hundreds of registrations and sign-ins in the server-backed tests stop spending ~50 ms each. Production
    settings (backend/passwords.py) are untouched; child processes and tests marked `real_password_hashing`
    (which check the production parameters) use the real hasher. Acts only when backend.passwords is already
    imported -- a desktop-only test run never imports server packages because of this fixture.
    """
    passwords = sys.modules.get("backend.passwords")
    if passwords is None or request.node.get_closest_marker("real_password_hashing"):
        return
    monkeypatch.setattr(passwords, "_hasher", _TEST_HASHER[0])
    monkeypatch.setattr(passwords, "_DUMMY_HASH", _TEST_HASHER[1])


def _test_hasher():
    if "argon2" not in sys.modules:
        return (None, None)
    from argon2 import PasswordHasher

    hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    return (hasher, hasher.hash("dummy-password-for-unknown-accounts"))


class _LazyHasher(list):
    """Built on first use (argon2 is imported only where the backend is)."""

    def __getitem__(self, index):
        if not len(self):
            self.extend(_test_hasher())
        return super().__getitem__(index)


_TEST_HASHER = _LazyHasher()


def pytest_collection_modifyitems(config, items):
    """Give every test its tier markers (tests/test_tiers.py): unit/integration, ui, system, slow and dev."""
    root = Path(str(config.rootpath))
    sources: dict[Path, str] = {}
    for item in items:
        path = Path(str(item.path))
        if path not in sources:
            try:
                sources[path] = path.read_text(encoding="utf-8")
            except OSError:
                sources[path] = ""
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.as_posix()
        explicit = {mark.name for mark in item.iter_markers()}
        for name in tiers(relative, item.name, sources[path]):
            if name == "dev" and explicit & {"slow", "ui", "system"}:
                continue  # an explicit @pytest.mark.slow (etc.) keeps a test out of the development suite
            item.add_marker(getattr(pytest.mark, name))


@pytest.fixture(autouse=True)
def _forget_destroyed_tk_roots():
    """After each test: drop destroyed Tk roots from CustomTkinter's global registries (tests/tk_cleanup.py)."""
    yield
    purge_destroyed_roots()
