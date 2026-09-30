"""
app/ui/clock_input.py

The one time input of the desktop app:

    [ Hour ] : [ Minute ] [ AM/PM ]

- Hour: a numeric text field, 1-12.
- Minute: a numeric text field, 00-59 (shown with two digits once the input
  is left).
- AM/PM: a button; a new input starts at AM and each click toggles
  AM -> PM -> AM. Typing "a" or "p" in either field sets it too.

No spinners, no stepping buttons and no minutes-from-midnight. Every
conversion and check is app/ui/time_fields.py's (clock_to_minutes,
minutes_to_clock, parse_clock_parts), so a typed 1:15 PM is minute 795
everywhere. The value is exact to the minute; nothing is rounded to a grid.

For the forms, ClockInput is a drop-in for a text field: get() returns the
canonical "h:mm AM/PM" text ("" when empty) and `variable.set(text)` fills
it from such text, like the LabeledEntry it replaces. An end-of-interval
input (end_of_interval=True) reads 12:00 AM as the following midnight and
says "next day" beside it.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from app.ui import theme
from app.ui.components import AppButton, Tooltip, font
from app.ui.time_fields import (
    AM,
    MINUTES_PER_DAY,
    PM,
    FieldError,
    clock_parts_text,
    minutes_to_clock,
    parse_clock,
    parse_clock_parts,
    toggle_meridiem,
)


class _ClockText:
    """The input's value as "h:mm AM/PM" text, with the get/set of the text variable it replaces."""

    def __init__(self, owner: "ClockInput") -> None:
        self._owner = owner

    def get(self) -> str:
        return self._owner.get()

    def set(self, text: str) -> None:
        self._owner.set_text(text)


