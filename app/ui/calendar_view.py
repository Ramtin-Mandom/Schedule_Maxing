"""
app/ui/calendar_view.py

The drawing of the Week and Month pages (Milestone 4, Prompt 5), painted from
a CalendarSnapshot.

Week: seven columns. A fixed header canvas holds the weekday/date headers
and a band listing each day's tasks that are not scheduled (in input order,
"not scheduled" -- no invented times; "+N more" when crowded). Below it, a
time canvas with an hour axis scrolls vertically; fixed blocks and saved
placements are drawn there at their actual times. Both share horizontal
scrolling, so the headers always match their columns.

Month: whole Monday-first weeks. Each cell shows its date number, then its
timed items in time order ("9:00a Lecture") and its unscheduled tasks, with
"+N more" when the cell is full. The page lists the selected day's exact,
ordered details below the grid. Days of the neighbouring months are drawn
on a different background with the month's name.

Everywhere:

- fixed blocks and tasks use their category's color;
- past days are muted (a quieter background, and muted text that still
  passes the contrast checks) but stay fully visible;
- today's date is marked, and the selected day has an accent outline;
- status is also said in words.

Pointer and keyboard: a click selects a day, and a double-click opens it (a
shortcut; the page has an explicit Open Day button). The time/month canvas
takes Tab focus:

- Left/Right move a day;
- Up/Down move a week (Month) or scroll (Week);
- Home/End jump to the first/last day;
- Enter opens the selected day.

Painting is coalesced. A width change repaints once after the resize burst
(never per pixel), and nothing here reads the database.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from datetime import date, timedelta
from tkinter import ttk

import customtkinter as ctk

from app.ui import theme
from app.ui.calendar_controller import CalendarDay, CalendarItem, CalendarSnapshot
from app.ui.calendar_model import WEEKDAY_NAMES
from app.ui.components import Card
from app.ui.day_timeline import hour_label
from app.ui.layout import Coalescer
from app.ui.time_fields import format_clock

AXIS_WIDTH = 58
WEEK_HEADER = 40
SUMMARY_LINES = 3
LINE = 16
SUMMARY_HEIGHT = SUMMARY_LINES * LINE + LINE + 8
PX_PER_HOUR = 36
MIN_COLUMN = 96
MONTH_HEADER = 26
CELL_HEIGHT = 112
CELL_LINES = 4
WEEK_VIEW_HEIGHT = 440
TOP = 6


def item_tag(day: date, name: str) -> str:
    """A canvas tag for one item (whitespace and tag-expression characters replaced)."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in name)
    return f"item:{day.isoformat()}:{safe}"


def compact_time(minute: int) -> str:
    """600 -> "10:00a", 780 -> "1:00p" (for crowded cells; details use the full h:mm AM/PM)."""
    text = format_clock(minute % 1440)
    return text.replace(" AM", "a").replace(" PM", "p")


