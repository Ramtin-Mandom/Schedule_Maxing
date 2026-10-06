"""Headless tests of the desktop shell's foundations (Milestone 4, Prompt 1): the theme
(readable light and dark palettes, centralized category colors that never remap data),
the UI settings boundary, the shell/navigation state with its responsive breakpoints,
the event-loop helpers that keep resizing stable, and the default worker-result guard.
No display is needed: widgets are stood in for by small fakes."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.ui import theme
from app.ui.background import ControllerResult, WorkerRegistry, run_in_background
from app.ui.components import ContextMenu, MenuItem
from app.ui.layout import BoundedAnimation, Coalescer
from app.ui.shell_state import (
    DEFAULT_PAGE,
    HYSTERESIS,
    MEDIUM_MIN,
    NAV_ITEMS,
    WIDE_MIN,
    LayoutMode,
    ShellState,
    layout_for_width,
)
from app.ui.ui_settings import SCALES, UISettings, UISettingsStore, settings_path_for
from tests.ui.test_app_services import FakeWidget, _wait_idle


@pytest.mark.parametrize("system, menu_key", [("win32", "<App>"), ("x11", "<Menu>"), ("aqua", None)])
def test_context_menu_uses_platform_key_and_keeps_keyboard_access(system, menu_key):
    class Widget:
        def __init__(self):
            self.tk = self
            self.bindings = {}

        def call(self, *args):
            assert args == ("tk", "windowingsystem")
            return system

        def bind(self, sequence, callback, add):
            assert sequence in {"<Button-3>", "<Shift-F10>", menu_key}
            assert add == "+"
            self.bindings[sequence] = callback

        def winfo_rootx(self):
            return 100

        def winfo_rooty(self):
            return 200

    widget = Widget()
    menu = ContextMenu(widget)
    calls = []
    items = [MenuItem("Edit", lambda: None)]
    menu.popup = lambda entries, x, y: calls.append((entries, x, y))
    menu.attach(widget, lambda event: items if event is None else [])
    expected = {"<Button-3>", "<Shift-F10>"} | ({menu_key} if menu_key else set())
    assert set(widget.bindings) == expected
    for key in expected - {"<Button-3>"}:
        assert widget.bindings[key](None) == "break"
        assert calls[-1] == (items, 124, 224)

# -----------------------------------------------------------------------------
# Theme
# -----------------------------------------------------------------------------

TEXT_ON_SURFACES = [
    (theme.TEXT_PRIMARY, theme.CARD_BG), (theme.TEXT_PRIMARY, theme.APP_BG), (theme.TEXT_PRIMARY, theme.INPUT_BG),
    (theme.TEXT_PRIMARY, theme.SECONDARY_BG), (theme.TEXT_MUTED, theme.CARD_BG), (theme.TEXT_MUTED, theme.APP_BG),
    (theme.TEXT_MUTED, theme.SUBTLE_BG), (theme.TEXT_ON_ACCENT, theme.ACCENT), (theme.TEXT_ON_ACCENT, theme.DANGER),
    (theme.TEXT_ON_ACCENT, theme.SUCCESS), (theme.TEXT_ON_ACCENT, theme.NEUTRAL_BG),
    (theme.SIDEBAR_TEXT, theme.SIDEBAR_BG), (theme.TEXT_ON_ACCENT, theme.SIDEBAR_ACTIVE),
]


@pytest.mark.parametrize("mode", [theme.LIGHT, theme.DARK])
def test_every_text_color_is_readable_on_its_surface_in_both_appearances(mode: str) -> None:
    for foreground, background in TEXT_ON_SURFACES:
        ratio = theme.contrast_ratio(theme.resolve(foreground, mode), theme.resolve(background, mode))
        assert ratio >= 4.5, (mode, foreground, background, round(ratio, 2))
    for tone in theme.TONES.values():
        assert theme.contrast_ratio(theme.resolve(tone.foreground, mode), theme.resolve(tone.background, mode)) >= 4.5
    for name in [*theme.CATEGORY_STYLES, "lab", "Volunteering", "x"]:
        style = theme.category_style(name)
        assert theme.contrast_ratio(theme.resolve(style.text, mode), theme.resolve(style.fill, mode)) >= 4.5, name


def test_light_and_dark_palettes_are_really_different() -> None:
    for token in (theme.APP_BG, theme.CARD_BG, theme.TEXT_PRIMARY, theme.CANVAS_BG):
        assert theme.resolve(token, theme.LIGHT) != theme.resolve(token, theme.DARK)


def test_category_colors_are_central_stable_and_never_remap_the_category() -> None:
    assert theme.category_style("Study") == theme.category_style("study")  # case-insensitive lookup
    assert theme.category_style("sleep") != theme.category_style("study")
    unknown = theme.category_style("Lab")
    assert unknown == theme.category_style("lab") == theme.category_style(" lab ")  # stable across runs/spelling
    assert unknown not in theme.CATEGORY_STYLES.values()  # its own pastel, not silently "other"
    assert theme.category_style(None) == theme.category_style("other")
    assert theme.resolve("#123456") == "#123456"  # plain colors pass through


# -----------------------------------------------------------------------------
# UI settings
# -----------------------------------------------------------------------------


def test_ui_settings_default_round_trip_and_live_beside_the_database(tmp_path: Path) -> None:
    store = UISettingsStore(settings_path_for(tmp_path / "data" / "executions.db"))
    assert store.path == (tmp_path / "data" / "ui_settings.json").resolve()
    assert store.load() == UISettings()  # no file: light, 100%
    assert store.save(UISettings().with_appearance("dark").with_scale(1.15))
    assert store.load() == UISettings(appearance="dark", ui_scale=1.15)
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "appearance": "dark", "ui_scale": 1.15, "language": "en",
        # the update preferences (app/ui/update_controller.py), at their defaults
        "check_for_updates": True, "skipped_version": None, "last_update_check": None,
    }
    assert not store.path.with_name("ui_settings.json.tmp").exists()  # written atomically


def test_bad_ui_settings_fall_back_per_field_and_never_block_startup(tmp_path: Path) -> None:
    store = UISettingsStore(tmp_path / "ui_settings.json")
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == UISettings()
    store.path.write_text(json.dumps({"appearance": "neon", "ui_scale": 1.3, "token": "x"}), encoding="utf-8")
    assert store.load() == UISettings(appearance="light", ui_scale=1.3)  # the valid field survives
    store.path.write_text("[1, 2]", encoding="utf-8")
    assert store.load() == UISettings()
    with pytest.raises(ValueError):
        UISettings().with_scale(7.0)
    with pytest.raises(ValueError):
        UISettings().with_appearance("sepia")
    assert set(SCALES) >= {1.0}


def test_an_unwritable_settings_location_is_reported_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "file_not_folder"
    blocker.write_text("", encoding="utf-8")
    assert UISettingsStore(blocker / "ui_settings.json").save(UISettings()) is False


# -----------------------------------------------------------------------------
# Shell state
# -----------------------------------------------------------------------------


def test_navigation_starts_on_day_with_a_collapsed_sidebar() -> None:
    state = ShellState()
    assert DEFAULT_PAGE == "day" and state.page == "day" and state.sidebar_open is False
    assert [item.label for item in NAV_ITEMS] == [
        "Day Schedule", "Week Schedule", "Month Schedule", "Project Schedule",
        "Productivity", "Settings", "Account", "How to Use", "About",
    ]
    assert len({item.glyph for item in NAV_ITEMS}) == len(NAV_ITEMS)  # every collapsed item is distinguishable
    with pytest.raises(ValueError):
        state.select("nowhere")


def test_layout_breakpoints_have_hysteresis_so_resizing_never_flaps() -> None:
    assert layout_for_width(WIDE_MIN) == LayoutMode.WIDE
    assert layout_for_width(WIDE_MIN - 1) == LayoutMode.MEDIUM
    assert layout_for_width(MEDIUM_MIN - 1) == LayoutMode.NARROW
    # Near a breakpoint, the current layout is kept.
    assert layout_for_width(WIDE_MIN - HYSTERESIS + 1, LayoutMode.WIDE) == LayoutMode.WIDE
    assert layout_for_width(WIDE_MIN + HYSTERESIS - 1, LayoutMode.MEDIUM) == LayoutMode.MEDIUM
    assert layout_for_width(MEDIUM_MIN + HYSTERESIS - 1, LayoutMode.NARROW) == LayoutMode.NARROW

    state = ShellState()
    changes = [state.resize(width) for width in range(WIDE_MIN + 60, MEDIUM_MIN - 60, -7)]  # a slow drag inward
    changes += [state.resize(width) for width in range(MEDIUM_MIN - 60, WIDE_MIN + 60, 7)]  # and back out
    assert [mode for mode in changes if mode is not None] == [
        LayoutMode.WIDE, LayoutMode.MEDIUM, LayoutMode.NARROW, LayoutMode.MEDIUM, LayoutMode.WIDE]
    wobble = [state.resize(WIDE_MIN + offset) for offset in (-10, 5, -12, 3, -20, 8)]  # jitter at the edge
    assert wobble == [None] * 6


def test_a_narrow_window_closes_the_sidebar_and_navigation_keeps_selections() -> None:
    state = ShellState()
    state.resize(WIDE_MIN + 100)
    assert state.toggle_sidebar() is True
    assert state.select("week") is True and state.sidebar_open is True  # wide: stays open
    state.resize(MEDIUM_MIN - 100)
    assert state.layout == LayoutMode.NARROW and state.sidebar_open is False
    state.toggle_sidebar()
    state.select("month")
    assert state.sidebar_open is False  # opening a page on a narrow window closes the sidebar again

    state.remember("week", "2024-06-03")
    state.select("settings")
    state.select("week")
    assert state.selection("week") == "2024-06-03" and state.selection("day", "none") == "none"


# -----------------------------------------------------------------------------
# Event-loop helpers
# -----------------------------------------------------------------------------


class LoopWidget(FakeWidget):
    """FakeWidget plus after_idle/after_cancel, like the Tk methods the helpers use."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list = []

    def after_idle(self, callback):
        self.after(0, callback)
        return callback

    def after(self, _ms, callback):
        super().after(_ms, callback)
        return callback

    def after_cancel(self, handle) -> None:
        with self.lock:
            self.scheduled = [callback for callback in self.scheduled if callback is not handle]
        self.cancelled.append(handle)


