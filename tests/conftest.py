"""Shared builders for the core scheduling domain (Task, FixedBlock, DaySchedule,
ScheduledTask). Used by tests/test_constraints.py, test_pert.py, test_reward.py,
test_optimizer.py, and test_data_processor.py so each test only sets the fields
it actually cares about.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.models import DaySchedule, FixedBlock, ScheduledTask, Task, TimeWindow
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


@pytest.fixture
def make_time_window():
    def _make(start: int, end: int) -> TimeWindow:
        return TimeWindow(start_time=start, end_time=end)

    return _make


@pytest.fixture
def make_task():
    def _make(
        name: str = "Task",
        *,
        date: int = 1,
        category: str = "study",
        tag: str = "",
        duration: int = 60,
        priority: int = 5,
        preference_start: int = 0,
        preference_end: int = 1440,
        dependencies: list[str] | None = None,
        fixed: bool = False,
    ) -> Task:
        return Task(
            name=name,
            date=date,
            category=category,
            tag=tag,
            fixed=fixed,
            duration=duration,
            priority=priority,
            preference_time=TimeWindow(
                start_time=preference_start,
                end_time=preference_end,
            ),
            dependencies=dependencies or [],
        )

    return _make


@pytest.fixture
def make_fixed_block():
    def _make(
        name: str = "Fixed",
        *,
        start: int,
        end: int,
        category: str = "fixed",
    ) -> FixedBlock:
        return FixedBlock(
            name=name,
            time_window=TimeWindow(start_time=start, end_time=end),
            category=category,
        )

    return _make


@pytest.fixture
def make_day_schedule():
    def _make(
        *,
        day_start: int = 0,
        day_end: int = 1440,
        fixed_blocks: list[FixedBlock] | None = None,
        tasks: list[Task] | None = None,
    ) -> DaySchedule:
        return DaySchedule(
            time_window=TimeWindow(start_time=day_start, end_time=day_end),
            fixed_blocks=fixed_blocks or [],
            tasks=tasks or [],
        )

    return _make


@pytest.fixture
def make_scheduled_task():
    def _make(
        name: str = "Scheduled",
        *,
        start: int,
        end: int,
        category: str = "study",
        tag: str = "",
        score: float = 0.0,
    ) -> ScheduledTask:
        return ScheduledTask(
            name=name,
            category=category,
            tag=tag,
            time_window=TimeWindow(start_time=start, end_time=end),
            score=score,
        )

    return _make
