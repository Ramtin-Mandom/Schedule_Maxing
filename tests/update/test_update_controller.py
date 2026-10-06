"""
The desktop's update behavior (app/ui/update_controller.py and the worker hand-off used by
app/ui/update_view.py), plus the database carried from one application version to the next.
No window and no network: the fake GitHub and fake Tk root of tests/update_fakes.py.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.execution.backup import backups_dir_for, list_backups
from app.execution.db import LATEST_SCHEMA_VERSION, get_connection, initialize_schema
from app.ui.background import WorkerRegistry, run_in_background
from app.ui.ui_settings import UISettings, UISettingsStore
from app.ui.update_controller import (
    UpdateController,
    automatic_check_due,
    with_automatic_checks,
    with_check_recorded,
    with_skipped_version,
)
from app.update.service import UpdateService
from app.update.transport import UpdateTransportError
from tests.update_fakes import API, REPOSITORY, FakeRoot

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def controller(github, tmp_path: Path, installed: str = "1.3.2", **options) -> UpdateController:
    options.setdefault("launcher", lambda path, arguments: None)
    options.setdefault("frozen", lambda: True)
    return UpdateController(UpdateService(installed_version=installed, repository=REPOSITORY, fetcher=github,
                                          updates_dir=tmp_path / "updates", **options))


# -----------------------------------------------------------------------------
# What a check tells the user
# -----------------------------------------------------------------------------


def test_up_to_date_and_available_are_worded_for_the_user(github, tmp_path):
    github.publish("1.3.2")
    current = controller(github, tmp_path).check().value
    assert current.state == "up_to_date" and not current.offer and "up to date" in current.message

    github.publish("1.4.0")
    newer = controller(github, tmp_path).check().value
    assert newer.offer and newer.release.version == "1.4.0"
    assert "1.4.0 is available" in newer.message and "you have 1.3.2" in newer.message


def test_a_failed_check_is_a_calm_message_and_never_an_error(github, tmp_path):
    github.responses[API] = UpdateTransportError("could not reach the update server (TimeoutError)")
    result = controller(github, tmp_path).check()
    assert result.ok and result.value.state == "unavailable" and not result.value.offer
    assert "Could not check for updates" in result.value.message and "Nothing was changed" in result.value.message


def test_a_skipped_version_is_not_offered_again_but_a_later_one_is(github, tmp_path):
    github.publish("1.4.0")
    updates = controller(github, tmp_path)
    assert updates.check(skipped_version="1.4.0").value.state == "skipped"
    assert not updates.check(skipped_version="v1.4.0").value.offer
    assert updates.check().value.offer                      # "Check now" still shows it
    github.publish("1.4.1")
    assert updates.check(skipped_version="1.4.0").value.offer


def test_the_installed_version_is_not_offered_again_after_updating(github, tmp_path):
    github.publish("1.4.0")
    assert controller(github, tmp_path, installed="1.3.2").check().value.offer
    assert not controller(github, tmp_path, installed="1.4.0").check().value.offer


def test_declining_changes_nothing(github, tmp_path):
    github.publish("1.4.0")
    updates = controller(github, tmp_path)
    assert updates.check().value.offer  # "Later": the user simply does nothing
    assert not (tmp_path / "updates").exists() and len(github.requests) == 1


# -----------------------------------------------------------------------------
# Downloading and installing
# -----------------------------------------------------------------------------


def test_update_now_downloads_verifies_and_starts_the_installer(github, tmp_path):
    started: list = []
    github.publish("1.4.0")
    updates = controller(github, tmp_path, launcher=lambda path, arguments: started.append((path.name, arguments)))
    download = updates.download(updates.check().value.release)
    assert download.ok and download.value.path.is_file()
    assert updates.install(download.value).ok
    assert started == [("ScheduleMaxing-Setup-1.4.0.exe", ("/SILENT", "/NORESTART", "/RELAUNCH=1"))]


def test_download_failures_are_explained_and_start_nothing(github, tmp_path):
    started: list = []
    github.publish("1.4.0", checksums="")
    updates = controller(github, tmp_path, launcher=lambda *args: started.append(args))
    release = updates.check().value.release
    failed = updates.download(release)
    assert not failed.ok and "could not be verified" in failed.error and "Nothing was changed" in failed.error

    github.publish("1.4.0")
    github.responses[release.installer_url] = UpdateTransportError("the download was interrupted (ConnectionError)")
    failed = updates.download(release)
    assert not failed.ok and "could not be downloaded" in failed.error
    assert started == [] and not list((tmp_path / "updates").glob("*"))


def test_a_download_can_be_cancelled(github, tmp_path):
    github.publish("1.4.0")
    updates = controller(github, tmp_path)
    release = updates.check().value.release
    result = updates.download(release, progress=lambda written, total: updates.cancel_download())
    assert not result.ok and "cancelled" in result.error and not list((tmp_path / "updates").glob("*"))
    assert updates.download(release).ok  # and can be started again


def test_a_source_checkout_is_told_why_it_is_not_installed(github, tmp_path):
    github.publish("1.4.0")
    updates = controller(github, tmp_path, frozen=lambda: False)
    result = updates.install(updates.download(updates.check().value.release).value)
    assert not result.ok and "source code" in result.error and not updates.can_install


# -----------------------------------------------------------------------------
# The automatic check and its preferences
# -----------------------------------------------------------------------------


def test_the_automatic_check_runs_once_a_day_and_only_when_enabled():
    fresh = UISettings()
    assert automatic_check_due(fresh, NOW)
    checked = with_check_recorded(fresh, NOW)
    assert not automatic_check_due(checked, NOW + timedelta(hours=23))
    assert automatic_check_due(checked, NOW + timedelta(hours=24))
    assert automatic_check_due(checked, NOW - timedelta(days=2))  # the clock was set back: do not stay silent forever
    assert not automatic_check_due(with_automatic_checks(fresh, False), NOW)
    assert not automatic_check_due(fresh, NOW, enabled=False)  # no repository configured
    assert automatic_check_due(UISettings(last_update_check="not a date"), NOW)


def test_update_preferences_survive_a_restart_and_old_settings_files(tmp_path):
    store = UISettingsStore(tmp_path / "ui_settings.json")
    store.path.write_text('{"appearance": "dark", "ui_scale": 1.15}', encoding="utf-8")  # written by an older version
    loaded = store.load()
    assert loaded.check_for_updates is True and loaded.skipped_version is None and loaded.appearance == "dark"

    changed = with_skipped_version(with_automatic_checks(with_check_recorded(loaded, NOW), False), "1.4.0")
    assert store.save(changed) and store.load() == changed
    assert with_skipped_version(changed, "not-a-version").skipped_version is None

    store.path.write_text('{"check_for_updates": "yes", "skipped_version": 14, "last_update_check": []}', encoding="utf-8")
    assert store.load() == UISettings()  # nonsense falls back to the defaults


# -----------------------------------------------------------------------------
# The interface is never blocked
# -----------------------------------------------------------------------------


def test_a_slow_update_check_does_not_block_the_interface_thread(github, tmp_path):
    github.publish("1.4.0")
    github.gate = threading.Event()  # GitHub does not answer until this is set
    updates = controller(github, tmp_path)
    registry, root, delivered = WorkerRegistry(), FakeRoot(), []

    started = time.monotonic()
    assert run_in_background(root, updates.check, delivered.append, registry=registry, still_current=lambda: True)
    assert time.monotonic() - started < 0.5  # scheduling the check returned at once
    root.run_pending()
    assert delivered == [] and registry.active == 1  # still waiting, and the interface thread is free

    github.gate.set()
    deadline = time.monotonic() + 5
    while not delivered and time.monotonic() < deadline:
        time.sleep(0.01)
        root.run_pending()
    assert len(delivered) == 1 and delivered[0].value.offer  # the result arrives on the interface thread later
    assert registry.shutdown(timeout=5)


# -----------------------------------------------------------------------------
# The database from one application version to the next
# -----------------------------------------------------------------------------


def test_data_saved_by_an_older_version_is_backed_up_and_migrated_by_the_new_one(tmp_path):
    """Application N with schema S, updated to application N+1 with schema S+1: same file, rows kept."""
    path = tmp_path / "executions.db"
    older = LATEST_SCHEMA_VERSION - 1
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_schema(connection, target_version=older)  # what the previous version left on disk
        connection.execute("CREATE TABLE keepsake (note TEXT)")
        connection.execute("INSERT INTO keepsake VALUES ('written before the update')")
    finally:
        connection.close()

    upgraded = get_connection(path)  # the new version's first start
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert upgraded.execute("SELECT note FROM keepsake").fetchone()[0] == "written before the update"
    finally:
        upgraded.close()

    (backup,) = list_backups(backups_dir_for(path), path)
    copy = sqlite3.connect(str(backup))
    try:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == older
        assert copy.execute("SELECT COUNT(*) FROM keepsake").fetchone()[0] == 1
    finally:
        copy.close()

    get_connection(path).close()  # later starts: nothing to migrate, no further copy
    assert len(list_backups(backups_dir_for(path), path)) == 1
