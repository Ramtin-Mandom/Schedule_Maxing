"""Plain Tk widgets themed like CustomTkinter (app/ui/light_widgets.py): one native window each (real windows)."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk
import pytest

from app.ui import theme
from app.ui.light_widgets import LightButton, LightFrame, LightLabel
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display


@pytest.fixture
def root():
    ctk.set_appearance_mode("light")
    window = ctk.CTk()
    window.withdraw()
    card = ctk.CTkFrame(window, fg_color=theme.CARD_BG)
    card.pack(fill="both", expand=True)
    yield card
    ctk.set_appearance_mode("light")
    ctk.set_widget_scaling(1.0)
    window.update()
    window.destroy()


def test_light_widgets_follow_light_dark_and_the_interface_size(root) -> None:
    frame = LightFrame(root)
    label = LightLabel(frame, "Task", text_color=theme.TEXT_PRIMARY, wraplength=100)
    frame.grid(row=0, column=0, padx=10)
    label.grid(row=0, column=0)
    assert tk.Frame.cget(frame, "bg").lower() == theme.CARD_BG[0].lower()  # the card's colour, not the window's
    assert frame.cget("bg") == theme.CARD_BG  # the (light, dark) pair, for CustomTkinter widgets inside
    assert label.cget("fg").lower() == theme.TEXT_PRIMARY[0].lower()

    ctk.set_appearance_mode("dark")
    root.update()
    assert tk.Frame.cget(frame, "bg").lower() == theme.CARD_BG[1].lower()
    assert label.cget("fg").lower() == theme.TEXT_PRIMARY[1].lower()

    ctk.set_widget_scaling(1.5)
    root.update()
    assert int(label.cget("wraplength")) == 150
    label.configure(wraplength=200, text="Changed")
    assert int(label.cget("wraplength")) == 300 and label.cget("text") == "Changed"


def test_a_light_button_works_like_an_app_button(root) -> None:
    clicks = []
    button = LightButton(root, "Edit", lambda: clicks.append(1), variant="secondary")
    button.grid(row=0, column=0)
    assert int(button.cget("takefocus")) == 1 and int(button.cget("highlightthickness")) == 2  # Tab + focus ring
    assert all(button.bind(sequence) for sequence in ("<Return>", "<KP_Enter>", "<space>"))
    button.invoke()
    button._activate()  # what Enter and Space call
    assert clicks == [1, 1]
    button.configure(state="disabled", text="Busy")
    button._activate()
    assert clicks == [1, 1] and button.cget("text") == "Busy"
    button.configure(state="normal", command=lambda: clicks.append(2))
    button.invoke()
    assert clicks == [1, 1, 2]
    button._set_hover(True)  # what <Enter> calls
    assert button.cget("bg").lower() == theme.SECONDARY_HOVER[0].lower()
    button._set_hover(False)
    assert button.cget("bg").lower() == theme.SECONDARY_BG[0].lower()


def test_destroyed_light_widgets_leave_no_tracker_callbacks(root) -> None:
    from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import AppearanceModeTracker

    before = len(AppearanceModeTracker.callback_list)
    widgets = [LightLabel(root, "x"), LightButton(root, "y"), LightFrame(root)]
    assert len(AppearanceModeTracker.callback_list) == before + 3
    for widget in widgets:
        widget.destroy()
    assert len(AppearanceModeTracker.callback_list) == before
