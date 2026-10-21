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

Pages of a group (shell_state.PAGE_GROUPS: Settings, Account, How to Use)
share one sidebar item; the shell shows the group's top bar above them.

Pages may implement on_show(), set_layout(mode) and on_appearance_changed();
the host calls them when relevant. A page chosen by the user from the
navigation (sidebar or Ctrl+number) also goes to `on_navigate(key)` after it
is shown -- e.g. the Day page then shows today -- while pages shown by the
app itself (Open Day from Week/Month) do not.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from app.ui import diagnostics, theme
from app.ui.activity import input_idle_ms as _input_idle_ms
from app.ui.components import AppButton, Tooltip, font, focus_target
from app.ui.layout import BoundedAnimation, Coalescer
from app.ui.shell_state import NAV_ITEMS, PAGE_KEYS, LayoutMode, ShellState, nav_key, page_group

COLLAPSED_WIDTH = 64
EXPANDED_WIDTH = 236
#: How long the page area must keep its size before the shown page is laid out at it (a trailing debounce).
PAGE_SETTLE_MS = 120
#: The page that stays attached while hidden (AppShell._hide): the one the app opens on and returns to most.
HOME_PAGE = "day"
#: The page left last is detached (AppShell._hide) after staying hidden this long, once there was no input
#: for DETACH_IDLE_MS.
DETACH_AFTER_MS = 30_000
DETACH_IDLE_MS = 2_000
#: A detach waits until no idle callback is pending and there was no input for QUIET_INPUT_MS
#: (AppShell._quiet), looking every QUIET_RETRY_MS, QUIET_ATTEMPTS times at most.
QUIET_INPUT_MS = 300
QUIET_RETRY_MS = 200
QUIET_ATTEMPTS = 150

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
        #: What is drawn now: the status poll asks every few seconds, and a CTk configure redraws the control.
        self._shown: tuple[str, str, str] | None = None

    def show(self, text: str, *, can_sync: bool, syncing: bool) -> None:
        state, label = ("normal" if can_sync and not syncing else "disabled"), ("Syncing..." if syncing else "Sync now")
        previous, self._shown = self._shown, (text, state, label)
        if previous is None or previous[0] != text:
            self.label.configure(text=text)
        if previous is None or previous[1:] != (state, label):
            self.sync_button.configure(state=state, text=label)


