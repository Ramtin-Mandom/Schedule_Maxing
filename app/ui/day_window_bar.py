"""
app/ui/day_window_bar.py

The Day Window bar shown above the Day, Week and Month schedules, for the
date the page has selected:

    Day Window · Wed, Jun 5            [Custom for this date]
    Start [ 9]:[00] [AM]   End [ 5]:[30] [PM]   [Apply to this date] [Use default]
    Default from Settings: 12:00 AM – 12:00 AM (next day). ...

Widgets only: reading and saving go through app/ui/day_window.py's
DayWindowController, and DayWindowActions (mixed into the pages, like
TaskFormActions) runs those calls through the page's _io -- at once with
local storage, in a worker with direct PostgreSQL storage -- and re-reads
the page afterwards, since a changed window changes the schedule's
freshness and free time.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import customtkinter as ctk

from app.ui import theme
from app.ui.clock_input import ClockInput
from app.ui.components import AppButton, Card, Notice, font
from app.ui.day_window import DayWindowController, DayWindowState
from app.ui.shell_state import LayoutMode

_OVERRIDE_TONE = "info"
_DEFAULT_TONE = "success"


class DayWindowBar(Card):
    """Start/end of one date's usable day; on_apply(start_text, end_text) and on_default() do the work."""

    def __init__(self, parent, *, on_apply: Callable[[str, str], None], on_default: Callable[[], None]) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self._on_apply, self._on_default = on_apply, on_default
        self.state: DayWindowState | None = None
        self.layout: LayoutMode | None = None

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_M, 4))
        top.columnconfigure(1, weight=1)
        self.title_label = ctk.CTkLabel(top, text="Day Window", font=font(theme.SIZE_HEADING, "bold"),
                                        text_color=theme.TEXT_PRIMARY, anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.badge = ctk.CTkLabel(top, text="", font=font(theme.SIZE_SMALL, "bold"), corner_radius=10, padx=10)
        self.badge.grid(row=0, column=1, sticky="w", padx=(12, 0))

        self.controls = controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        self.start_input = ClockInput(controls, "Start")
        self.end_input = ClockInput(controls, "End", end_of_interval=True)
        self.buttons = buttons = ctk.CTkFrame(controls, fg_color="transparent")
        self.apply_button = AppButton(buttons, "Apply to this date", self.apply, height=theme.CONTROL_HEIGHT)
        self.apply_button.grid(row=0, column=0, padx=(0, 6))
        self.default_button = AppButton(buttons, "Use default", self._on_default, variant="ghost",
                                        height=theme.CONTROL_HEIGHT)
        self.default_button.grid(row=0, column=1)
        for field in (self.start_input, self.end_input):
            for entry in (field.hour_entry, field.minute_entry):
                entry.bind("<Return>", lambda _e: (self.apply(), "break")[1], add="+")

        self.note = ctk.CTkLabel(self, text="", font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED,
                                 anchor="w", justify="left", wraplength=760)
        self.note.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_L, pady=(4, 0))
        self.notice = Notice(self, wraplength=760)
        self.notice.grid(row=3, column=0, sticky="ew", padx=theme.SPACE_L, pady=(6, 0))
        self.notice.hide()
        ctk.CTkFrame(self, fg_color="transparent", height=theme.SPACE_M).grid(row=4, column=0)
        self.set_layout(LayoutMode.WIDE)

    # ------------------------------------------------------------------ layout

    def set_layout(self, mode: LayoutMode) -> None:
        if mode == self.layout:
            return
        self.layout = mode
        for widget in (self.start_input, self.end_input, self.buttons):
            widget.grid_forget()
        wrap = {LayoutMode.WIDE: 760, LayoutMode.MEDIUM: 560, LayoutMode.NARROW: 380}[mode]
        self.note.configure(wraplength=wrap)
        if mode == LayoutMode.NARROW:
            self.start_input.grid(row=0, column=0, sticky="w", pady=(0, 6))
            self.end_input.grid(row=1, column=0, sticky="w", pady=(0, 6))
            self.buttons.grid(row=2, column=0, sticky="w")
        else:
            self.start_input.grid(row=0, column=0, sticky="sw", padx=(0, theme.SPACE_L))
            self.end_input.grid(row=0, column=1, sticky="sw", padx=(0, theme.SPACE_L))
            self.buttons.grid(row=0, column=2, sticky="sw")

    # ------------------------------------------------------------------ state

    def render(self, state: DayWindowState) -> None:
        """Show `state`: the date, its effective start/end, and whether they are its own or the default."""
        unchanged = state == self.state
        self.state = state
        day = state.day
        self.title_label.configure(text=f"Day Window · {day:%a}, {day:%b} {day.day}")
        if not unchanged:  # a re-read of the same window (e.g. after adding a task) keeps what is being typed
            self.start_input.show(state.start_minute)
            self.end_input.show(state.end_minute)
            for field in (self.start_input, self.end_input):
                field.set_error(None)
        tone = theme.TONES[_OVERRIDE_TONE if state.overridden else _DEFAULT_TONE]
        self.badge.configure(text="Custom for this date" if state.overridden else "Default",
                             fg_color=tone.background, text_color=tone.foreground)
        self.default_button.configure(state="normal" if state.overridden else "disabled")
        source = (f"This date has its own window. The default from Settings is {state.default_text}."
                  if state.overridden else
                  f"This date uses the default from Settings ({state.default_text}); applying a window here "
                  "changes this date only.")
        self.note.configure(text=f"{source} Make Schedule places flexible tasks only inside the window "
                                 f"(times in {state.timezone}).")

    def show_error(self, message: str) -> None:
        self.notice.show("error", message)

    def show_success(self, message: str) -> None:
        self.notice.show("success", message)

    def set_enabled(self, enabled: bool) -> None:
        self.start_input.set_enabled(enabled)
        self.end_input.set_enabled(enabled)
        self.apply_button.configure(state="normal" if enabled else "disabled")
        overridden = self.state is not None and self.state.overridden
        self.default_button.configure(state="normal" if enabled and overridden else "disabled")

    def apply(self) -> None:
        self._on_apply(self.start_input.get(), self.end_input.get())


