"""
app/ui/day_timeline.py

The Day Schedule's horizontal timeline (Milestone 4, Prompt 4): one bounded
canvas with hour markers, where every fixed block and saved placement is
drawn at its exact minutes (TimelineGeometry: one logical pixel per minute,
so a 13-minute task is 13 px wide, starting at its own minute). Fixed blocks
use their stored category's color; a schedule that is out of date is drawn
with a dashed warning outline and says so in words. Free gaps inside the
effective scheduling window are drawn as dashed outlines -- they come from
DaySnapshot.free_gaps and are display only. Items that overlap (possible
only for out-of-date work) go to their own lane.

Keyboard and pointer: the canvas takes Tab focus (with a visible ring);
Left/Right/Home/End select an item, Enter or a double-click edits it,
Delete removes it, and the Menu key, Shift+F10 or a right-click open its
actions. The selected item's full details (h:mm AM/PM times, duration,
kind, category) are written out below the canvas. The Day page's task list
offers the same actions.

Painting is coalesced and the scale is fixed, so moving or resizing the
window never repaints; wide days scroll horizontally (Shift + mouse wheel).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import ttk

import customtkinter as ctk

from app.ui import theme
from app.ui.components import Card, ContextMenu, MenuItem, SectionTitle, font
from app.ui.day_controller import DaySnapshot, FreeGap, TimelineItem
from app.ui.layout import Coalescer
from app.ui.time_fields import format_clock

MINUTES_PER_DAY = 1440


@dataclass(frozen=True)
class TimelineGeometry:
    """Where a minute of the day is drawn (logical pixels; CustomTkinter's scaling is applied by the widget)."""

    left: int = 16
    px_per_minute: float = 1.0
    ruler_height: int = 30
    lane_height: int = 58
    lane_gap: int = 6

    def x(self, minute: int) -> float:
        return self.left + minute * self.px_per_minute

    def lane_top(self, lane: int) -> float:
        return self.ruler_height + lane * (self.lane_height + self.lane_gap)

    def width(self) -> float:
        return self.x(MINUTES_PER_DAY) + self.left

    def height(self, lanes: int) -> float:
        return self.lane_top(max(1, lanes)) + 8


def hour_label(hour: int) -> str:
    """0 -> "12 AM", 13 -> "1 PM", 24 -> "12 AM"."""
    return format_clock((hour % 24) * 60).replace(":00", "")


class DayTimeline(Card):
    """See the module docstring. on_edit/on_remove receive the TimelineItem acted on."""

    def __init__(self, parent, *, on_edit: Callable[[TimelineItem], None], on_remove: Callable[[TimelineItem], None],
                 geometry: TimelineGeometry | None = None) -> None:
        super().__init__(parent)
        self.geometry = geometry or TimelineGeometry()
        self._on_edit, self._on_remove = on_edit, on_remove
        self.snapshot: DaySnapshot | None = None
        self.items: list[TimelineItem] = []
        self.selected: TimelineItem | None = None
        self.draw_count = 0
        self._scrolled_for = None
        self.columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 4))
        header.columnconfigure(0, weight=1)
        SectionTitle(header, "Schedule", "Exact times for the day. Select an item with the arrow keys or the "
                                         "mouse; Enter edits, Delete removes.", wraplength=620).grid(
            row=0, column=0, sticky="ew")
        self.legend = ctk.CTkFrame(header, fg_color="transparent")
        self.legend.grid(row=1, column=0, sticky="w", pady=(6, 0))
        for column, (text, color, border) in enumerate((
                ("Fixed block (its category color)", theme.category_style("fixed").fill, theme.GRID_LINE_STRONG),
                ("Scheduled task", theme.category_style("study").fill, theme.CARD_BG),
                ("Out of date (dashed)", theme.CARD_BG, theme.WARNING),
                ("Free time (dashed)", theme.CANVAS_BG, theme.SUCCESS))):
            item = ctk.CTkFrame(self.legend, fg_color="transparent")
            item.grid(row=0, column=column, sticky="w", padx=(0, 14), pady=1)
            ctk.CTkFrame(item, width=14, height=12, fg_color=color, border_color=border, border_width=2,
                         corner_radius=3).pack(side="left", padx=(0, 5))
            ctk.CTkLabel(item, text=text, font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED).pack(side="left")

        shell = ctk.CTkFrame(self, fg_color=theme.CANVAS_BG, corner_radius=12, border_color=theme.CARD_BORDER,
                             border_width=1)
        shell.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L, pady=(6, 4))
        shell.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(shell, background=theme.resolve(theme.CANVAS_BG), highlightthickness=2, bd=0,
                                relief="flat", width=200, height=int(self.geometry.height(1)), takefocus=1,
                                highlightbackground=theme.resolve(theme.CANVAS_BG),
                                highlightcolor=theme.resolve(theme.ACCENT))
        self.h_scroll = ttk.Scrollbar(shell, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self.h_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        self.h_scroll.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))

        self.details = ctk.CTkLabel(self, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_PRIMARY,
                                    anchor="w", justify="left", wraplength=900)
        self.details.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_L, pady=(2, theme.SPACE_L))

        canvas = self.canvas
        for sequence, handler in (("<Left>", lambda: self.step(-1)), ("<Right>", lambda: self.step(1)),
                                  ("<Home>", lambda: self.select_index(0)), ("<End>", lambda: self.select_index(-1)),
                                  ("<Return>", self.edit_selected), ("<KP_Enter>", self.edit_selected),
                                  ("<Delete>", self.remove_selected)):
            canvas.bind(sequence, lambda _e, h=handler: (h(), "break")[1], add="+")
        canvas.bind("<Button-1>", self._on_click, add="+")
        canvas.bind("<Double-Button-1>", lambda event: (self._on_click(event), self.edit_selected()), add="+")
        canvas.bind("<Shift-MouseWheel>", self._on_shift_wheel, add="+")
        canvas.bind("<FocusIn>", lambda _e: self._on_focus(), add="+")
        self.context_menu = ContextMenu(self)
        self.context_menu.attach(canvas, self._menu_items)
        self.redraws = Coalescer(self, self._paint)
        self._paint()

    # -- public ------------------------------------------------------------------------

    def draw(self, snapshot: DaySnapshot) -> None:
        """Show `snapshot` (painted once the current event burst is handled)."""
        self.snapshot = snapshot
        self.items = list(snapshot.timeline)
        if self.selected is not None:
            self.selected = next((item for item in self.items if item.key == self.selected.key), None)
        self.redraws.request()

    def request_redraw(self) -> None:
        self.redraws.request()

    def select(self, key: str | None) -> None:
        self.selected = next((item for item in self.items if item.key == key), None)
        self._show_details()
        self.redraws.request()

    def select_index(self, index: int) -> None:
        if self.items:
            self.select(self.items[index].key)

    def step(self, offset: int) -> None:
        if not self.items:
            return
        keys = [item.key for item in self.items]
        index = keys.index(self.selected.key) if self.selected is not None and self.selected.key in keys else -1
        self.select(keys[max(0, min(len(keys) - 1, index + offset)) if index >= 0 else 0])
        self._scroll_to(self.selected)

    def edit_selected(self) -> None:
        if self.selected is not None:
            self._on_edit(self.selected)

    def remove_selected(self) -> None:
        if self.selected is not None:
            self._on_remove(self.selected)

    def item_bounds(self, key: str) -> tuple[float, float, float, float] | None:
        """The drawn rectangle (x0, y0, x1, y1) of an item, in canvas coordinates (None if not drawn)."""
        found = self.canvas.find_withtag(f"box:{key}")
        if not found:
            return None
        return tuple(self.canvas.coords(found[0]))  # type: ignore[return-value]

    # -- events ------------------------------------------------------------------------

    def _on_focus(self) -> None:
        if self.selected is None and self.items:
            self.select_index(0)

    def _on_click(self, event) -> None:
        self.canvas.focus_set()
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        hit = next((item for item in self.items if self._contains(item, x, y)), None)
        self.select(hit.key if hit is not None else None)

    def _contains(self, item: TimelineItem, x: float, y: float) -> bool:
        bounds = self.item_bounds(item.key)
        return bounds is not None and bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]

    def _on_shift_wheel(self, event) -> str:
        self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _menu_items(self, event) -> list[MenuItem]:
        if event is not None:
            self._on_click(event)
        chosen = self.selected is not None
        return [MenuItem("Edit...", self.edit_selected, enabled=chosen),
                MenuItem("Remove...", self.remove_selected, enabled=chosen, danger=True)]

    def _show_details(self) -> None:
        if self.selected is not None:
            self.details.configure(text=f"Selected: {self.selected.description}.")
        elif self.snapshot is not None:
            self.details.configure(text=self._summary(self.snapshot))
        else:
            self.details.configure(text="")

    @staticmethod
    def _summary(snapshot: DaySnapshot) -> str:
        if snapshot.window_error:
            return f"The scheduling window cannot be used on this date: {snapshot.window_error}"
        parts = []
        if snapshot.window is not None:
            start, end = snapshot.window
            parts.append(f"Day window {format_clock(start)} – {format_clock(end)}")
        if snapshot.free_gaps:
            parts.append("free: " + "; ".join(gap.text.removeprefix("Free ") for gap in snapshot.free_gaps))
        if not snapshot.timeline:
            parts.append("nothing scheduled yet")
        return ". ".join(parts) + "."

    def _scroll_to(self, item: TimelineItem | None) -> None:
        if item is None:
            return
        self._scroll_to_minute(item.start_minute)

    def _scroll_to_minute(self, minute: int) -> None:
        total = self.geometry.width()
        self.canvas.xview_moveto(max(0.0, (self.geometry.x(minute) - 40) / total))

    # -- painting ----------------------------------------------------------------------

    def _paint(self) -> None:
        self.draw_count += 1
        geometry, canvas = self.geometry, self.canvas
        canvas.delete("all")
        canvas.configure(background=theme.resolve(theme.CANVAS_BG), highlightbackground=theme.resolve(theme.CANVAS_BG),
                         highlightcolor=theme.resolve(theme.ACCENT))
        snapshot = self.snapshot
        lanes = snapshot.lane_count if snapshot is not None else 1
        width, height = geometry.width(), geometry.height(lanes)
        canvas.configure(scrollregion=(0, 0, width, height), height=int(height))
        bottom = height - 8

        if snapshot is not None and snapshot.window is not None:
            start, end = snapshot.window
            shade = theme.resolve(theme.SUBTLE_BG)
            for x0, x1 in ((geometry.x(0), geometry.x(start)), (geometry.x(end), geometry.x(MINUTES_PER_DAY))):
                if x1 > x0:
                    canvas.create_rectangle(x0, geometry.ruler_height, x1, bottom, fill=shade, outline="",
                                            tags=("outside",))
                    if x1 - x0 > 150:
                        canvas.create_text((x0 + x1) / 2, (geometry.ruler_height + bottom) / 2,
                                           text="Outside the scheduling window", width=x1 - x0 - 10,
                                           font=(theme.FONT_FAMILY, 8), fill=theme.resolve(theme.TEXT_MUTED))

        for hour in range(25):
            x = geometry.x(hour * 60)
            strong = hour % 6 == 0
            canvas.create_line(x, geometry.ruler_height - (8 if strong else 4), x, bottom,
                               fill=theme.resolve(theme.GRID_LINE_STRONG if strong else theme.GRID_LINE))
            if hour < 24:
                canvas.create_text(x + 3, 12, text=hour_label(hour), anchor="w", font=(theme.FONT_FAMILY, 8),
                                   fill=theme.resolve(theme.TEXT_MUTED))
                half = geometry.x(hour * 60 + 30)
                canvas.create_line(half, geometry.ruler_height - 3, half, geometry.ruler_height,
                                   fill=theme.resolve(theme.GRID_LINE))

        if snapshot is not None:
            for gap in snapshot.free_gaps:
                self._draw_gap(gap)
            for item in snapshot.timeline:
                self._draw_item(item)
            if self._scrolled_for != snapshot.day:  # a newly shown date starts at its window / first item
                self._scrolled_for = snapshot.day
                first = min([item.start_minute for item in snapshot.timeline]
                            + ([snapshot.window[0]] if snapshot.window else [480]))
                canvas.after_idle(lambda minute=first: self._scroll_to_minute(minute))
        self._show_details()

    def _draw_gap(self, gap: FreeGap) -> None:
        geometry = self.geometry
        x0, x1 = geometry.x(gap.start_minute), geometry.x(gap.end_minute)
        y0 = geometry.lane_top(0) + 4
        y1 = y0 + geometry.lane_height - 8
        self.canvas.create_rectangle(x0 + 1, y0, x1 - 1, y1, outline=theme.resolve(theme.SUCCESS), dash=(3, 3),
                                     fill="", tags=("gap",))
        if x1 - x0 >= 44:
            self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="Free", width=x1 - x0 - 6,
                                    font=(theme.FONT_FAMILY, 8), fill=theme.resolve(theme.TEXT_MUTED))

    def _draw_item(self, item: TimelineItem) -> None:
        geometry, canvas = self.geometry, self.canvas
        x0, x1 = geometry.x(item.start_minute), geometry.x(item.end_minute)
        y0 = geometry.lane_top(item.lane) + 2
        y1 = y0 + geometry.lane_height - 4
        style = theme.category_style(item.category)
        fill, text_fill = theme.resolve(style.fill), theme.resolve(style.text)
        outline, dash, width = theme.resolve(theme.CARD_BG), None, 1
        if item.kind == "fixed":
            outline, width = theme.resolve(theme.GRID_LINE_STRONG), 2
        elif item.kind == "stale":
            outline, dash, width = theme.resolve(theme.WARNING), (5, 3), 2
        if self.selected is not None and self.selected.key == item.key:
            outline, width, dash = theme.resolve(theme.ACCENT), 3, None
        canvas.create_rectangle(x0, y0, max(x1, x0 + 1), y1, fill=fill, outline=outline, width=width, dash=dash,
                                tags=("item", f"box:{item.key}"))
        box_width = x1 - x0
        if box_width >= 28:
            caption = {"fixed": "Fixed", "stale": "Out of date", "scheduled": ""}[item.kind]
            canvas.create_text(x0 + 5, y0 + 5, text=item.name, anchor="nw", width=box_width - 8,
                               font=(theme.FONT_FAMILY, 9, "bold"), fill=text_fill, tags=("item",))
            if box_width >= 70:
                detail = item.time_text + (f"  · {caption}" if caption else "")
                canvas.create_text(x0 + 5, y1 - 5, text=detail, anchor="sw", width=box_width - 8,
                                   font=(theme.FONT_FAMILY, 7), fill=text_fill, tags=("item",))