class ClockInput(ctk.CTkFrame):
    """[ Hour ] : [ Minute ] [ AM/PM ] with a label, an optional hint and an inline error."""

    def __init__(self, parent, label: str = "", *, end_of_interval: bool = False, hint: str = "",
                 wraplength: int = 280, on_change: Callable[[], None] | None = None) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.end_of_interval = end_of_interval
        self._on_change = on_change
        self._quiet = False
        self.meridiem = AM
        self.hour_var = tk.StringVar()
        self.minute_var = tk.StringVar()
        self.variable = _ClockText(self)
        self.error = ""
        self._pending_check: str | None = None

        self.label = ctk.CTkLabel(self, text=label, text_color=theme.TEXT_MUTED, font=font(theme.SIZE_SMALL, "bold"),
                                  anchor="w")
        if label:
            self.label.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        controls = self.controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="w")
        entry_style = dict(width=46, height=theme.CONTROL_HEIGHT, corner_radius=theme.RADIUS_CONTROL,
                           border_color=theme.CARD_BORDER, fg_color=theme.INPUT_BG, text_color=theme.TEXT_PRIMARY,
                           justify="center")
        self.hour_entry = ctk.CTkEntry(controls, textvariable=self.hour_var, placeholder_text="hh", **entry_style)
        self.hour_entry.grid(row=0, column=0)
        ctk.CTkLabel(controls, text=":", font=font(theme.SIZE_HEADING, "bold"), text_color=theme.TEXT_PRIMARY,
                     width=10).grid(row=0, column=1, padx=2)
        self.minute_entry = ctk.CTkEntry(controls, textvariable=self.minute_var, placeholder_text="mm", **entry_style)
        self.minute_entry.grid(row=0, column=2)
        self.meridiem_button = AppButton(controls, AM, self.toggle_meridiem, variant="secondary", width=52,
                                         font=font(theme.SIZE_SMALL, "bold"))
        self.meridiem_button.grid(row=0, column=3, padx=(6, 0))
        Tooltip(self.meridiem_button, "Switch AM / PM (or type a / p)")
        self.next_day_label = ctk.CTkLabel(controls, text="next day", font=font(theme.SIZE_CAPTION),
                                           text_color=theme.TEXT_MUTED)
        #: The field a caller focuses or binds keys on (the hour, where typing starts).
        self.entry = self.hour_entry

        self.hint_label = None
        if hint:
            self.hint_label = ctk.CTkLabel(self, text=hint, text_color=theme.TEXT_MUTED, font=font(theme.SIZE_CAPTION),
                                           anchor="w", justify="left", wraplength=wraplength)
            self.hint_label.grid(row=2, column=0, sticky="ew")
        self.error_label = ctk.CTkLabel(self, text="", text_color=theme.TONES["error"].foreground,
                                        font=font(theme.SIZE_CAPTION), anchor="w", justify="left", wraplength=wraplength)
        self._border = theme.CARD_BORDER

        self.label.bind("<Button-1>", lambda _e: self.hour_entry.focus_set(), add="+")
        for entry in (self.hour_entry, self.minute_entry):
            entry.bind("<FocusIn>", lambda _e, e=entry: self._ring(e, True), add="+")
            entry.bind("<FocusOut>", lambda _e, e=entry: self._left(e), add="+")
            entry.bind("<Return>", lambda _e: self.normalize(), add="+")
            entry.bind("<KP_Enter>", lambda _e: self.normalize(), add="+")
            for key, meridiem in (("a", AM), ("A", AM), ("p", PM), ("P", PM)):
                entry.bind(f"<KeyPress-{key}>", lambda _e, m=meridiem: (self.set_meridiem(m), "break")[1], add="+")
        self.hour_entry.bind("<KeyPress-colon>", lambda _e: (self._to_minutes(), "break")[1], add="+")
        self.hour_entry.bind("<KeyRelease>", self._hour_typed, add="+")
        self.hour_var.trace_add("write", lambda *_: self._changed())
        self.minute_var.trace_add("write", lambda *_: self._changed())

    # ------------------------------------------------------------------ value

    def get(self) -> str:
        """The time as "h:mm AM/PM" text ("" when empty; the raw parts when they are not a valid time)."""
        return clock_parts_text(self.hour_var.get(), self.minute_var.get(), self.meridiem,
                                end_of_interval=self.end_of_interval)

    def value(self) -> int:
        """Minutes from local midnight (1440 for an end at the following midnight); FieldError if unreadable."""
        return parse_clock_parts(self.hour_var.get(), self.minute_var.get(), self.meridiem,
                                 end_of_interval=self.end_of_interval)

    def is_empty(self) -> bool:
        return not self.hour_var.get().strip() and not self.minute_var.get().strip()

    def show(self, minutes: int) -> None:
        """Show minutes from midnight (0..1440) as hour, two-digit minute and AM/PM."""
        hour, minute, meridiem = minutes_to_clock(minutes)
        self._quiet = True
        try:
            self.hour_var.set(str(hour))
            self.minute_var.set(f"{minute:02d}")
            self.set_meridiem(meridiem)
        finally:
            self._quiet = False
        self._changed()

    def set_text(self, text: str) -> None:
        """Fill from "h:mm AM/PM"-style text; empty text clears the input (back to AM)."""
        raw = (text or "").strip()
        if not raw:
            self.clear()
            return
        try:
            self.show(parse_clock(raw, end_of_interval=self.end_of_interval))
        except FieldError:
            # Not a time: keep what was given visible (never guessed); reading it reports why.
            hour, _, minute = raw.partition(":")
            self._quiet = True
            self.hour_var.set(hour.strip())
            self.minute_var.set(minute.strip())
            self._quiet = False
            self._changed()

    def clear(self) -> None:
        self._quiet = True
        self.hour_var.set("")
        self.minute_var.set("")
        self.set_meridiem(AM)
        self._quiet = False
        self._changed()

    def set_meridiem(self, meridiem: str) -> None:
        self.meridiem = meridiem
        self.meridiem_button.configure(text=meridiem)
        self._changed()

    def toggle_meridiem(self) -> None:
        self.set_meridiem(toggle_meridiem(self.meridiem))

    def normalize(self) -> None:
        """Rewrite what was typed canonically (minute as two digits), or show why it is not a time."""
        if self.is_empty():
            self.set_error(None)
            return
        try:
            self.show(self.value())
            self.set_error(None)
        except FieldError as error:
            self.set_error(str(error))

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (self.hour_entry, self.minute_entry, self.meridiem_button):
            widget.configure(state=state)

    # ------------------------------------------------------------------ errors

    def set_error(self, message: str | None) -> None:
        """Show (or clear) an error under the input; the borders change too, but the text says it."""
        self.error = message or ""
        self._border = theme.TONES["error"].border if message else theme.CARD_BORDER
        for entry in (self.hour_entry, self.minute_entry):
            entry.configure(border_color=self._border)
        if message:
            self.error_label.configure(text=f"Error: {message}")
            self.error_label.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        else:
            self.error_label.grid_remove()

    # ------------------------------------------------------------------ typing

    def _changed(self) -> None:
        if self._quiet:
            return
        next_day = False
        if self.end_of_interval:
            try:
                next_day = self.value() == MINUTES_PER_DAY
            except FieldError:
                next_day = False
        if next_day:
            self.next_day_label.grid(row=0, column=4, padx=(6, 0))
        else:
            self.next_day_label.grid_remove()
        if self._on_change is not None:
            self._on_change()

    def _hour_typed(self, event) -> None:
        """Two hour digits (or one that cannot start a two-digit hour, 2-9) move on to the minutes."""
        # The keysym, not event.char: X11 Tk leaves the char of every KeyRelease empty.
        if not getattr(event, "keysym", "").removeprefix("KP_").isdigit():
            return
        text = self.hour_var.get().strip()
        if text.isdigit() and (len(text) >= 2 or int(text) >= 2):
            self._to_minutes()

    def _to_minutes(self) -> None:
        self.minute_entry.focus_set()
        self.minute_entry.select_range(0, "end")

    def _left(self, entry) -> None:
        self._ring(entry, False)
        # Normalize only once the focus has left the whole input (not when moving from hour to minute).
        if self._pending_check is None:
            self._pending_check = self.after_idle(self._normalize_if_left)

    def destroy(self) -> None:
        if self._pending_check is not None:
            try:
                self.after_cancel(self._pending_check)
            except tk.TclError:
                pass
            self._pending_check = None
        super().destroy()

    def _normalize_if_left(self) -> None:
        self._pending_check = None
        if not self.winfo_exists():
            return
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None or not str(focused).startswith(str(self) + "."):
            self.normalize()

    def _ring(self, entry, focused: bool) -> None:
        if self.winfo_exists():
            entry.configure(border_color=theme.FOCUS_RING if focused else self._border, border_width=2 if focused else 1)
