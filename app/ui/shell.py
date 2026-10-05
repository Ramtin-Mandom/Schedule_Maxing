"""
app/ui/shell.py

The desktop shell (Milestone 4): a left sidebar that starts collapsed and
opens with a keyboard-focusable hamburger button (or Ctrl+B), and a page
host that shows one page at a time. Tk widgets only; the state they draw --
current page, sidebar open, responsive layout, remembered selections -- is
app/ui/shell_state.py's ShellState, tested headlessly.

Stable layout while moving/resizing/minimizing:

- Only the visible page is gridded; hidden pages are grid_remove()d, so a
  resize lays out one page, not all of them.
- <Configure> events of the page host are coalesced (app/ui/layout.py) into
  one width check after the event burst; pages are re-laid out only when
  the responsive mode changes (ShellState.resize, with hysteresis), never
  per pixel, and nothing here changes the window's own geometry, so no
  Configure feedback loop can form. A minimized window (width 1) is ignored.
- The sidebar animation has a fixed number of steps and stops when the
  sidebar is destroyed.

Pages may implement on_show(), set_layout(mode) and on_appearance_changed();
the host calls them when relevant. A page chosen by the user from the
navigation (sidebar or Ctrl+number) also goes to `on_navigate(key)` after it
is shown -- e.g. the Day page then shows today -- while pages shown by the
app itself (Open Day from Week/Month) do not.
"""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from app.ui import diagnostics, theme
from app.ui.components import AppButton, Tooltip, font, focus_target
from app.ui.layout import BoundedAnimation, Coalescer
from app.ui.shell_state import NAV_ITEMS, LayoutMode, ShellState

COLLAPSED_WIDTH = 64
EXPANDED_WIDTH = 236

_SIDEBAR_BUTTON = dict(fg_color="transparent", hover_color=theme.SIDEBAR_HOVER, text_color=theme.SIDEBAR_TEXT,
                       border_color=theme.SIDEBAR_BG)
_SIDEBAR_ACTIVE = dict(fg_color=theme.SIDEBAR_ACTIVE, hover_color=theme.SIDEBAR_ACTIVE, text_color=theme.TEXT_ON_ACCENT,
                       border_color=theme.SIDEBAR_ACTIVE)


