"""
app/ui/budget_charts.py

Native tk.Canvas charts for Budgeting > Stats (rectangles, lines and text
only, like app/ui/productivity_charts.py). They draw values they are given --
rows of (label, minor units) or the forecast's daily balances -- and never
compute money; amounts are worded by app.budget.money.format_amount.
Colors follow the light/dark appearance on every draw; redraw() repaints.
"""

from __future__ import annotations

import tkinter as tk
from datetime import date

from app.budget.money import format_amount
from app.ui import theme

_c = theme.resolve
EXPENSE_BAR = ("#DC2626", "#F87171")
BALANCE_LINE = ("#2563EB", "#60A5FA")
ZERO_LINE = ("#B91C1C", "#F87171")


class BudgetBarChart(tk.Canvas):
    """One vertical bar per period (oldest first), labelled below; the largest value is written on its bar."""

    def __init__(self, parent: tk.Widget, height: int = 170, *, empty: str = "No data yet.", **kwargs) -> None:
        super().__init__(parent, height=height, background=_c(theme.CANVAS_BG), highlightthickness=0, bd=0, **kwargs)
        self._height = height
        self._empty = empty
        self.rows: list[tuple[str, int]] = []
        self.bind("<Configure>", lambda _event: self.redraw(), add="+")

    def redraw(self) -> None:
        self.draw(self.rows)

    def draw(self, rows: list[tuple[str, int]]) -> None:
        self.rows = rows
        self.delete("all")
        self.configure(background=_c(theme.CANVAS_BG))
        width = max(int(self.winfo_width()), 240)
        if not rows:
            self.create_text(width / 2, self._height / 2, text=self._empty, fill=_c(theme.TEXT_MUTED),
                             tags=("empty",))
            return
        top, bottom = 22, self._height - 30
        largest = max(amount for _, amount in rows) or 1
        slot = width / len(rows)
        label_every = max(1, int(len(rows) / max(1, width // 46)) + (1 if len(rows) * 46 > width else 0))
        for index, (label, amount) in enumerate(rows):
            x0, x1 = index * slot + max(1, slot * 0.15), (index + 1) * slot - max(1, slot * 0.15)
            y0 = bottom - (amount / largest) * (bottom - top)
            self.create_rectangle(x0, y0, x1, bottom, fill=_c(EXPENSE_BAR) if amount else _c(theme.GRID_LINE_STRONG),
                                  outline="", tags=("bar",))
            if amount == largest and amount:
                self.create_text((x0 + x1) / 2, y0 - 9, text=format_amount(amount), fill=_c(theme.TEXT_PRIMARY),
                                 font=("Segoe UI", 8, "bold"), tags=("peak",))
            if index % label_every == 0 or index == len(rows) - 1:
                self.create_text((x0 + x1) / 2, bottom + 12, text=label, fill=_c(theme.TEXT_MUTED),
                                 font=("Segoe UI", 7))
        self.create_line(0, bottom, width, bottom, fill=_c(theme.GRID_LINE_STRONG))


class ForecastChart(tk.Canvas):
    """The projected balance per forecast day as a line, the zero line, and the first day at or below zero."""

    def __init__(self, parent: tk.Widget, height: int = 200, **kwargs) -> None:
        super().__init__(parent, height=height, background=_c(theme.CANVAS_BG), highlightthickness=0, bd=0, **kwargs)
        self._height = height
        self.points: list[tuple[date, int]] = []
        self.zero_day: date | None = None
        self.bind("<Configure>", lambda _event: self.redraw(), add="+")

    def redraw(self) -> None:
        self.draw(self.points, self.zero_day)

    def draw(self, points: list[tuple[date, int]], zero_day: date | None) -> None:
        """`points`: (day, projected balance in minor units, rounded for display), in date order."""
        self.points, self.zero_day = points, zero_day
        self.delete("all")
        self.configure(background=_c(theme.CANVAS_BG))
        width = max(int(self.winfo_width()), 240)
        if not points:
            self.create_text(width / 2, self._height / 2, text="Enter your current balance to see a forecast.",
                             fill=_c(theme.TEXT_MUTED), tags=("empty",))
            return
        left, right, top, bottom = 64, width - 12, 14, self._height - 26
        values = [value for _, value in points]
        low, high = min(0, *values), max(0, *values)
        span = (high - low) or 1

        def y(value: int) -> float:
            return bottom - (value - low) / span * (bottom - top)

        def x(index: int) -> float:
            return left + index * (right - left) / max(1, len(points) - 1)

        for value, text in ((high, format_amount(high)), (low, format_amount(low))):
            self.create_text(left - 6, y(value), text=text, anchor="e", fill=_c(theme.TEXT_MUTED),
                             font=("Segoe UI", 7))
        self.create_line(left, y(0), right, y(0), fill=_c(ZERO_LINE), dash=(4, 3), tags=("zero",))
        coordinates = [coordinate for index, value in enumerate(values) for coordinate in (x(index), y(value))]
        if len(points) == 1:
            coordinates *= 2
        self.create_line(*coordinates, fill=_c(BALANCE_LINE), width=2, tags=("balance",))
        for index in (0, len(points) // 2, len(points) - 1):
            day = points[index][0]
            self.create_text(x(index), bottom + 12, text=f"{day:%b} {day.day}", fill=_c(theme.TEXT_MUTED),
                             font=("Segoe UI", 7))
        if zero_day is not None:
            index = next(i for i, (day, _) in enumerate(points) if day == zero_day)
            self.create_oval(x(index) - 4, y(values[index]) - 4, x(index) + 4, y(values[index]) + 4,
                             fill=_c(ZERO_LINE), outline="", tags=("zero_day",))
