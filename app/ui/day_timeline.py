"""
app/ui/day_timeline.py

The Day Schedule's timeline: the whole day, 12 AM to 12 AM, fitted to the
width it is given -- there is no horizontal scrolling. It is the upper part
of the Day page's workspace (the To Do notes sit directly below it, in the
same card), so it draws no frame, title or legend of its own.

Every fixed block and saved placement is drawn at its exact minutes
(TimelineGeometry: `px_per_minute` is the fitted scale, so a 13-minute task
is exactly 13 minutes wide on the ruler) as a soft rounded block in its
category's muted color. A fixed block has a stronger outline; a schedule
that is out of date a dashed warning outline. Hours outside the effective
scheduling window are shaded, and free gaps inside it are faint dashed
outlines (from DaySnapshot.free_gaps; display only). Items that overlap
(possible only for out-of-date work) go to their own lane.

Labels: a block wide enough shows its name and times across; a narrower one
shows its name vertically (read bottom to top), which is what keeps short
tasks readable on a full-day scale; one too narrow even for that is named in
the details line when it is selected. Names never spill out of their block.

Keyboard and pointer: the canvas takes Tab focus (with a visible ring);
Left/Right/Home/End select an item, Enter or a double-click edits it,
Delete removes it, and the Menu key, Shift+F10 or a right-click open its
actions (a task the user placed by moving it also offers "Release manual
placement": Make Schedule keeps it until it is released). The selected
item's full details are written out below the canvas.

Painting is coalesced. The scale follows the canvas width, so a change of
width repaints once after the resize settles (a short delay; nothing is
computed in the resize handler itself); moving the window, or a change of
height only, never repaints.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, replace

import customtkinter as ctk

from app.ui import theme
from app.ui.components import ContextMenu, MenuItem, font
from app.ui.day_controller import DaySnapshot, FreeGap, TimelineItem
from app.ui.layout import Coalescer
from app.ui.time_fields import format_clock

MINUTES_PER_DAY = 1440
#: A block at least this wide (px) is labelled across, with its times below the name.
HORIZONTAL_LABEL_WIDTH = 78
#: A narrower block at least this wide is labelled vertically; below it the block is too thin for text.
VERTICAL_LABEL_WIDTH = 13
#: The corner radius of a block (never more than half its width).
BLOCK_RADIUS = 7
#: A width change smaller than this (px) is not worth a repaint.
RESIZE_TOLERANCE = 2


@dataclass(frozen=True)
class TimelineGeometry:
    """Where a minute of the day is drawn (pixels; `px_per_minute` is fitted to the canvas by the widget)."""

    left: int = 10
    px_per_minute: float = 1.0
    ruler_height: int = 24
    lane_height: int = 104
    lane_gap: int = 6

    def x(self, minute: int) -> float:
        return self.left + minute * self.px_per_minute

    def lane_top(self, lane: int) -> float:
        return self.ruler_height + lane * (self.lane_height + self.lane_gap)

    def width(self) -> float:
        return self.x(MINUTES_PER_DAY) + self.left

    def height(self, lanes: int) -> float:
        return self.lane_top(max(1, lanes)) + 6

    def fitted(self, canvas_width: float) -> "TimelineGeometry":
        """This geometry scaled so the whole day is exactly `canvas_width` wide."""
        return replace(self, px_per_minute=max(0.1, (canvas_width - 2 * self.left) / MINUTES_PER_DAY))


def hour_label(hour: int) -> str:
    """0 -> "12 AM", 13 -> "1 PM", 24 -> "12 AM"."""
    return format_clock((hour % 24) * 60).replace(":00", "")


def hour_step(px_per_hour: float) -> int:
    """How many hours apart the ruler's labels are, so they never run into each other."""
    return next((step for step in (1, 2, 3, 6) if step * px_per_hour >= 40), 12)