def test_the_coalescer_turns_a_burst_of_requests_into_one_call_and_stops_when_destroyed() -> None:
    widget, calls = LoopWidget(), []
    coalescer = Coalescer(widget, lambda: calls.append(1))
    for _ in range(300):  # e.g. one per <Configure> event of a drag-resize
        coalescer.request()
    assert len(widget.scheduled) == 1
    widget.run_pending()
    assert calls == [1] and coalescer.runs == 1 and not coalescer.pending

    coalescer.request()
    widget.exists = False  # the widget was destroyed before the idle call ran
    widget.run_pending()
    assert calls == [1]
    coalescer.request()
    assert widget.scheduled == []  # nothing is scheduled on a destroyed widget


def test_the_bounded_animation_never_schedules_more_than_its_steps_and_stops_on_destroy() -> None:
    widget, fractions, done = LoopWidget(), [], []
    animation = BoundedAnimation(widget, fractions.append, steps=5, on_done=lambda: done.append(True))
    animation.start()
    animation.start()  # a restart cancels the earlier run instead of doubling it
    for _ in range(20):
        widget.run_pending()
    assert fractions == [0.2, 0.4, 0.6, 0.8, 1.0] and done == [True]
    assert animation.scheduled == 6 and not animation.running  # 1 cancelled + 5 steps

    fractions.clear()
    animation.start()
    widget.run_pending()
    widget.exists = False
    for _ in range(10):
        widget.run_pending()
    assert fractions == [0.2] and not animation.running


# -----------------------------------------------------------------------------
# Worker results after a workspace switch
# -----------------------------------------------------------------------------


def test_the_registry_guard_drops_results_started_before_a_workspace_switch() -> None:
    registry, widget, delivered = WorkerRegistry(), FakeWidget(), []
    epoch = {"value": 0}
    registry.result_guard = lambda: (lambda started=epoch["value"]: epoch["value"] == started)
    release = threading.Event()

    def slow() -> ControllerResult:
        release.wait(timeout=5)
        return ControllerResult.success("old workspace")

    assert run_in_background(widget, slow, delivered.append, registry=registry)  # no explicit guard passed
    epoch["value"] += 1  # the workspace switched while it ran
    release.set()
    _wait_idle(registry)
    widget.run_pending()
    assert delivered == []

    run_in_background(widget, lambda: ControllerResult.success("current"), delivered.append, registry=registry)
    _wait_idle(registry)
    widget.run_pending()
    assert [result.value for result in delivered] == ["current"]
