"""
theme.py

The desktop design tokens (Milestone 4 desktop shell): one place for colors,
category colors, type sizes and spacing, in genuine light and dark variants.

Colors are (light, dark) pairs. CustomTkinter widgets take the pair directly
and follow ctk.set_appearance_mode() by themselves; raw Tk widgets (a
tk.Canvas, ttk styles, tk.Menu) take resolve(color), and redraw when the
appearance changes. The historical names (APP_BG, CARD_BG, TEXT_PRIMARY, ...)
are kept, as pairs, so every existing widget module follows the theme.

Category colors are muted pastels, centralized for flexible tasks and for
the real categories of fixed blocks. A category nobody defined (an imported
"lab", say) gets a stable color derived from its name -- the same one every
run -- without the stored category being remapped or renamed.

Kept separate from app.py (which imports from app/ui/, never the other way
round) and free of any Tk call except in resolve()/appearance(), so the
palette itself is tested headlessly (tests/ui/test_theme.py).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: A color token: (light, dark).
Color = tuple[str, str]

LIGHT, DARK = "light", "dark"

# -----------------------------------------------------------------------------
# Surfaces and text
# -----------------------------------------------------------------------------

APP_BG: Color = ("#EEF2F7", "#0E131B")
CARD_BG: Color = ("#FFFFFF", "#161D28")
CARD_BORDER: Color = ("#E2E8F0", "#2A3444")
SUBTLE_BG: Color = ("#F8FAFC", "#1B2330")
INPUT_BG: Color = ("#F8FAFC", "#1F2836")
TEXT_PRIMARY: Color = ("#0F172A", "#E5E9F0")
TEXT_MUTED: Color = ("#5B6B80", "#9AA8BA")
TEXT_ON_ACCENT: Color = ("#FFFFFF", "#FFFFFF")
SHADOW: Color = ("#D8E0EA", "#070A0F")

ACCENT: Color = ("#2563EB", "#2F6FE4")
ACCENT_HOVER: Color = ("#1D4ED8", "#285FC6")
ACCENT_SOFT: Color = ("#DBEAFE", "#1E3357")
SECONDARY_BG: Color = ("#E2E8F0", "#263041")
SECONDARY_HOVER: Color = ("#CBD5E1", "#313D52")
NEUTRAL_BG: Color = ("#334155", "#3A4658")
NEUTRAL_HOVER: Color = ("#1E293B", "#46546A")
DANGER: Color = ("#C62828", "#B83232")
DANGER_HOVER: Color = ("#A11F1F", "#9C2828")
SUCCESS: Color = ("#15803D", "#237A46")
WARNING: Color = ("#B45309", "#E0A33A")
FOCUS_RING: Color = ("#1D4ED8", "#8AB4FF")

CANVAS_BG: Color = ("#F8FAFC", "#121923")
GRID_LINE: Color = ("#E5E7EB", "#222C3A")
GRID_LINE_STRONG: Color = ("#CBD5E1", "#344052")

SIDEBAR_BG: Color = ("#0F172A", "#0A0F17")
SIDEBAR_TEXT: Color = ("#CBD5E1", "#AEB9C9")
SIDEBAR_HOVER: Color = ("#1E293B", "#18212F")
SIDEBAR_ACTIVE: Color = ("#2563EB", "#2F6FE4")
SIDEBAR_MUTED: Color = ("#94A3B8", "#7C8799")


@dataclass(frozen=True)
class Tone:
    """A status tone: its colors and a word that says it without color."""

    label: str
    background: Color
    foreground: Color
    border: Color


TONES: dict[str, Tone] = {
    "info": Tone("Info", ("#E8F0FE", "#18263D"), ("#1E3A8A", "#C7DBFF"), ("#BFD3FB", "#2B4A7A")),
    "success": Tone("Done", ("#E7F6EC", "#15291D"), ("#14532D", "#BDEBCB"), ("#B7E4C7", "#2A5A3B")),
    "warning": Tone("Warning", ("#FEF3E2", "#2E2412"), ("#7A3E06", "#F7D8A8"), ("#F5D3A1", "#5E4722")),
    "error": Tone("Error", ("#FDECEC", "#321A1C"), ("#8A1C1C", "#F9C4C4"), ("#F4C2C2", "#6B2E31")),
}

# -----------------------------------------------------------------------------
# Typography and spacing (points / logical pixels; CustomTkinter scales both)
# -----------------------------------------------------------------------------

FONT_FAMILY = "Segoe UI"
SIZE_TITLE = 24
SIZE_HEADING = 17
SIZE_BODY = 13
SIZE_SMALL = 12
SIZE_CAPTION = 11

SPACE_XS, SPACE_S, SPACE_M, SPACE_L, SPACE_XL = 4, 8, 12, 18, 24
RADIUS_CONTROL, RADIUS_CARD = 12, 18
CONTROL_HEIGHT = 38

# -----------------------------------------------------------------------------
# Category colors
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryStyle:
    fill: Color
    text: Color


CATEGORY_STYLES: dict[str, CategoryStyle] = {
    "study": CategoryStyle(("#C7E2F6", "#274760"), ("#0B3B56", "#DCEEFC")),
    "sleep": CategoryStyle(("#DCD0F5", "#3C3160"), ("#37205A", "#E7DDFB")),
    "food": CategoryStyle(("#F7E1BF", "#5A4523"), ("#5C3600", "#FBE9CC")),
    "meal": CategoryStyle(("#F7E1BF", "#5A4523"), ("#5C3600", "#FBE9CC")),
    "exercise": CategoryStyle(("#CBEBD5", "#28503A"), ("#0F3D22", "#D8F3E1")),
    "work": CategoryStyle(("#D2DAF7", "#2F3C66"), ("#15275E", "#DEE4FB")),
    "class": CategoryStyle(("#D4E4F0", "#2E4556"), ("#163548", "#DDECF6")),
    "event": CategoryStyle(("#F4D3E7", "#58304A"), ("#5B144A", "#F9DFEE")),
    "entertainment": CategoryStyle(("#F4EDBE", "#524B25"), ("#4A3E00", "#F8F1CE")),
    "errand": CategoryStyle(("#F4D2D2", "#5A3131"), ("#5B1717", "#F8DEDE")),
    "other": CategoryStyle(("#DDE3EA", "#394454"), ("#273449", "#E4E9F0")),
    "fixed": CategoryStyle(("#D5DBE3", "#3A4453"), ("#172033", "#E2E7EE")),
}

#: Extra pastels for categories not defined above (picked by a stable hash of the name).
_FALLBACK_STYLES: tuple[CategoryStyle, ...] = (
    CategoryStyle(("#D6EFEA", "#264B45"), ("#113F37", "#D8F2EC")),
    CategoryStyle(("#EADAF2", "#473352"), ("#44215A", "#EFDFF6")),
    CategoryStyle(("#F3E3D3", "#523F2E"), ("#553015", "#F6E6D7")),
    CategoryStyle(("#DCE8D0", "#3A4A2B"), ("#2C4212", "#E3EED8")),
    CategoryStyle(("#D9E1F7", "#303B5E"), ("#1F2F66", "#DFE6FA")),
    CategoryStyle(("#F2D9DF", "#553540"), ("#5A1E2E", "#F6DFE5")),
)


def category_style(category: str | None) -> CategoryStyle:
    """
    The display colors of a category. Known categories (case-insensitive)
    have their own pastel; any other name gets a stable fallback pastel.
    Only the display is affected -- the category itself is never changed.
    """
    key = (category or "other").strip().lower() or "other"
    if key in CATEGORY_STYLES:
        return CATEGORY_STYLES[key]
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return _FALLBACK_STYLES[digest[0] % len(_FALLBACK_STYLES)]


# -----------------------------------------------------------------------------
# Resolving tokens for raw Tk widgets
# -----------------------------------------------------------------------------


def appearance() -> str:
    """The current appearance, "light" or "dark" (CustomTkinter's global mode)."""
    import customtkinter as ctk

    return DARK if ctk.get_appearance_mode().lower() == DARK else LIGHT


def resolve(color: Color | str, mode: str | None = None) -> str:
    """The hex color of a token for `mode` (default: the current appearance); plain strings pass through."""
    if isinstance(color, str):
        return color
    mode = mode or appearance()
    return color[1] if mode == DARK else color[0]


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2 contrast ratio of two #RRGGBB colors (1..21)."""

    def luminance(hex_color: str) -> float:
        channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
