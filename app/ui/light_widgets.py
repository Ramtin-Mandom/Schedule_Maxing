"""
app/ui/light_widgets.py

Plain Tk labels, buttons and frames for long lists, themed like the
CustomTkinter widgets around them.

Why: Tk gives every widget its own native (Win32) child window, and a
CustomTkinter widget is two or three of them (a frame, a drawing canvas and
often an inner label). Moving or resizing the application window costs the
Windows compositor (dwm.exe) work for every visible child window -- measured
on the Day page with 200 tasks: about 556 child windows and 22-24% of one core
in dwm.exe while the window is dragged, against 7-10% for an empty window
(docs/desktop-performance-round3.md). Each widget here is ONE native window
and draws natively (no Python canvas redraw on every <Configure>), so the
available-task chips and the status-board cards need a fraction of them.

They follow light/dark and the interface size through CustomTkinter's own
trackers (the same callbacks its widgets register), take the theme's
(light, dark) colours, and keep the keyboard behaviour of AppButton: Tab
focus with a visible focus ring, Enter/Space activation. What changes is the
look: square corners and no outline (a ghost button's border is not drawn).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from customtkinter.windows.widgets.appearance_mode import CTkAppearanceModeBaseClass
from customtkinter.windows.widgets.scaling import CTkScalingBaseClass

from app.ui import theme

# app/ui/components.py builds its labelled fields on these classes, so it is imported only where needed here.

Color = str | tuple[str, str]


def _font(size: int, weight: str):
    from app.ui.components import font

    return font(size, weight)


def surface_color(widget) -> Color:
    """The colour a transparent widget inside `widget` shows: the nearest filled CTk/light container's."""
    while widget is not None:
        light = getattr(widget, "_light_bg", None)
        if light is not None:
            return light
        fill = getattr(widget, "_fg_color", None)
        if fill is not None and fill != "transparent":
            return fill
        widget = getattr(widget, "master", None)
    return theme.APP_BG


def _scaled_pad(value, scaling: float):
    if isinstance(value, (tuple, list)):
        return tuple(round(part * scaling) for part in value)
    return round(value * scaling) if isinstance(value, (int, float)) else value


class _Themed(CTkAppearanceModeBaseClass, CTkScalingBaseClass):
    """A plain Tk widget registered with CustomTkinter's appearance and scaling trackers (see the module doc)."""

    def _start_theming(self) -> None:
        CTkAppearanceModeBaseClass.__init__(self)
        CTkScalingBaseClass.__init__(self, scaling_type="widget")
        self._restyle()

    def _set_appearance_mode(self, mode_string: str) -> None:
        CTkAppearanceModeBaseClass._set_appearance_mode(self, mode_string)
        self._restyle()

    def _set_scaling(self, new_widget_scaling, new_window_scaling) -> None:
        CTkScalingBaseClass._set_scaling(self, new_widget_scaling, new_window_scaling)
        self._restyle()

    def _restyle(self) -> None:
        raise NotImplementedError

    def _color(self, color: Color) -> str:
        return self._apply_appearance_mode(color)

    def grid(self, **kwargs):
        """Like a CTk widget's grid(): padding is given in logical pixels and scaled."""
        scaling = self._get_widget_scaling()
        for key in ("padx", "pady"):
            if key in kwargs:
                kwargs[key] = _scaled_pad(kwargs[key], scaling)
        return tk.Grid.grid_configure(self, **kwargs)

    def _stop_theming(self) -> None:
        CTkAppearanceModeBaseClass.destroy(self)
        CTkScalingBaseClass.destroy(self)


class LightFrame(_Themed, tk.Frame):
    """A container in one colour (default: its parent's surface), one native window instead of two."""

    def __init__(self, parent, *, fg_color: Color | None = None, **kwargs) -> None:
        tk.Frame.__init__(self, parent, bd=0, highlightthickness=0, **kwargs)
        self._light_bg = fg_color if fg_color is not None else surface_color(parent)
        self._start_theming()

    def _restyle(self) -> None:
        tk.Frame.configure(self, bg=self._color(self._light_bg))

    def cget(self, key):
        # CustomTkinter widgets inside take their corner colour from a plain Tk master's cget("bg"):
        # the (light, dark) pair lets them follow a light/dark switch too.
        if key in ("bg", "background"):
            return self._light_bg
        return tk.Frame.cget(self, key)

    __getitem__ = cget

    def destroy(self) -> None:
        self._stop_theming()
        tk.Frame.destroy(self)


