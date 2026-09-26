"""
app/ui/calendar_model.py

Calendar arithmetic for the desktop Week and Month pages (Milestone 4,
Prompt 5). Tk-free and storage-free.

- Weeks start on **Monday**, matching the existing default_anchor and the
  allocator's week_dates.
- A month is a real calendar month (28, 29 in a leap February, 30 or 31
  days), never "30 days from an anchor". Its grid is whole Monday-first weeks
  (4 to 6 rows): the days before the 1st and after the last day belong to
  the neighbouring months and are marked as out-of-month.
- Moving by months clamps the day of the month (Jan 31 + 1 month = the last
  day of February) and rolls over into the next or previous year.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from app.planning.allocation import month_dates, week_dates

#: Monday (datetime.weekday() == 0) starts every week.
FIRST_WEEKDAY = 0
WEEKDAY_NAMES = tuple(calendar.day_abbr[(FIRST_WEEKDAY + offset) % 7] for offset in range(7))
MONTH_NAMES = tuple(calendar.month_name[month] for month in range(1, 13))


def week_start(day: date) -> date:
    return day - timedelta(days=(day.weekday() - FIRST_WEEKDAY) % 7)


def week_of(day: date) -> list[date]:
    """The seven real dates, Monday first, of the week containing `day`."""
    return week_dates(week_start(day))


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def month_grid(year: int, month: int) -> list[list[date]]:
    """Whole Monday-first weeks covering the month (4-6 rows of 7 real dates)."""
    return calendar.Calendar(firstweekday=FIRST_WEEKDAY).monthdatescalendar(year, month)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def move_months(day: date, delta: int) -> date:
    """`day` moved by whole months, the day of the month clamped to the target month's length."""
    year, month = shift_month(day.year, day.month, delta)
    return date(year, month, min(day.day, days_in_month(year, month)))


def month_title(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"


@dataclass(frozen=True)
class Period:
    """What one Week/Month page shows: an immutable snapshot, so a late load can be recognised."""

    mode: str  # "week" or "month"
    #: The dates the period consists of (a week, or every day of the month).
    start: date
    end: date
    #: The dates drawn (the month grid includes neighbouring months' days).
    grid_start: date
    grid_end: date
    selected: date

    @classmethod
    def for_date(cls, mode: str, selected: date) -> "Period":
        if mode == "week":
            days = week_of(selected)
            return cls(mode, days[0], days[-1], days[0], days[-1], selected)
        if mode != "month":
            raise ValueError(f"unknown calendar mode {mode!r}")
        days = month_dates(selected.year, selected.month)
        grid = month_grid(selected.year, selected.month)
        return cls(mode, days[0], days[-1], grid[0][0], grid[-1][-1], selected)

    @property
    def key(self) -> tuple[str, date, date]:
        """Identifies the dates shown (the selection may move inside it without a reload)."""
        return (self.mode, self.start, self.end)

    @property
    def dates(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range((self.end - self.start).days + 1)]

    @property
    def grid_dates(self) -> list[date]:
        return [self.grid_start + timedelta(days=offset) for offset in range((self.grid_end - self.grid_start).days + 1)]

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def shifted(self, delta: int) -> "Period":
        """The previous (-1) or next (+1) week/month, keeping the selected weekday / day of the month."""
        if self.mode == "week":
            return Period.for_date("week", self.selected + timedelta(days=7 * delta))
        return Period.for_date("month", move_months(self.selected, delta))

    @property
    def title(self) -> str:
        if self.mode == "month":
            return month_title(self.start.year, self.start.month)
        if self.start.year != self.end.year:
            return f"Week of {self.start:%b} {self.start.day}, {self.start.year} – {self.end:%b} {self.end.day}, {self.end.year}"
        if self.start.month != self.end.month:
            return f"Week of {self.start:%b} {self.start.day} – {self.end:%b} {self.end.day}, {self.end.year}"
        return f"Week of {self.start:%b} {self.start.day} – {self.end.day}, {self.end.year}"
