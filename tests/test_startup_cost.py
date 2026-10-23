"""
What the desktop does NOT do while it starts (docs/desktop-performance-round3.md):
importing app.app loads no machine-learning stack and none of the pages that are
built on first show, and the credential store is found only when it is needed.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.sync.credentials import LazyVault, MemoryVault

ROOT = Path(__file__).resolve().parents[1]

#: Loaded the first time an ML report is computed (in a worker), never by start-up.
ML_MODULES = ("sklearn", "scipy", "pandas", "joblib")
#: Pages built on first show (app/ui/pages.py DeferredPage) are imported then, too.
DEFERRED_PAGE_MODULES = ("app.ui.calendar_page", "app.ui.planning_pages", "app.ui.productivity_page",
                         "app.ui.budget_page", "app.ui.guide_page", "app.ui.account_page")


@pytest.mark.slow
def test_importing_the_desktop_loads_no_ml_stack_and_no_deferred_page() -> None:
    code = (
        "import sys, app.app\n"
        f"print(sorted(m for m in sys.modules if m.split('.')[0] in {ML_MODULES!r} or m in {DEFERRED_PAGE_MODULES!r}"
        " or m.split('.')[0] == 'keyring'))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_ml_stack_still_loads_when_a_model_is_used() -> None:
    from app.productivity import ml_model

    assert ml_model.Ridge.__module__.startswith("sklearn")  # the trainer itself imports it as before


def test_a_lazy_vault_finds_the_store_once_on_first_use() -> None:
    calls = []
    store = MemoryVault()
    vault = LazyVault(lambda: calls.append(1) or store)
    assert calls == []  # nothing looked up when it is created
    assert vault.save("key", "secret") is True
    assert vault.load("key") == "secret" and vault.available
    vault.delete("key")
    assert vault.load("key") is None
    assert calls == [1]


def test_a_lazy_vault_without_a_store_answers_like_no_vault() -> None:
    vault = LazyVault(lambda: None)
    assert vault.available is False
    assert vault.load("key") is None and vault.save("key", "secret") is False
    vault.delete("key")  # nothing to do, never raises


def test_a_lazy_vault_uses_the_module_lookup_at_first_use(monkeypatch) -> None:
    from app.sync import credentials

    store = MemoryVault()
    vault = LazyVault()
    monkeypatch.setattr(credentials, "system_vault", lambda: store)  # replaced after the vault was made
    assert vault.save("key", "secret") and store.secrets == {"key": "secret"}


def test_prefetch_finds_the_store_in_the_background_exactly_once() -> None:
    started, release, calls = threading.Event(), threading.Event(), []

    def find():
        calls.append(threading.current_thread().name)
        started.set()
        release.wait(5)
        return MemoryVault()

    vault = LazyVault(find)
    vault.prefetch()
    assert started.wait(5)
    waiter = threading.Thread(target=lambda: vault.load("key"))  # a caller meanwhile waits for the same lookup
    waiter.start()
    release.set()
    waiter.join(5)
    vault.prefetch()  # already found: no second lookup
    assert calls == ["schedule-maxing-credential-store"]


def test_sync_service_offers_keep_signed_in_only_with_a_store(tmp_path) -> None:
    from app.execution.db import get_connection
    from app.sync.service import SyncService

    connection = get_connection(tmp_path / "app.db")
    try:
        assert SyncService(connection, None, vault=LazyVault(lambda: MemoryVault())).can_keep_signed_in
        assert not SyncService(connection, None, vault=LazyVault(lambda: None)).can_keep_signed_in
        assert not SyncService(connection, None).can_keep_signed_in
    finally:
        connection.close()


def test_credential_prefetch_needs_a_configured_backend(tmp_path) -> None:
    from app.execution.db import get_connection
    from app.sync.service import SyncService

    class Transport:
        base_url = "http://127.0.0.1:9"

    class Vault(LazyVault):
        def prefetch(self) -> None:
            prefetched.append(True)

    prefetched: list = []
    connection = get_connection(tmp_path / "app.db")
    try:
        SyncService(connection, None, vault=Vault(MemoryVault)).prefetch_credentials()
        assert prefetched == []  # no backend: no lookup thread (it would outlive a quick close)
        SyncService(connection, Transport(), vault=Vault(MemoryVault)).prefetch_credentials()
        assert prefetched == [True]
    finally:
        connection.close()
