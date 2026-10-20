"""When a shown page may skip re-reading (app/ui/page_freshness.py) and AppServices.data_version, without Tk."""

from __future__ import annotations

from app.ui.page_freshness import data_key, unchanged


class _Services:
    def __init__(self, version) -> None:
        self.version = version

    def data_version(self):
        return self.version


class _Root:
    def __init__(self, services) -> None:
        self.services = services


class _Widget:
    def __init__(self, root) -> None:
        self._root = root

    def winfo_toplevel(self):
        return self._root


def test_a_key_combines_the_data_version_with_the_view() -> None:
    services = _Services((7, 0))
    page = _Widget(_Root(services))
    key = data_key(page, "2026-10-20", "week")
    assert key == ((7, 0), "2026-10-20", "week")
    assert unchanged(data_key(page, "2026-10-20", "week"), key)
    services.version = (8, 0)  # something was written
    assert not unchanged(data_key(page, "2026-10-20", "week"), key)
    assert not unchanged(data_key(page, "2026-10-27", "week"), key)  # another period


def test_without_a_known_version_every_show_re_reads() -> None:
    assert data_key(_Widget(_Root(None))) is None  # e.g. a page used outside the app
    assert data_key(_Widget(_Root(_Services(None)))) is None  # e.g. closed storage
    assert not unchanged(None, None)


def test_app_services_data_version_follows_writes_and_workspace_switches(tmp_path) -> None:
    from app.planning.models import Task
    from app.ui.app_services import open_app_services

    services = open_app_services(tmp_path / "app.db", timezone="UTC", project_root=str(tmp_path),
                                 background_sync=False)
    try:
        first = services.data_version()
        services.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()  # a read
        assert services.data_version() == first
        assert services.planning_controller.add_or_update_task(Task(name="Study", category="study",
                                                                    estimated_duration_minutes=30)).ok
        written = services.data_version()
        assert written != first
        services.switch_workspace()
        assert services.data_version() != written  # another workspace: another view of the data
    finally:
        services.close()
    assert services.data_version() is None
