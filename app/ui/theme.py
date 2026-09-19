"""
theme.py

A small set of color constants mirroring app/app.py's existing palette, so
the new app/ui/ widgets look consistent with the rest of the desktop app.
Kept separate (not imported from app.py) because app.py imports *from*
app/ui/, not the other way around -- app/ui/ modules must not import
app.py, to avoid a circular import.
"""

from __future__ import annotations

APP_BG = "#EEF2F7"
CARD_BG = "#FFFFFF"
CARD_BORDER = "#E2E8F0"
TEXT_PRIMARY = "#0F172A"
TEXT_MUTED = "#64748B"
ACCENT = "#2563EB"
ACCENT_HOVER = "#1D4ED8"
DANGER = "#DC2626"
DANGER_HOVER = "#B91C1C"
SUCCESS = "#16A34A"
WARNING = "#F59E0B"
CANVAS_BG = "#F8FAFC"
GRID_LINE = "#E5E7EB"
GRID_LINE_STRONG = "#CBD5E1"