class CalendarView(Card):
    def __init__(self, parent, *, mode: str, on_select: Callable[[date], None], on_open: Callable[[date], None]) -> None:
        super().__init__(parent)
        self.mode = mode
        self._on_select, self._on_open = on_select, on_open
        self.snapshot: CalendarSnapshot | None = None
        self.selected: date | None = None
        self.draw_count = 0
        self._width = 0
        self._scrolled_for = None
        self.columnconfigure(0, weight=1)
        shell = ctk.CTkFrame(self, fg_color=theme.CANVAS_BG, corner_radius=12, border_color=theme.CARD_BORDER,
                             border_width=1)
        shell.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=theme.SPACE_L)
        shell.columnconfigure(0, weight=1)
        row = 0
        self.header_canvas: tk.Canvas | None = None
        if mode == "week":
            self.header_canvas = tk.Canvas(shell, background=theme.resolve(theme.CANVAS_BG), highlightthickness=0,
                                           bd=0, width=300, height=WEEK_HEADER + SUMMARY_HEIGHT)
            self.header_canvas.grid(row=0, column=0, sticky="ew", padx=(8, 2), pady=(6, 0))
            row = 1
        height = WEEK_VIEW_HEIGHT if mode == "week" else MONTH_HEADER + 5 * CELL_HEIGHT
        self.canvas = tk.Canvas(shell, background=theme.resolve(theme.CANVAS_BG), highlightthickness=2, bd=0,
                                width=300, height=height, takefocus=1,
                                highlightbackground=theme.resolve(theme.CANVAS_BG),
                                highlightcolor=theme.resolve(theme.ACCENT))
        self.canvas.grid(row=row, column=0, sticky="ew", padx=(6, 0), pady=(0 if row else 6, 0))
        self.h_scroll = ttk.Scrollbar(shell, orient="horizontal", command=self._xview)
        self.h_scroll.grid(row=row + 1, column=0, sticky="ew", padx=6, pady=(0, 6))
        self.canvas.configure(xscrollcommand=self.h_scroll.set)
        canvas = self.canvas
        if mode == "week":
            self.v_scroll = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
            self.v_scroll.grid(row=row, column=1, sticky="ns")
            canvas.configure(yscrollcommand=self.v_scroll.set)
            self.header_canvas.bind("<Button-1>", self._on_click, add="+")
            self.header_canvas.bind("<Double-Button-1>", self._on_double, add="+")
        self.redraws = Coalescer(self, self._paint)
        self.resizes = Coalescer(self, self._check_width, delay_ms=80)
        canvas.bind("<Configure>", lambda _e: self.resizes.request(), add="+")
        canvas.bind("<Button-1>", self._on_click, add="+")
        canvas.bind("<Double-Button-1>", self._on_double, add="+")
        bindings = [("<Left>", lambda: self.move(-1)), ("<Right>", lambda: self.move(1)),
                    ("<Home>", lambda: self._jump(0)), ("<End>", lambda: self._jump(-1)),
                    ("<Return>", self.open_selected), ("<KP_Enter>", self.open_selected)]
        if mode == "month":
            bindings += [("<Up>", lambda: self.move(-7)), ("<Down>", lambda: self.move(7))]
        else:
            bindings += [("<Up>", lambda: canvas.yview_scroll(-2, "units")),
                         ("<Down>", lambda: canvas.yview_scroll(2, "units"))]
            canvas.bind("<MouseWheel>", lambda e: (canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"), "break")[1],
                        add="+")
        for sequence, handler in bindings:
            canvas.bind(sequence, lambda _e, h=handler: (h(), "break")[1], add="+")
        self._paint()

    # -- public ------------------------------------------------------------------------

    def draw(self, snapshot: CalendarSnapshot, selected: date) -> None:
        self.snapshot, self.selected = snapshot, selected
        self.redraws.request()

    def set_selected(self, selected: date) -> None:
        self.selected = selected
        self.redraws.request()

    def request_redraw(self) -> None:
        self.redraws.request()

    def move(self, days: int) -> None:
        if self.selected is not None:
            self._on_select(self.selected + timedelta(days=days))

    def open_selected(self) -> None:
        if self.selected is not None:
            self._on_open(self.selected)

    def _canvases(self) -> list[tk.Canvas]:
        return [self.canvas] + ([self.header_canvas] if self.header_canvas is not None else [])

    def cell_bounds(self, day: date) -> tuple[float, float, float, float] | None:
        found = self.canvas.find_withtag(f"cell:{day.isoformat()}")
        return tuple(self.canvas.coords(found[0])) if found else None  # type: ignore[return-value]

    def item_bounds(self, day: date, name: str) -> tuple[float, float, float, float] | None:
        for canvas in self._canvases():
            found = canvas.find_withtag(item_tag(day, name))
            if found:
                return tuple(canvas.coords(found[0]))  # type: ignore[return-value]
        return None

    # -- events ------------------------------------------------------------------------

    def _xview(self, *args) -> None:
        for canvas in self._canvases():
            canvas.xview(*args)

    def _jump(self, index: int) -> None:
        if self.snapshot is not None and self.snapshot.period is not None:
            self._on_select(self.snapshot.period.dates[index])

    @staticmethod
    def _day_at(event) -> date | None:
        canvas = event.widget
        x, y = canvas.canvasx(event.x), canvas.canvasy(event.y)
        for item in canvas.find_overlapping(x, y, x, y):
            for tag in canvas.gettags(item):
                if tag.startswith("cell:"):
                    return date.fromisoformat(tag[5:])
        return None

    def _on_click(self, event) -> None:
        self.canvas.focus_set()
        day = self._day_at(event)
        if day is not None:
            self._on_select(day)

    def _on_double(self, event) -> None:
        day = self._day_at(event)
        if day is not None:
            self._on_open(day)

    def _check_width(self) -> None:
        width = self.canvas.winfo_width()
        if width > 1 and width != self._width:
            self._width = width
            self.redraws.request()

    # -- painting ----------------------------------------------------------------------

    def _column_width(self, reserved: float) -> float:
        available = max(self._width or self.canvas.winfo_width(), 300) - reserved - 6
        return max(MIN_COLUMN, available / 7)

    def _paint(self) -> None:
        self.draw_count += 1
        for canvas in self._canvases():
            canvas.delete("all")
            canvas.configure(background=theme.resolve(theme.CANVAS_BG))
        self.canvas.configure(highlightbackground=theme.resolve(theme.CANVAS_BG),
                              highlightcolor=theme.resolve(theme.ACCENT))
        if self.snapshot is None:
            return
        if self.mode == "week":
            self._paint_week(self.snapshot)
        else:
            self._paint_month(self.snapshot)

    def _day_colors(self, cell: CalendarDay) -> tuple[str, str]:
        """(background, text) of a day: out-of-month and past days are quieter but readable."""
        if not cell.in_period:
            return theme.resolve(theme.SUBTLE_BG), theme.resolve(theme.TEXT_MUTED)
        if cell.is_past:
            return theme.resolve(theme.CANVAS_BG), theme.resolve(theme.TEXT_MUTED)
        return theme.resolve(theme.CARD_BG), theme.resolve(theme.TEXT_PRIMARY)

    def _outline(self, cell: CalendarDay) -> tuple[str, int]:
        if cell.date == self.selected:
            return theme.resolve(theme.ACCENT), 3
        return theme.resolve(theme.GRID_LINE_STRONG), 1

    def _item_line(self, canvas: tk.Canvas, x: float, y: float, width: float, cell: CalendarDay,
                   item: CalendarItem) -> None:
        style = theme.category_style(item.category)
        if item.timed:
            text = f"{compact_time(item.start_minute)} {item.name}"
        elif item.kind == "elsewhere":
            text = f"{item.name} (on {item.elsewhere[0]:%b} {item.elsewhere[0].day})"
        else:
            text = f"{item.name} · not scheduled"
        limit = max(4, int((width - 6) / 5.6))  # one line per item; the page's details list has the full text
        if len(text) > limit:
            text = text[:limit - 1].rstrip() + "…"
        fill = theme.resolve(style.fill) if item.timed else ""
        outline = "" if item.timed else theme.resolve(style.fill)
        dash = None if item.timed else (3, 2)
        if item.kind == "stale":
            outline, dash = theme.resolve(theme.WARNING), (4, 2)
        tag = f"cell:{cell.date.isoformat()}"
        canvas.create_rectangle(x, y, x + width, y + LINE - 1, fill=fill, outline=outline, dash=dash,
                                tags=(tag, item_tag(cell.date, item.name)))
        color = theme.resolve(style.text) if item.timed else theme.resolve(
            theme.TEXT_MUTED if cell.is_past or not cell.in_period else theme.TEXT_PRIMARY)
        canvas.create_text(x + 3, y + LINE / 2, text=text, anchor="w", width=width - 6, font=(theme.FONT_FAMILY, 8),
                           fill=color, tags=(tag,))

    def _paint_week(self, snapshot: CalendarSnapshot) -> None:
        canvas, header = self.canvas, self.header_canvas
        column = self._column_width(AXIS_WIDTH)
        height = TOP + 24 * PX_PER_HOUR + 6
        width = AXIS_WIDTH + 7 * column + 6
        canvas.configure(scrollregion=(0, 0, width, height))
        header.configure(scrollregion=(0, 0, width, WEEK_HEADER + SUMMARY_HEIGHT))
        muted, text = theme.resolve(theme.TEXT_MUTED), theme.resolve(theme.TEXT_PRIMARY)
        header.create_text(AXIS_WIDTH - 8, WEEK_HEADER + 2, text="Not\nscheduled", anchor="ne", justify="right",
                           font=(theme.FONT_FAMILY, 7), fill=muted)
        for index, cell in enumerate(snapshot.days):
            x0 = AXIS_WIDTH + index * column
            x1 = x0 + column - 4
            background, header_color = self._day_colors(cell)
            outline, line_width = self._outline(cell)
            tag = f"cell:{cell.date.isoformat()}"
            header.create_rectangle(x0, 2, x1, WEEK_HEADER + SUMMARY_HEIGHT - 2, fill=background, outline=outline,
                                    width=line_width, tags=(tag,))
            canvas.create_rectangle(x0, 2, x1, height - 2, fill=background, outline=outline, width=line_width,
                                    tags=(tag,))
            label = f"{WEEKDAY_NAMES[index]} {cell.date:%b} {cell.date.day}"
            label += " · today" if cell.is_today else " · past" if cell.is_past else ""
            header.create_text((x0 + x1) / 2, 14, text=label, font=(theme.FONT_FAMILY, 9, "bold"),
                               fill=theme.resolve(theme.ACCENT) if cell.is_today else header_color, tags=(tag,))
            if cell.freshness_label:
                header.create_text((x0 + x1) / 2, 30, text=cell.freshness_label, font=(theme.FONT_FAMILY, 7),
                                   fill=muted if cell.freshness_label == "Current" else theme.resolve(theme.WARNING),
                                   tags=(tag,))
            untimed = cell.untimed
            for line, item in enumerate(untimed[:SUMMARY_LINES]):
                self._item_line(header, x0 + 3, WEEK_HEADER + line * LINE, column - 10, cell, item)
            if len(untimed) > SUMMARY_LINES:
                header.create_text(x0 + 5, WEEK_HEADER + SUMMARY_LINES * LINE + LINE / 2,
                                   text=f"+{len(untimed) - SUMMARY_LINES} more", anchor="w",
                                   font=(theme.FONT_FAMILY, 8, "bold"), fill=text, tags=(tag,))
        for hour in range(25):
            y = TOP + hour * PX_PER_HOUR
            canvas.create_line(AXIS_WIDTH - 4, y, width, y,
                               fill=theme.resolve(theme.GRID_LINE_STRONG if hour % 6 == 0 else theme.GRID_LINE))
            if hour < 24:
                canvas.create_text(AXIS_WIDTH - 8, y + 2, text=hour_label(hour), anchor="ne",
                                   font=(theme.FONT_FAMILY, 8), fill=muted)
        for index, cell in enumerate(snapshot.days):
            x0 = AXIS_WIDTH + index * column
            x1 = x0 + column - 4
            tag = f"cell:{cell.date.isoformat()}"
            for item in cell.timed:
                style = theme.category_style(item.category)
                y0 = TOP + item.start_minute * PX_PER_HOUR / 60
                y1 = max(y0 + 3, TOP + item.end_minute * PX_PER_HOUR / 60)
                outline_color, dash = theme.resolve(theme.CARD_BG), None
                if item.kind == "fixed":
                    outline_color = theme.resolve(theme.GRID_LINE_STRONG)
                elif item.kind == "stale":
                    outline_color, dash = theme.resolve(theme.WARNING), (4, 2)
                canvas.create_rectangle(x0 + 4, y0, x1 - 4, y1, fill=theme.resolve(style.fill), outline=outline_color,
                                        dash=dash, width=2 if item.kind != "scheduled" else 1,
                                        tags=(tag, item_tag(cell.date, item.name)))
                if y1 - y0 >= 12:
                    canvas.create_text(x0 + 7, y0 + 2, text=f"{compact_time(item.start_minute)} {item.name}",
                                       anchor="nw", width=column - 16, font=(theme.FONT_FAMILY, 8),
                                       fill=theme.resolve(style.text), tags=(tag,))
        if self._scrolled_for != snapshot.period.key:  # a new week starts at 7 AM or its first item
            self._scrolled_for = snapshot.period.key
            starts = [item.start_minute for cell in snapshot.days for item in cell.timed]
            first = max(0, min(starts + [7 * 60]) - 30)
            canvas.after_idle(lambda: canvas.yview_moveto(first * PX_PER_HOUR / 60 / height))

    def _paint_month(self, snapshot: CalendarSnapshot) -> None:
        canvas = self.canvas
        column = self._column_width(0)
        weeks = len(snapshot.days) // 7
        height = MONTH_HEADER + weeks * CELL_HEIGHT + 4
        width = 7 * column + 6
        canvas.configure(scrollregion=(0, 0, width, height), height=height)
        muted = theme.resolve(theme.TEXT_MUTED)
        for index, name in enumerate(WEEKDAY_NAMES):
            canvas.create_text(index * column + column / 2, MONTH_HEADER / 2, text=name,
                               font=(theme.FONT_FAMILY, 9, "bold"), fill=muted)
        for index, cell in enumerate(snapshot.days):
            row, col = divmod(index, 7)
            x0, y0 = col * column + 2, MONTH_HEADER + row * CELL_HEIGHT
            x1, y1 = x0 + column - 4, y0 + CELL_HEIGHT - 4
            background, text_color = self._day_colors(cell)
            outline, line_width = self._outline(cell)
            tag = f"cell:{cell.date.isoformat()}"
            canvas.create_rectangle(x0, y0, x1, y1, fill=background, outline=outline, width=line_width, tags=(tag,))
            number = str(cell.date.day) if cell.in_period else f"{cell.date:%b} {cell.date.day}"
            if cell.is_today:
                canvas.create_oval(x0 + 3, y0 + 3, x0 + 25, y0 + 21, fill=theme.resolve(theme.ACCENT), outline="",
                                   tags=(tag,))
            canvas.create_text(x0 + 14 if cell.is_today else x0 + 6, y0 + 12, text=number,
                               anchor="center" if cell.is_today else "w", font=(theme.FONT_FAMILY, 9, "bold"),
                               fill=theme.resolve(theme.TEXT_ON_ACCENT) if cell.is_today else text_color, tags=(tag,))
            if cell.freshness_label:
                canvas.create_text(x1 - 4, y0 + 12, text=cell.freshness_label, anchor="e", font=(theme.FONT_FAMILY, 7),
                                   fill=muted if cell.freshness_label == "Current" else theme.resolve(theme.WARNING),
                                   tags=(tag,))
            items = cell.items
            shown = items if len(items) <= CELL_LINES else items[:CELL_LINES - 1]
            for line, item in enumerate(shown):
                self._item_line(canvas, x0 + 3, y0 + 24 + line * LINE, column - 10, cell, item)
            if len(items) > len(shown):
                canvas.create_text(x0 + 6, y0 + 24 + len(shown) * LINE + LINE / 2, text=f"+{len(items) - len(shown)} more",
                                   anchor="w", font=(theme.FONT_FAMILY, 8, "bold"), fill=text_color, tags=(tag,))