class Sidebar(ctk.CTkFrame):
    def __init__(self, parent, state: ShellState, *, on_select: Callable[[str], None],
                 on_toggle: Callable[[], None]) -> None:
        super().__init__(parent, width=COLLAPSED_WIDTH, corner_radius=0, fg_color=theme.SIDEBAR_BG)
        self.grid_propagate(False)
        self.columnconfigure(0, weight=1)
        self._state = state
        self._width = COLLAPSED_WIDTH
        self._animation_from = self._animation_to = COLLAPSED_WIDTH
        #: What is drawn now, so a page switch repaints only what changed (each button repaint costs ~3 ms).
        self._active_key: str | None = None
        self._labels_open: bool | None = None
        self.animation = BoundedAnimation(self, self._animate, steps=6, interval_ms=15, on_done=self._settle)

        self.menu_button = AppButton(self, "☰", on_toggle, variant="ghost", height=40, width=44,
                                     style=_SIDEBAR_BUTTON, font=font(18, "bold"))
        self.menu_button.grid(row=0, column=0, sticky="w", padx=10, pady=(14, 10))
        self.menu_tooltip = Tooltip(self.menu_button, "Open navigation (Ctrl+B)")
        self.title_label = ctk.CTkLabel(self, text="Schedule Maxing", font=font(18, "bold"),
                                        text_color=theme.TEXT_ON_ACCENT, anchor="w")

        self.buttons: dict[str, AppButton] = {}
        self.markers: dict[str, ctk.CTkFrame] = {}
        self.tooltips: dict[str, Tooltip] = {}
        for row, item in enumerate(NAV_ITEMS, start=2):
            marker = ctk.CTkFrame(self, width=4, height=28, corner_radius=2, fg_color=theme.SIDEBAR_BG)
            marker.grid(row=row, column=0, sticky="w", padx=(3, 0))
            button = AppButton(self, item.glyph, lambda key=item.key: on_select(key), variant="ghost", height=40,
                               anchor="center", style=_SIDEBAR_BUTTON, font=font(theme.SIZE_BODY, "bold"))
            button.grid(row=row, column=0, sticky="ew", padx=(10, 10), pady=3)
            self.buttons[item.key] = button
            self.markers[item.key] = marker
            self.tooltips[item.key] = Tooltip(button, item.label)
        self.rowconfigure(len(NAV_ITEMS) + 2, weight=1)
        self.footer = ctk.CTkLabel(self, text="", font=font(theme.SIZE_CAPTION), text_color=theme.SIDEBAR_MUTED,
                                   anchor="w", justify="left", wraplength=EXPANDED_WIDTH - 40)
        self._apply_labels(open_=False)

    # -- state ------------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._state.sidebar_open

    def set_footer(self, text: str) -> None:
        self.footer.configure(text=text)

    def set_active(self, key: str) -> None:
        previous, self._active_key = self._active_key, key
        if previous == key:
            return
        for item_key in (previous, key):  # the other items already look inactive
            if item_key not in self.buttons:
                continue
            active = item_key == key
            self.buttons[item_key].configure(**(_SIDEBAR_ACTIVE if active else _SIDEBAR_BUTTON))
            # A shape cue besides the color: the active item has a bar at its left edge.
            self.markers[item_key].configure(fg_color=theme.TEXT_ON_ACCENT if active else theme.SIDEBAR_BG)

    def show_open(self, open_: bool, *, animate: bool = True) -> None:
        """Draw the sidebar open or collapsed (the state itself belongs to ShellState)."""
        target = EXPANDED_WIDTH if open_ else COLLAPSED_WIDTH
        self.menu_tooltip.text = "Close navigation (Ctrl+B)" if open_ else "Open navigation (Ctrl+B)"
        if not open_:
            self._apply_labels(open_=False)  # labels go first so nothing is squeezed while it narrows
        if not animate or target == self._width:
            self.animation.stop()
            self._set_width(target)
            self._settle()
            return
        self._animation_from, self._animation_to = self._width, target
        self.animation.start()

    def _animate(self, fraction: float) -> None:
        self._set_width(round(self._animation_from + (self._animation_to - self._animation_from) * fraction))

    def _set_width(self, width: int) -> None:
        if width != self._width:
            self._width = width
            self.configure(width=width)

    def _settle(self) -> None:
        self._apply_labels(open_=self._width == EXPANDED_WIDTH)

    def _apply_labels(self, *, open_: bool) -> None:
        if open_ == self._labels_open:
            return  # already drawn this way (every page switch asks again)
        self._labels_open = open_
        for item in NAV_ITEMS:
            button = self.buttons[item.key]
            button.configure(text=f"  {item.label}" if open_ else item.glyph, anchor="w" if open_ else "center")
            self.tooltips[item.key].text = "" if open_ else item.label
        if open_:
            self.title_label.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
            self.footer.grid(row=len(NAV_ITEMS) + 3, column=0, sticky="sew", padx=18, pady=(0, 16))
        else:
            self.title_label.grid_remove()
            self.footer.grid_remove()


class StatusBar(ctk.CTkFrame):
    """
    The compact connection/sync indicator above every page: one line in words
    (e.g. "Signed in -- 2 change(s) waiting", "Offline -- no backend
    configured"), Sync now when it can run, and a way to the Account page.
    """

    def __init__(self, parent) -> None:
        super().__init__(parent, fg_color=theme.CARD_BG, corner_radius=theme.RADIUS_CONTROL, border_width=1,
                         border_color=theme.CARD_BORDER)
        self.columnconfigure(0, weight=1)
        self.label = ctk.CTkLabel(self, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_PRIMARY,
                                  anchor="w", justify="left", wraplength=380)
        self.label.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=4)
        self.sync_button = AppButton(self, "Sync now", None, variant="secondary", height=28, width=96,
                                     font=font(theme.SIZE_SMALL, "bold"))
        self.sync_button.grid(row=0, column=1, padx=(0, 6), pady=4)
        self.account_button = AppButton(self, "Account", None, variant="ghost", height=28, width=84,
                                        font=font(theme.SIZE_SMALL, "bold"))
        self.account_button.grid(row=0, column=2, padx=(0, 6), pady=4)

    def show(self, text: str, *, can_sync: bool, syncing: bool) -> None:
        self.label.configure(text=text)
        self.sync_button.configure(state="normal" if can_sync and not syncing else "disabled",
                                   text="Syncing..." if syncing else "Sync now")


