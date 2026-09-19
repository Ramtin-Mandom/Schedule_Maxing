"""
productivity_charts.py

Small, self-contained native tk.Canvas bar charts for the Productivity page
-- rectangles and text only, no plotting library. Deliberately independent
of app.py (no import from it) since app.py imports from app/ui/, not the
other way around; that would be a circular import.
"""

from __future__ import annotations

import tkinter as tk

from app.ui import theme


class PlannedVsActualChart(tk.Canvas):
    """One row per category: a planned-duration bar and an actual-duration bar, to the same scale."""

    def __init__(self, parent: tk.Widget, height: int = 160, **kwargs: object) -> None:
        super().__init__(parent, height=height, background=theme.CANVAS_BG, highlightthickness=0, bd=0, **kwargs)
        self._height = height

    def draw(self, rows: list[tuple[str, float | None, float | None]]) -> None:
        """`rows`: (category, median_planned_minutes_or_None, median_actual_minutes_or_None)."""
        self.delete("all")
        width = max(int(self.winfo_width()), 320)
        plottable = [(name, planned, actual) for name, planned, actual in rows if planned is not None and actual is not None]

        row_height = 32
        content_height = max(self._height, len(plottable) * row_height + 12)
        self.configure(scrollregion=(0, 0, width, content_height))

        if not plottable:
            self.create_text(width / 2, self._height / 2, text="Not enough completed tasks yet.", fill=theme.TEXT_MUTED)
            return

        max_value = max(max(planned, actual) for _, planned, actual in plottable) or 1.0
        left_margin = 96
        chart_width = max(width - left_margin - 70, 40)

        for index, (name, planned, actual) in enumerate(plottable):
            y0 = index * row_height + 6

            self.create_text(
                8, y0 + row_height / 2, text=name.capitalize(), anchor="w",
                fill=theme.TEXT_PRIMARY, font=("Segoe UI", 9, "bold"),
            )

            planned_width = (planned / max_value) * chart_width
            self.create_rectangle(left_margin, y0, left_margin + planned_width, y0 + 9, fill="#94A3B8", outline="")
            self.create_text(
                left_margin + planned_width + 6, y0 + 4, text=f"planned {planned:g}m", anchor="w",
                fill=theme.TEXT_MUTED, font=("Segoe UI", 7),
            )

            actual_width = (actual / max_value) * chart_width
            y1 = y0 + 13
            self.create_rectangle(left_margin, y1, left_margin + actual_width, y1 + 9, fill=theme.ACCENT, outline="")
            self.create_text(
                left_margin + actual_width + 6, y1 + 4, text=f"actual {actual:g}m", anchor="w",
                fill=theme.TEXT_MUTED, font=("Segoe UI", 7),
            )


class CompletionRateByBucketChart(tk.Canvas):
    """One vertical bar per time bucket, height = completion rate."""

    def __init__(self, parent: tk.Widget, height: int = 160, **kwargs: object) -> None:
        super().__init__(parent, height=height, background=theme.CANVAS_BG, highlightthickness=0, bd=0, **kwargs)
        self._height = height

    def draw(self, rows: list[tuple[str, float | None, int]]) -> None:
        """`rows`: (time_bucket, completion_rate_or_None, observation_count), in the display order wanted."""
        self.delete("all")
        width = max(int(self.winfo_width()), 320)
        self.configure(scrollregion=(0, 0, width, self._height))

        if not rows:
            self.create_text(width / 2, self._height / 2, text="Not enough data yet.", fill=theme.TEXT_MUTED)
            return

        bottom = self._height - 34
        top_margin = 22
        bar_slot_width = width / len(rows)

        for index, (bucket, rate, count) in enumerate(rows):
            x0 = index * bar_slot_width + 14
            x1 = (index + 1) * bar_slot_width - 14
            bar_height = (rate or 0.0) * (bottom - top_margin)
            y0 = bottom - bar_height
            color = theme.ACCENT if rate is not None else theme.GRID_LINE_STRONG

            self.create_rectangle(x0, y0, x1, bottom, fill=color, outline="")
            label = f"{rate:.0%}" if rate is not None else "n/a"
            self.create_text((x0 + x1) / 2, y0 - 8, text=label, fill=theme.TEXT_PRIMARY, font=("Segoe UI", 8, "bold"))
            self.create_text(
                (x0 + x1) / 2, bottom + 10, text=bucket.capitalize(), fill=theme.TEXT_MUTED, font=("Segoe UI", 8)
            )
            self.create_text((x0 + x1) / 2, bottom + 22, text=f"n={count}", fill=theme.TEXT_MUTED, font=("Segoe UI", 7))