def rounded_points(x0: float, y0: float, x1: float, y1: float, radius: float) -> list[float]:
    """The points of a smooth polygon that draws the rectangle with rounded corners."""
    r = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    # Each straight edge's ends are given twice, which keeps the edges straight and rounds only the corners.
    edge_ends = [(x0 + r, y0), (x1 - r, y0), (x1, y0 + r), (x1, y1 - r), (x1 - r, y1), (x0 + r, y1), (x0, y1 - r),
                 (x0, y0 + r)]
    corners = [(x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    points: list[float] = []
    for index in range(4):
        start, end = edge_ends[2 * index], edge_ends[2 * index + 1]
        points += [*start, *start, *end, *end, *corners[index]]
    return points


def fit_text(text: str, room_px: float, font_px: float = 6.6) -> str:
    """`text` cut (with an ellipsis) to what `room_px` of a ~9 pt label can show; "" when not even two letters fit."""
    letters = int(room_px / font_px)
    if letters >= len(text):
        return text
    return text[: letters - 1].rstrip() + "…" if letters >= 3 else ""


class DayTimeline(ctk.CTkFrame):
    """See the module docstring. on_edit/on_remove/on_release receive the TimelineItem acted on."""

    def __init__(self, parent, *, on_edit: Callable[[TimelineItem], None], on_remove: Callable[[TimelineItem], None],
                 on_release: Callable[[TimelineItem], None] | None = None,
                 geometry: TimelineGeometry | None = None) -> None:
        super().__init__(parent, fg_color="transparent")
        self._base_geometry = geometry or TimelineGeometry()
        #: The geometry of what is drawn now: the base one, scaled to the canvas width.
        self.geometry = self._base_geometry
        self._on_edit, self._on_remove, self._on_release = on_edit, on_remove, on_release
        self.snapshot: DaySnapshot | None = None
        self.items: list[TimelineItem] = []
        self.selected: TimelineItem | None = None
        self.draw_count = 0
        self._bounds: dict[str, tuple[float, float, float, float]] = {}
        self._painted_width = 0
        self.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, background=theme.resolve(theme.CARD_BG), highlightthickness=2, bd=0,
                                relief="flat", width=200, height=int(self.geometry.height(1)), takefocus=1,
                                highlightbackground=theme.resolve(theme.CARD_BG),
                                highlightcolor=theme.resolve(theme.ACCENT))
        self.canvas.grid(row=0, column=0, sticky="ew")
        self.details = ctk.CTkLabel(self, text="", font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED,
                                    anchor="w", justify="left", wraplength=900)
        self.details.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_S, pady=(2, 0))

        canvas = self.canvas
        for sequence, handler in (("<Left>", lambda: self.step(-1)), ("<Right>", lambda: self.step(1)),
                                  ("<Home>", lambda: self.select_index(0)), ("<End>", lambda: self.select_index(-1)),
                                  ("<Return>", self.edit_selected), ("<KP_Enter>", self.edit_selected),
                                  ("<Delete>", self.remove_selected)):
            canvas.bind(sequence, lambda _e, h=handler: (h(), "break")[1], add="+")
        canvas.bind("<Button-1>", self._on_click, add="+")
        canvas.bind("<Double-Button-1>", lambda event: (self._on_click(event), self.edit_selected()), add="+")
        canvas.bind("<FocusIn>", lambda _e: self._on_focus(), add="+")
        self.context_menu = ContextMenu(self)
        self.context_menu.attach(canvas, self._menu_items)
        self.redraws = Coalescer(self, self._paint)
        # A new width means a new scale: one repaint, after the resize has settled.
        self._resizes = Coalescer(self, self._paint, delay_ms=60)
        canvas.bind("<Configure>", self._on_configure, add="+")
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

    def edit_selected(self) -> None:
        if self.selected is not None:
            self._on_edit(self.selected)

    def remove_selected(self) -> None:
        if self.selected is not None:
            self._on_remove(self.selected)

    def release_selected(self) -> None:
        if self.selected is not None and self.selected.preserved and self._on_release is not None:
            self._on_release(self.selected)

    def item_bounds(self, key: str) -> tuple[float, float, float, float] | None:
        """The drawn rectangle (x0, y0, x1, y1) of an item, in canvas coordinates (None if not drawn)."""
        return self._bounds.get(key)

    # -- events ------------------------------------------------------------------------

    def _on_configure(self, event) -> None:
        # Only a real change of width changes the scale; nothing else is done here.
        if abs(event.width - self._painted_width) >= RESIZE_TOLERANCE:
            self._resizes.request()

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
        # A very thin block is still easy to hit: a few pixels of slack on each side.
        return bounds is not None and bounds[0] - 3 <= x <= bounds[2] + 3 and bounds[1] <= y <= bounds[3]

    def _menu_items(self, event) -> list[MenuItem]:
        if event is not None:
            self._on_click(event)
        chosen = self.selected is not None
        items = [MenuItem("Edit...", self.edit_selected, enabled=chosen)]
        if self._on_release is not None:
            items.append(MenuItem("Release manual placement", self.release_selected,
                                  enabled=chosen and self.selected.preserved))
        return [*items, MenuItem("Remove...", self.remove_selected, enabled=chosen, danger=True)]

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
        if not snapshot.timeline:
            parts.append("nothing scheduled yet")
        return ". ".join(parts) + "."

    # -- painting ----------------------------------------------------------------------

    def _paint(self) -> None:
        self.draw_count += 1
        canvas = self.canvas
        self._painted_width = canvas.winfo_width()
        geometry = self.geometry = self._base_geometry.fitted(max(self._painted_width, 200))
        canvas.delete("all")
        self._bounds = {}
        background = theme.resolve(theme.CARD_BG)
        canvas.configure(background=background, highlightbackground=background,
                         highlightcolor=theme.resolve(theme.ACCENT))
        snapshot = self.snapshot
        lanes = snapshot.lane_count if snapshot is not None else 1
        height = geometry.height(lanes)
        if int(float(canvas.cget("height"))) != int(height):
            canvas.configure(height=int(height))
        self.details.configure(wraplength=max(200, self._painted_width - 24))
        top, bottom = geometry.ruler_height, height - 6

        # The day's track, with the hours outside the scheduling window shaded.
        canvas.create_polygon(rounded_points(geometry.x(0), top, geometry.x(MINUTES_PER_DAY), bottom, 10), smooth=True,
                              fill=theme.resolve(theme.CANVAS_BG), outline=theme.resolve(theme.GRID_LINE),
                              tags=("track",))
        if snapshot is not None and snapshot.window is not None:
            start, end = snapshot.window
            for x0, x1 in ((geometry.x(0), geometry.x(start)), (geometry.x(end), geometry.x(MINUTES_PER_DAY))):
                if x1 - x0 > 1:
                    canvas.create_polygon(rounded_points(x0, top, x1, bottom, 10), smooth=True, outline="",
                                          fill=theme.resolve(theme.GRID_LINE), stipple="gray50", tags=("outside",))

        step = hour_step(geometry.px_per_minute * 60)
        for hour in range(25):
            x = geometry.x(hour * 60)
            if 0 < hour < 24:
                canvas.create_line(x, top + 1, x, bottom - 1, fill=theme.resolve(
                    theme.GRID_LINE_STRONG if hour % 6 == 0 else theme.GRID_LINE))
            if hour % step == 0 and hour < 24:  # each label sits at the start of its own hour(s): none collide
                canvas.create_text(x + 1, top - 11, text=hour_label(hour), anchor="w", font=(theme.FONT_FAMILY, 8),
                                   fill=theme.resolve(theme.TEXT_MUTED), tags=("hour",))

        if snapshot is not None:
            for gap in snapshot.free_gaps:
                self._draw_gap(gap)
            for item in snapshot.timeline:
                self._draw_item(item)
        self._show_details()

    def _draw_gap(self, gap: FreeGap) -> None:
        geometry = self.geometry
        x0, x1 = geometry.x(gap.start_minute), geometry.x(gap.end_minute)
        if x1 - x0 < 6:
            return
        y0 = geometry.lane_top(0) + 5
        y1 = y0 + geometry.lane_height - 10
        self.canvas.create_polygon(rounded_points(x0 + 2, y0, x1 - 2, y1, BLOCK_RADIUS), smooth=True, fill="",
                                   outline=theme.resolve(theme.GRID_LINE_STRONG), dash=(2, 4), tags=("gap",))

    def _draw_item(self, item: TimelineItem) -> None:
        geometry, canvas = self.geometry, self.canvas
        x0, x1 = geometry.x(item.start_minute), max(geometry.x(item.end_minute), geometry.x(item.start_minute) + 2)
        y0 = geometry.lane_top(item.lane) + 3
        y1 = y0 + geometry.lane_height - 6
        style = theme.category_style(item.category)
        fill, text_fill = theme.resolve(style.fill), theme.resolve(style.text)
        outline, dash, width = fill, None, 1
        if item.kind == "fixed":
            outline, width = theme.resolve(theme.GRID_LINE_STRONG), 2
        elif item.kind == "stale":
            outline, dash, width = theme.resolve(theme.WARNING), (5, 3), 2
        if self.selected is not None and self.selected.key == item.key:
            outline, width, dash = theme.resolve(theme.ACCENT), 3, None
        self._bounds[item.key] = (x0, y0, x1, y1)
        # A hairline of the track shows between neighbours, so back-to-back blocks stay distinct.
        canvas.create_polygon(rounded_points(x0 + 1, y0, x1 - 1, y1, BLOCK_RADIUS), smooth=True, fill=fill,
                              outline=outline, width=width, dash=dash, tags=("item", f"box:{item.key}"))
        box_width, box_height = x1 - x0, y1 - y0
        if box_width >= HORIZONTAL_LABEL_WIDTH:
            canvas.create_text(x0 + 8, y0 + 8, text=item.name, anchor="nw", width=box_width - 14,
                               font=(theme.FONT_FAMILY, 9, "bold"), fill=text_fill, tags=("item", "label"))
            caption = {"fixed": "  · fixed", "stale": "  · out of date", "scheduled": ""}[item.kind]
            canvas.create_text(x0 + 8, y1 - 7, text=item.time_text + caption, anchor="sw", width=box_width - 14,
                               font=(theme.FONT_FAMILY, 7), fill=text_fill, tags=("item", "label"))
        elif box_width >= VERTICAL_LABEL_WIDTH:
            # Vertical (read bottom to top): the block's height is the room a narrow block has for its name.
            name = fit_text(item.name, box_height - 12)
            if name:
                canvas.create_text((x0 + x1) / 2, y1 - 7, text=name, anchor="w", angle=90,
                                   font=(theme.FONT_FAMILY, 9, "bold"), fill=text_fill,
                                   tags=("item", "label", f"vertical:{item.key}"))