class LightLabel(_Themed, tk.Label):
    """
    Text in the theme's font, colour and scaled wrap length; one native window instead of a CTkLabel's three.
    configure() takes text, text_color, wraplength (logical pixels), anchor and justify.
    """

    def __init__(self, parent, text: str = "", *, size: int = theme.SIZE_BODY, weight: str = "normal",
                 text_color: Color = theme.TEXT_PRIMARY, fg_color: Color | None = None, wraplength: int = 0,
                 anchor: str = "w", justify: str = "left") -> None:
        tk.Label.__init__(self, parent, text=text, anchor=anchor, justify=justify, bd=0, padx=0, pady=0,
                          highlightthickness=0)
        self._font = _font(size, weight)
        self._text_color = text_color
        self._light_bg = fg_color if fg_color is not None else surface_color(parent)
        self._wraplength = wraplength
        self._start_theming()

    def _restyle(self) -> None:
        scaling = self._get_widget_scaling()
        tk.Label.configure(self, font=self._font.create_scaled_tuple(scaling), fg=self._color(self._text_color),
                           bg=self._color(self._light_bg), wraplength=round(self._wraplength * scaling))

    def configure(self, **kwargs):
        restyle = False
        if "text_color" in kwargs:
            self._text_color, restyle = kwargs.pop("text_color"), True
        if "wraplength" in kwargs:
            self._wraplength, restyle = kwargs.pop("wraplength"), True
        if "fg_color" in kwargs:
            self._light_bg, restyle = kwargs.pop("fg_color"), True
        if kwargs:
            tk.Label.configure(self, **kwargs)
        if restyle:
            self._restyle()

    config = configure

    def destroy(self) -> None:
        self._stop_theming()
        tk.Label.destroy(self)


class LightButton(_Themed, tk.Button):
    """
    A button in one of AppButton's variants for long lists: one native window, native drawing.
    Keyboard: Tab focus with a focus ring, Enter/Space activate. invoke(), cget/configure(text, command,
    state) work as on AppButton; text_color/fg_color/hover_color may override the variant.
    """

    def __init__(self, parent, text: str, command: Callable[[], object] | None = None, *,
                 variant: str = "secondary", size: int = theme.SIZE_BODY, weight: str = "bold",
                 padx: int = 8, pady: int = 4, **style) -> None:
        from app.ui.components import _BUTTON_VARIANTS, make_keyboard_accessible

        tk.Button.__init__(self, parent, text=text, command=command, relief="flat", bd=0, overrelief="flat",
                           cursor="hand2", takefocus=1, highlightthickness=2)
        colors = dict(_BUTTON_VARIANTS[variant])
        colors.update(style)
        self.variant = variant
        self._fill = colors["fg_color"] if colors["fg_color"] != "transparent" else None
        self._hover = colors["hover_color"]
        self._text_color = colors["text_color"]
        self._surface = surface_color(parent)
        self._font = _font(size, weight)
        self._pad = (padx, pady)
        self._hovering = False
        self.bind("<Enter>", lambda _e: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda _e: self._set_hover(False), add="+")
        make_keyboard_accessible(self, activate=self._activate, ring=False)
        self._start_theming()

    def _activate(self) -> None:
        if str(self.cget("state")) != "disabled":
            self.invoke()

    def _set_hover(self, hovering: bool) -> None:
        self._hovering = hovering and str(self.cget("state")) != "disabled"
        self._restyle()

    def _restyle(self) -> None:
        scaling = self._get_widget_scaling()
        surface = self._color(self._surface)
        fill = self._color(self._hover) if self._hovering else (self._color(self._fill) if self._fill else surface)
        tk.Button.configure(
            self, font=self._font.create_scaled_tuple(scaling), fg=self._color(self._text_color), bg=fill,
            activebackground=self._color(self._hover), activeforeground=self._color(self._text_color),
            disabledforeground=self._color(theme.TEXT_MUTED), highlightbackground=surface,
            highlightcolor=self._color(theme.FOCUS_RING), padx=round(self._pad[0] * scaling),
            pady=round(self._pad[1] * scaling))

    def configure(self, **kwargs):
        if "text_color" in kwargs:
            self._text_color = kwargs.pop("text_color")
        if kwargs:
            tk.Button.configure(self, **kwargs)
        if "state" in kwargs and kwargs["state"] == "disabled":
            self._hovering = False
        self._restyle()

    config = configure

    def destroy(self) -> None:
        self._stop_theming()
        tk.Button.destroy(self)
