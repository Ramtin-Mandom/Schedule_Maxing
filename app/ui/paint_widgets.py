"""App-owned adapters for CustomTkinter's synchronous canvas idle flushes.

CTk 5.2.2 scrollbar/option-menu paints call canvas.update_idletasks(), re-entering
geometry processing while a resize is still being handled. Let the normal Tk loop
flush drawing instead. This touches only the private paint canvas of these controls;
root/widget update_idletasks remains available to callers. No global CTk patch, timer,
worker, or change to scrolling, bindings, colors, scaling or widget geometry.

The private _canvas/_scrollbar integration is covered by native Tk regression tests;
recheck it when upgrading CustomTkinter.

The scrollbars a scrollable frame or text box creates already paint once inside
their constructor; that first flush laid out every existing widget (about 90 ms per
scrollable area at start-up). While those two constructors run, their module's
CTkScrollbar name is AppScrollbar (restored right after; Tk constructs on one thread).
"""
from contextlib import contextmanager

import customtkinter as ctk
from customtkinter.windows.widgets import ctk_scrollable_frame, ctk_textbox


def _leave_idle_to_mainloop():
    """Painting must return to Tk before geometry/idle callbacks are dispatched."""


class AppOptionMenu(ctk.CTkOptionMenu):
    def _draw(self, no_color_updates=False):
        # Also covers the initial draw made by the superclass constructor.
        self._canvas.update_idletasks = _leave_idle_to_mainloop
        super()._draw(no_color_updates=no_color_updates)


class AppScrollbar(ctk.CTkScrollbar):
    def _draw(self, no_color_updates=False):
        # Also covers the initial draw made by the superclass constructor.
        self._canvas.update_idletasks = _leave_idle_to_mainloop
        super()._draw(no_color_updates=no_color_updates)


@contextmanager
def _app_scrollbars(module):
    original = module.CTkScrollbar
    module.CTkScrollbar = AppScrollbar
    try:
        yield
    finally:
        module.CTkScrollbar = original


class AppScrollableFrame(ctk.CTkScrollableFrame):
    def __init__(self, *args, **kwargs):
        with _app_scrollbars(ctk_scrollable_frame):
            super().__init__(*args, **kwargs)
        self._scrollbar._canvas.update_idletasks = _leave_idle_to_mainloop


class AppTextbox(ctk.CTkTextbox):
    def __init__(self, *args, **kwargs):
        with _app_scrollbars(ctk_textbox):
            super().__init__(*args, **kwargs)
        for scrollbar in (self._x_scrollbar, self._y_scrollbar):
            scrollbar._canvas.update_idletasks = _leave_idle_to_mainloop