class AppShell(ctk.CTkFrame):
    """Sidebar + page host. `pages` maps a navigation key (or an extra key such as "reward") to its frame."""

    def __init__(self, root, state: ShellState, *, scaling: Callable[[], float] = lambda: 1.0) -> None:
        super().__init__(root, fg_color=theme.APP_BG, corner_radius=0)
        self.state = state
        self._scaling = scaling
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        #: Called with the key of a page the user chose from the navigation, after it is shown.
        self.on_navigate: Callable[[str], None] | None = None
        self.sidebar = Sidebar(self, state, on_select=self.navigate, on_toggle=self.toggle_sidebar)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.host = ctk.CTkFrame(self, fg_color=theme.APP_BG, corner_radius=0)
        self.host.grid(row=0, column=1, sticky="nsew")
        self.host.rowconfigure(1, weight=1)
        self.host.columnconfigure(0, weight=1)
        self.status_bar = StatusBar(self.host)
        self.status_bar.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_M, pady=(theme.SPACE_S, 0))
        self.pages: dict[str, ctk.CTkFrame] = {}
        self.current: str | None = None
        self.layout_checks = Coalescer(self, self._check_layout)
        self.host.bind("<Configure>", lambda _e: self.layout_checks.request(), add="+")

        root.bind("<Control-b>", lambda _e: (self.toggle_sidebar(), "break")[1], add="+")
        root.bind("<Control-B>", lambda _e: (self.toggle_sidebar(), "break")[1], add="+")
        for index, item in enumerate(NAV_ITEMS[:9], start=1):
            root.bind(f"<Control-Key-{index}>", lambda _e, key=item.key: (self.navigate(key), "break")[1], add="+")
        root.bind("<Escape>", self._escape, add="+")

    # -- pages ------------------------------------------------------------------------

    def add_page(self, key: str, page: ctk.CTkFrame) -> None:
        self.pages[key] = page

    def navigate(self, key: str) -> None:
        """The user chose `key` from the navigation: show it, then tell on_navigate."""
        self.show_page(key)
        if self.on_navigate is not None:
            self.on_navigate(key)

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            raise KeyError(key)
        with diagnostics.span(f"shell.show_page.{key}"):
            self._show_page(key)

    def _show_page(self, key: str) -> None:
        if key in self.state_keys():
            self.state.select(key)
            self.sidebar.set_active(key)
        if self.current is not None and self.current != key:
            self.pages[self.current].grid_remove()
        page = self.pages[key]
        page.grid(row=1, column=0, sticky="nsew")
        self.current = key
        if self.state.layout is not None and hasattr(page, "set_layout"):
            page.set_layout(self.state.layout)
        if hasattr(page, "on_show"):
            page.on_show()
        if not self.state.sidebar_open:
            self.sidebar.show_open(False)

    def replace_page(self, key: str, page: ctk.CTkFrame) -> None:
        """Swap in a rebuilt page (e.g. for another workspace); the old one is destroyed, the current page kept."""
        old = self.pages.get(key)
        self.pages[key] = page
        if self.current == key and old is not None:
            old.grid_remove()
            self.current = None
            self.show_page(key)
        if old is not None:
            old.destroy()

    @staticmethod
    def state_keys() -> set[str]:
        return {item.key for item in NAV_ITEMS}

    # -- sidebar ----------------------------------------------------------------------

    def toggle_sidebar(self) -> None:
        opened = self.state.toggle_sidebar()
        self.sidebar.show_open(opened)
        if opened:
            focus_target(self.sidebar.buttons[self.state.page]).focus_set()

    def _escape(self, _event=None) -> None:
        if self.state.sidebar_open and self.state.layout == LayoutMode.NARROW and self.state.close_sidebar():
            self.sidebar.show_open(False)

    # -- responsive layout ------------------------------------------------------------

    def _check_layout(self) -> None:
        width = self.host.winfo_width()
        if width <= 1:  # not mapped yet, or minimized: keep the current layout
            return
        mode = self.state.resize(width / max(self._scaling(), 0.5))
        if mode is None:
            return
        if not self.state.sidebar_open:
            self.sidebar.show_open(False, animate=False)
        page = self.pages.get(self.current) if self.current else None
        if page is not None and hasattr(page, "set_layout"):
            page.set_layout(mode)

    def appearance_changed(self) -> None:
        for page in self.pages.values():
            if hasattr(page, "on_appearance_changed"):
                page.on_appearance_changed()