class DayWindowActions:
    """
    The Day Window actions a schedule page shares. The page provides
    `day_window` (a DayWindowController), `window_bar` (a DayWindowBar),
    `_io(work, done, blocking=...)`, `_refuse_while_busy()`, `reload()` and
    `_window_day()` -- the date the page has selected.
    """

    day_window: DayWindowController
    window_bar: DayWindowBar

    def _refresh_day_window(self) -> None:
        """Re-read the selected date's window (after a load or a change of the selected date)."""
        day = self._window_day()

        def done(result) -> None:
            if day != self._window_day():
                return  # the page moved to another date meanwhile
            if result.ok:
                self.window_bar.render(result.value)
            else:
                self.window_bar.show_error(result.error or "The day window could not be read.")

        self._io(lambda: self.day_window.state(day), done, blocking=False)

    def apply_day_window(self, start_text: str, end_text: str) -> None:
        state = self.window_bar.state
        if state is None or self._refuse_while_busy():
            return
        day, version = state.day, state.version
        self._io(lambda: self.day_window.save(day, start_text, end_text, expected_version=version),
                 lambda result: self._day_window_saved(day, result, "own"))

    def use_default_day_window(self) -> None:
        state = self.window_bar.state
        if state is None or not state.overridden or self._refuse_while_busy():
            return
        day, version = state.day, state.version
        self._io(lambda: self.day_window.use_default(day, expected_version=version),
                 lambda result: self._day_window_saved(day, result, "default"))

    def _day_window_saved(self, day: date, result, what: str) -> None:
        if not result.ok:
            self.window_bar.show_error(result.error or "The day window was not saved.")
            return
        state: DayWindowState = result.value
        if day == self._window_day():
            self.window_bar.render(state)
        label = f"{day:%a}, {day:%b} {day.day}"
        if what == "own":
            message = f"{label} now runs {state.text}. Other dates keep their windows."
        else:
            message = f"{label} follows the default day window again ({state.text})."
        self.window_bar.show_success(message + " If it has a saved schedule, Make Schedule updates it to the new window.")
        self.reload()