class GroupBar(ctk.CTkFrame):
    """The top bar of a page group: one button per page; the shown one is marked by colour and a dot."""

    def __init__(self, parent, *, on_select: Callable[[str], None]) -> None:
        super().__init__(parent, fg_color="transparent")
        self._on_select = on_select
        self.group: tuple[tuple[str, str], ...] = ()
        self.buttons: dict[str, AppButton] = {}
        self.labels: dict[str, str] = {}

    def show(self, group: tuple[tuple[str, str], ...], current: str) -> None:
        if group != self.group:
            for button in self.buttons.values():
                button.destroy()
            self.group, self.buttons, self.labels = group, {}, dict(group)
            for column, (key, label) in enumerate(group):
                self.columnconfigure(column, weight=1, uniform="group")
                button = AppButton(self, label, lambda key=key: self._on_select(key), variant="secondary",
                                   height=40)
                button.grid(row=0, column=column, sticky="ew", padx=4)
                self.buttons[key] = button
        for key, button in self.buttons.items():
            chosen = key == current
            button.configure(fg_color=theme.ACCENT if chosen else theme.SECONDARY_BG,
                             hover_color=theme.ACCENT_HOVER if chosen else theme.SECONDARY_HOVER,
                             text_color=theme.TEXT_ON_ACCENT if chosen else theme.TEXT_PRIMARY,
                             text=("● " if chosen else "") + self.labels[key])


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
        self.host.rowconfigure(2, weight=1)
        self.host.columnconfigure(0, weight=1)
        self.status_bar = StatusBar(self.host)
        self.status_bar.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_M, pady=(theme.SPACE_S, 0))
        # The top bar of a page group (Settings | Account | How to Use), shown above those pages only.
        self.group_bar = GroupBar(self.host, on_select=self.navigate)
        self.group_bar.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_M, 0))
        self.group_bar.grid_remove()
        # The shown page is placed in this area at a fixed pixel size, applied only once the area kept its size
        # for PAGE_SETTLE_MS (a trailing debounce): while the window is being resized -- or the sidebar slides --
        # the page is not laid out and redrawn on every step, only once at the end.
        self.page_area = ctk.CTkFrame(self.host, fg_color=theme.APP_BG, corner_radius=0)
        self.page_area.grid(row=2, column=0, sticky="nsew")
        self.page_sizes = Coalescer(self, self._apply_page_size, delay_ms=PAGE_SETTLE_MS, debounce=True)
        self.page_area.bind("<Configure>", self._page_area_resized, add="+")
        #: The pixel size the shown page is placed at (None: it follows the area, whose size is not known yet).
        self._page_size: tuple[int, int] | None = None
        #: Path names of the hidden pages that live in a withdrawn window of their own (_detach).
        self._detached: set[str] = set()
        #: Hidden pages waiting to be detached or released (_hide, _detach_when_quiet, _release_when_quiet):
        #: key -> pending timer.
        self._detach_pending: dict[str, str] = {}
        #: Light caching (Settings, set_light_caching): only HOME_PAGE and the shown page's section stay built.
        self.light_caching = False
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
            self.sidebar.set_active(nav_key(key))
        group = page_group(key)
        if group:
            self.group_bar.show(group, key)
            self.group_bar.grid()
        else:
            self.group_bar.grid_remove()
        if self.current is not None and self.current != key:
            self._hide(self.current, key)
        page = self.pages[key]
        self._attach(page)
        self._place(page)
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
            old.place_forget()
            self.current = None
            self.show_page(key)
        if old is not None:
            self._detached.discard(str(old))
            old.destroy()

    # -- hidden pages (native windows) ---------------------------------------------------

    def park(self, key: str) -> None:
        """Detach the built, hidden page `key` as soon as that is safe (e.g. one pre-built in idle time)."""
        if key != self.current and key in self.pages:
            self._detach_when_quiet(key)

    def _quiet(self) -> bool:
        """
        No idle callback is pending and there was no input just now. A `wm` command runs every pending idle
        callback at once, so detaching in the middle of sliced work (a page building its rows in idle slices)
        would turn the slices into one long block.
        """
        try:
            for ident in self.tk.splitlist(self.tk.call("after", "info")):
                if self.tk.splitlist(self.tk.call("after", "info", ident))[-1] == "idle":
                    return False
        except tk.TclError:
            return False
        return _input_idle_ms() >= QUIET_INPUT_MS

    def _detach_when_quiet(self, key: str, attempts: int = 0) -> None:
        self._detach_pending.pop(key, None)
        if key == self.current or key not in self.pages or not self.winfo_exists():
            return
        if self._quiet():
            self._detach(self.pages[key])
        elif attempts < QUIET_ATTEMPTS:  # else it stays an attached hidden page (as before detaching existed)
            self._detach_pending[key] = self.after(QUIET_RETRY_MS,
                                                   lambda: self._detach_when_quiet(key, attempts + 1))

    def _hide(self, key: str, showing: str) -> None:
        """
        The page `key` is left for `showing`. Attached hidden pages are bounded: only the home page (Day)
        and `key`, the page just left, stay attached -- going back to either is instant. Every other built
        hidden page is detached (_detach) as soon as that is safe (_detach_when_quiet); going back to it then
        costs a re-layout (0.2-0.7 s, about a first visit). Without this bound, opening every page made the
        window hold ~3,800 native child windows instead of ~450, and dragging it that much heavier. The page
        just left is detached too once it stayed hidden for DETACH_AFTER_MS with no input for DETACH_IDLE_MS.
        With light caching (set_light_caching), pages outside the shown page's section are released instead.
        """
        self.pages[key].place_forget()
        self._cancel_detach(key)
        released = self._release_others(showing) if self.light_caching else set()
        if key != HOME_PAGE and key not in released:
            self._detach_pending[key] = self.after(DETACH_AFTER_MS, lambda: self._detach_when_idle(key))
        for other, page in self.pages.items():
            if (other in (key, showing, HOME_PAGE) or other in released or not getattr(page, "built", True)
                    or str(page) in self._detached):
                continue
            self._cancel_detach(other)
            self._detach_pending[other] = self.after(QUIET_RETRY_MS, lambda other=other: self._detach_when_quiet(other))

    # -- light caching ---------------------------------------------------------------------

    def set_light_caching(self, on: bool) -> None:
        """
        Light caching: while a page is shown, only HOME_PAGE (Day) and the pages of the shown page's section
        (its group, e.g. Settings | Account | How to Use) stay built; every other built page is released
        (DeferredPage.release) and built anew when it is shown again. Fewer widgets and native windows are
        kept, at the cost of a first-visit build on every return to another section.
        """
        self.light_caching = on
        if on and self.current is not None:
            self._release_others(self.current)

    @staticmethod
    def kept_pages(showing: str) -> set[str]:
        """The pages light caching keeps built while `showing` is shown: its section and the home page."""
        return {showing, HOME_PAGE, *(page for page, _ in page_group(showing))}

    def _release_others(self, showing: str) -> set[str]:
        """Release every built page outside kept_pages(showing) once that is safe; their keys."""
        kept, released = self.kept_pages(showing), set()
        for other, page in self.pages.items():
            if other in kept or not hasattr(page, "release") or not getattr(page, "built", False):
                continue
            self._cancel_detach(other)
            self._detach_pending[other] = self.after(QUIET_RETRY_MS,
                                                     lambda other=other: self._release_when_quiet(other))
            released.add(other)
        return released

    def _release_when_quiet(self, key: str, attempts: int = 0) -> None:
        """Release `key` when no idle work is pending and there was no input just now (as _detach_when_quiet)."""
        self._detach_pending.pop(key, None)
        if (not self.light_caching or self.current is None or key in self.kept_pages(self.current)
                or key not in self.pages or not self.winfo_exists()):
            return
        if self._quiet() or attempts >= QUIET_ATTEMPTS:  # unlike a detach, it is released in the end anyway
            with diagnostics.span("shell.release_page"):
                self.pages[key].release()
        else:
            self._detach_pending[key] = self.after(QUIET_RETRY_MS,
                                                   lambda: self._release_when_quiet(key, attempts + 1))

    def _detach_when_idle(self, key: str) -> None:
        self._detach_pending.pop(key, None)
        if key == self.current or key not in self.pages or not self.winfo_exists():
            return
        if _input_idle_ms() < DETACH_IDLE_MS:  # the user is busy: look again later (a detach is ~50 ms of work)
            self._detach_pending[key] = self.after(DETACH_IDLE_MS, lambda: self._detach_when_idle(key))
            return
        self._detach_when_quiet(key)

    def _cancel_detach(self, key: str) -> None:
        timer = self._detach_pending.pop(key, None)
        if timer is not None:
            try:
                self.after_cancel(timer)
            except tk.TclError:
                pass

    def destroy(self) -> None:
        for key in list(self._detach_pending):
            self._cancel_detach(key)
        super().destroy()

    def _detach(self, page) -> None:
        """
        Hide `page` in a withdrawn window of its own (Tk's `wm manage`) instead of as a hidden child:
        moving or resizing the application window costs the Windows compositor work for every native child
        window it holds, hidden ones included (docs/desktop-performance-round3.md, "Round 3b"). Shown again,
        it becomes a child again (_attach). Widgets are always built while their page is attached.
        """
        if str(page) in self._detached:
            return
        width, height = page.winfo_width(), page.winfo_height()
        page.place_forget()
        try:
            page.tk.call("wm", "manage", page._w)
            page.tk.call("wm", "withdraw", page._w)
            if width > 1 and height > 1:  # keeps its size: shown again at the same size, nothing is laid out anew
                page.tk.call("wm", "geometry", page._w, f"{width}x{height}")
        except tk.TclError:
            return  # it stays a hidden child window, as before
        self._detached.add(str(page))

    def _attach(self, page) -> None:
        for key, value in self.pages.items():  # shown again: no pending detach
            if value is page:
                self._cancel_detach(key)
        if str(page) in self._detached:
            self._detached.discard(str(page))
            page.tk.call("wm", "forget", page._w)

    # -- page size (resizing) ------------------------------------------------------------

    def _place(self, page) -> None:
        """Place `page` over the page area: at the settled pixel size, or following the area until it is known."""
        if self._page_size is None:
            # Tk's own command: CustomTkinter's place() refuses sizes (and would scale them).
            page.tk.call("place", "configure", page._w, "-in", self.page_area._w, "-x", 0, "-y", 0,
                         "-relwidth", 1, "-relheight", 1, "-width", 0, "-height", 0)
        else:
            width, height = self._page_size
            page.tk.call("place", "configure", page._w, "-in", self.page_area._w, "-x", 0, "-y", 0,
                         "-relwidth", 0, "-relheight", 0, "-width", width, "-height", height)

    def _page_area_resized(self, event) -> None:
        if event.width <= 1 or event.height <= 1:  # not mapped yet, or minimized: nothing to fit
            return
        if self._page_size is None:  # the first real size: apply it now (start-up must not wait)
            self._page_size = (event.width, event.height)
            if self.current is not None:
                self._place(self.pages[self.current])
            return
        if (event.width, event.height) != self._page_size:
            self.page_sizes.request()

    def _apply_page_size(self) -> None:
        width, height = self.page_area.winfo_width(), self.page_area.winfo_height()
        if width <= 1 or height <= 1 or (width, height) == self._page_size:
            return
        self._page_size = (width, height)
        if self.current is not None:
            with diagnostics.span("shell.page_resize"):
                self._place(self.pages[self.current])

    @staticmethod
    def state_keys() -> set[str]:
        return set(PAGE_KEYS)

    # -- sidebar ----------------------------------------------------------------------

    def toggle_sidebar(self) -> None:
        opened = self.state.toggle_sidebar()
        self.sidebar.show_open(opened)
        if opened:
            focus_target(self.sidebar.buttons[self.state.nav_key]).focus_set()

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
