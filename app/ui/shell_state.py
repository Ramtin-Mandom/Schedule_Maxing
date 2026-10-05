"""
app/ui/shell_state.py

The desktop shell's state, Tk-free (tests/ui/test_shell_state.py): which
page is shown, whether the sidebar is open, which responsive layout the
window width calls for, and the date each page last showed.

Navigation: Day, Week and Month Schedule, Project Schedule, Productivity (the existing execution analytics; Execute stays in
each schedule page), Settings, Account, How to Use (the in-app guide) and
About. The app starts on Day with the sidebar collapsed; the sidebar's open
state is never persisted.

Layout: the page width (in logical pixels, i.e. divided by the interface
scale) picks WIDE / MEDIUM / NARROW. A small hysteresis band keeps a window
resized around a breakpoint from flipping back and forth, and the layout is
recomputed only when the mode actually changes -- never per pixel. Going
narrow closes an open sidebar, and choosing a page on a narrow window closes
it again, so navigation never covers the page it opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    #: A short symbol shown in the collapsed sidebar (the label is shown when it is open, and as its tooltip).
    glyph: str
    #: Available in a later desktop step: the page explains that instead of offering unfinished controls.
    placeholder: bool = False


NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("day", "Day Schedule", "D"),
    NavItem("week", "Week Schedule", "W"),
    NavItem("month", "Month Schedule", "M"),
    NavItem("projects", "Project Schedule", "P", placeholder=True),
    NavItem("productivity", "Productivity", "%"),
    NavItem("settings", "Settings", "S"),
    NavItem("account", "Account", "@", placeholder=True),
    NavItem("guide", "How to Use", "?"),
    NavItem("about", "About", "i", placeholder=True),
)
NAV_KEYS = tuple(item.key for item in NAV_ITEMS)
DEFAULT_PAGE = "day"
SCHEDULE_PAGES = ("day", "week", "month")


class LayoutMode(str, Enum):
    #: Three columns: task input | schedule | task manager.
    WIDE = "wide"
    #: Schedule beside one switchable side panel.
    MEDIUM = "medium"
    #: One panel at a time (schedule, task input, task manager), chosen by a switcher.
    NARROW = "narrow"


#: Minimum page widths (logical px) of WIDE and MEDIUM, and the hysteresis band around them.
WIDE_MIN = 1260
MEDIUM_MIN = 820
HYSTERESIS = 24


def layout_for_width(width: float, current: LayoutMode | None = None) -> LayoutMode:
    """The layout for a page `width` wide; near a breakpoint the current layout is kept."""
    if current is not None:
        if current == LayoutMode.WIDE and width >= WIDE_MIN - HYSTERESIS:
            return LayoutMode.WIDE
        if current == LayoutMode.MEDIUM and MEDIUM_MIN - HYSTERESIS <= width < WIDE_MIN + HYSTERESIS:
            return LayoutMode.MEDIUM
        if current == LayoutMode.NARROW and width < MEDIUM_MIN + HYSTERESIS:
            return LayoutMode.NARROW
    if width >= WIDE_MIN:
        return LayoutMode.WIDE
    if width >= MEDIUM_MIN:
        return LayoutMode.MEDIUM
    return LayoutMode.NARROW


class ShellState:
    def __init__(self, *, page: str = DEFAULT_PAGE) -> None:
        if page not in NAV_KEYS:
            raise ValueError(f"unknown page {page!r}")
        self.page = page
        self.sidebar_open = False
        self.layout: LayoutMode | None = None
        self._selections: dict[str, object] = {}

    def select(self, page: str) -> bool:
        """Show `page`; True if it changed. On a narrow window an open sidebar closes."""
        if page not in NAV_KEYS:
            raise ValueError(f"unknown page {page!r}")
        if self.layout == LayoutMode.NARROW:
            self.sidebar_open = False
        changed = page != self.page
        self.page = page
        return changed

    def toggle_sidebar(self) -> bool:
        self.sidebar_open = not self.sidebar_open
        return self.sidebar_open

    def close_sidebar(self) -> bool:
        """Close the sidebar; True if it was open."""
        was_open, self.sidebar_open = self.sidebar_open, False
        return was_open

    def resize(self, width: float) -> LayoutMode | None:
        """Record the page width; the new layout if it changed, else None."""
        mode = layout_for_width(width, self.layout)
        if mode == self.layout:
            return None
        self.layout = mode
        if mode == LayoutMode.NARROW:
            self.sidebar_open = False
        return mode

    def remember(self, page: str, selection: object) -> None:
        """The date/week/month a page shows, so returning to it (or reopening a view) keeps it."""
        self._selections[page] = selection

    def selection(self, page: str, default: object = None) -> object:
        return self._selections.get(page, default)
