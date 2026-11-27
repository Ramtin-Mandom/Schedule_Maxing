"""
The bound on detached pages and the shared light caching choice (app/ui/shell.py PagePool), without windows:
the caches and pages are stand-ins that record what the pool asks of them.
"""

from __future__ import annotations

from app.ui.shell import MAX_DETACHED, PagePool


class Page:
    def __init__(self, name: str, *, releasable: bool = True) -> None:
        self.name, self.built = name, True
        if releasable:
            self.release = lambda: True

    def __str__(self) -> str:
        return f".!{self.name}"


class Cache:
    def __init__(self, pool: PagePool, current: str | None = None) -> None:
        self.current, self.unbuilt, self.released_for = current, [], []
        pool.register(self)

    def unbuild(self, page) -> None:
        self.unbuilt.append(page.name)

    def release_others(self, showing: str) -> set[str]:
        self.released_for.append(showing)
        return set()


def detach(pool: PagePool, cache: Cache, *pages: Page) -> None:
    for page in pages:
        pool.detached(cache, page)


def test_at_most_three_detached_pages_keep_their_widgets_by_default() -> None:
    assert MAX_DETACHED == 3 and PagePool().limit == 3


def test_past_the_limit_the_page_shown_longest_ago_is_released() -> None:
    pool = PagePool(limit=3)
    cache = Cache(pool)
    week, month, budget, projects = (Page(name) for name in ("week", "month", "budget", "projects"))
    for page in (week, month, budget, projects):
        pool.shown(page)
    detach(pool, cache, month, week, budget)  # the order they were detached in does not matter
    assert cache.unbuilt == [] and len(pool.held) == 3
    detach(pool, cache, projects)
    assert cache.unbuilt == ["week"]  # shown first, so longest ago
    assert sorted(pool.held) == [".!budget", ".!month", ".!projects"]


def test_showing_a_page_again_moves_it_to_the_back_of_the_queue() -> None:
    pool = PagePool(limit=2)
    cache = Cache(pool)
    week, month, budget = Page("week"), Page("month"), Page("budget")
    for page in (week, month, budget):
        pool.shown(page)
    detach(pool, cache, week, month)
    pool.shown(week)  # attached again: no longer held, and now the most recent
    detach(pool, cache, week, budget)
    assert cache.unbuilt == ["month"] and sorted(pool.held) == [".!budget", ".!week"]


def test_a_page_never_shown_goes_first_and_a_page_used_on_demand_counts_as_recent() -> None:
    pool = PagePool(limit=1)
    cache = Cache(pool)
    prebuilt, section = Page("week"), Page("section")
    pool.used(section)  # built because something read one of its widgets
    detach(pool, cache, section, prebuilt)
    assert cache.unbuilt == ["week"] and pool.held == [".!section"]


def test_pages_that_cannot_be_released_or_are_not_built_are_not_counted() -> None:
    pool = PagePool(limit=1)
    cache = Cache(pool)
    plain, empty = Page("plain", releasable=False), Page("empty")
    empty.built = False
    detach(pool, cache, plain, empty, Page("week"))
    assert cache.unbuilt == [] and pool.held == [".!week"]


def test_a_forgotten_or_unregistered_page_leaves_the_queue() -> None:
    pool = PagePool(limit=1)
    first, second = Cache(pool), Cache(pool)
    gone, kept = Page("gone"), Page("kept")
    detach(pool, first, gone)
    pool.forget(gone)  # e.g. destroyed by a workspace switch
    detach(pool, second, kept)
    assert first.unbuilt == [] and pool.held == [".!kept"]
    pool.unregister(second)  # its owner (a Settings page) was destroyed
    assert pool.held == []


def test_light_caching_asks_every_cache_with_a_shown_page_to_release_the_rest() -> None:
    pool = PagePool()
    shell, settings, idle = Cache(pool, "settings"), Cache(pool, "general"), Cache(pool)
    pool.set_light_caching(True)
    assert pool.light_caching and shell.released_for == ["settings"] and settings.released_for == ["general"]
    assert idle.released_for == []
    pool.set_light_caching(False)  # off: nothing is rebuilt or released
    assert not pool.light_caching and shell.released_for == ["settings"]
