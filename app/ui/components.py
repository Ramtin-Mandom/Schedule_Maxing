"""
app/ui/components.py

Reusable, themed desktop widgets for the Milestone 4 shell and pages:
buttons, labelled entries and selects, cards and section titles, notices,
loading/empty/error states, context menus, modal dialogs (confirmation and
choice), drawers and tooltips.

Accessibility conventions every component follows:

- Keyboard: every button and select takes Tab focus, shows a visible focus
  ring, and activates with Enter or Space; selects also change with the
  Up/Down/Home/End keys and open with Alt+Down/F4. Dialogs and drawers close
  with Escape, confirm their primary action with Enter, put the focus inside
  when they open and give it back to where it was when they close.
- Labels are visible and belong to their field (clicking a label focuses
  its field); errors are shown as text beside the field, not only as color.
- Status is said in words (Notice prefixes "Error:", "Warning:", ...).
- Colors come from app/ui/theme.py as (light, dark) pairs, so everything
  follows the light/dark appearance.

Widgets hold no scheduling, SQL or synchronization logic: pages pass them
values and callbacks.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import customtkinter as ctk

from app.ui import theme

# -----------------------------------------------------------------------------
# Fonts and focus
# -----------------------------------------------------------------------------


def font(size: int = theme.SIZE_BODY, weight: str = "normal") -> ctk.CTkFont:
    """A font that follows CustomTkinter's widget scaling (the interface scale)."""
    return ctk.CTkFont(size=size, weight=weight)


def focus_target(widget) -> tk.Misc:
    """The Tk widget that actually receives keyboard focus (CustomTkinter delegates to an inner label/entry)."""
    for name in ("_entry", "_text_label"):
        inner = getattr(widget, name, None)
        if isinstance(inner, tk.Misc):
            return inner
    return widget


def make_keyboard_accessible(widget, *, activate: Callable[[], None] | None = None, ring: bool = True) -> tk.Misc:
    """
    Make a CustomTkinter control reachable with Tab, show a focus ring while
    it has the focus, and (with `activate`) trigger it with Enter/Space.
    Returns the widget that receives the focus.
    """
    target = focus_target(widget)
    try:
        tk.Misc.configure(target, takefocus=1)  # Tk's own option (CustomTkinter's configure does not accept it)
    except tk.TclError:
        pass
    if ring and hasattr(widget, "cget"):
        try:
            normal = (widget.cget("border_width"), widget.cget("border_color"))
        except (ValueError, tk.TclError, AttributeError):
            normal = None
        if normal is not None:
            def on_focus_in(_event=None) -> None:
                if widget.winfo_exists():
                    widget.configure(border_width=2, border_color=theme.FOCUS_RING)

            def on_focus_out(_event=None) -> None:
                if widget.winfo_exists():
                    widget.configure(border_width=normal[0], border_color=normal[1])

            target.bind("<FocusIn>", on_focus_in, add="+")
            target.bind("<FocusOut>", on_focus_out, add="+")
    if activate is not None:
        def on_key(_event=None) -> str:
            activate()
            return "break"

        for sequence in ("<Return>", "<KP_Enter>", "<space>"):
            target.bind(sequence, on_key, add="+")
    return target


# -----------------------------------------------------------------------------
# Buttons
# -----------------------------------------------------------------------------

_BUTTON_VARIANTS: dict[str, dict] = {
    "primary": dict(fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER, text_color=theme.TEXT_ON_ACCENT,
                    border_color=theme.ACCENT),
    "secondary": dict(fg_color=theme.SECONDARY_BG, hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT_PRIMARY,
                      border_color=theme.SECONDARY_BG),
    "neutral": dict(fg_color=theme.NEUTRAL_BG, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT_ON_ACCENT,
                    border_color=theme.NEUTRAL_BG),
    "danger": dict(fg_color=theme.DANGER, hover_color=theme.DANGER_HOVER, text_color=theme.TEXT_ON_ACCENT,
                   border_color=theme.DANGER),
    "ghost": dict(fg_color="transparent", hover_color=theme.SECONDARY_BG, text_color=theme.TEXT_PRIMARY,
                  border_color=theme.CARD_BORDER),
}


class AppButton(ctk.CTkButton):
    """A rounded, keyboard-operable button in one of the design variants (primary, secondary, neutral, danger, ghost)."""

    def __init__(self, parent, text: str, command: Callable[[], object] | None = None, *, variant: str = "primary",
                 height: int = theme.CONTROL_HEIGHT, **kwargs) -> None:
        style = dict(_BUTTON_VARIANTS[variant])
        style.update(kwargs.pop("style", {}))
        kwargs.setdefault("corner_radius", theme.RADIUS_CONTROL)
        kwargs.setdefault("font", font(theme.SIZE_BODY, "bold"))
        super().__init__(parent, text=text, command=command, height=height, border_width=1 if variant == "ghost" else 0,
                         **style, **kwargs)
        self.variant = variant
        make_keyboard_accessible(self, activate=self.invoke)


# -----------------------------------------------------------------------------
# Containers and text
# -----------------------------------------------------------------------------


class Card(ctk.CTkFrame):
    """A soft, rounded surface with a thin border."""

    def __init__(self, parent, **kwargs) -> None:
        kwargs.setdefault("fg_color", theme.CARD_BG)
        kwargs.setdefault("border_color", theme.CARD_BORDER)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("corner_radius", theme.RADIUS_CARD)
        super().__init__(parent, **kwargs)


class SectionTitle(ctk.CTkFrame):
    """A heading with an optional one-line explanation."""

    def __init__(self, parent, title: str, subtitle: str = "", *, wraplength: int = 0) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(self, text=title, font=font(theme.SIZE_HEADING, "bold"),
                                        text_color=theme.TEXT_PRIMARY, anchor="w")
        self.title_label.grid(row=0, column=0, sticky="ew")
        self.subtitle_label = None
        if subtitle:
            self.subtitle_label = ctk.CTkLabel(self, text=subtitle, font=font(theme.SIZE_SMALL),
                                               text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                                               wraplength=wraplength)
            self.subtitle_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))


class Notice(ctk.CTkFrame):
    """An inline status message; the tone is also spelled out ("Error: ...") so it never depends on color."""

    def __init__(self, parent, *, wraplength: int = 520, dismissible: bool = True) -> None:
        super().__init__(parent, corner_radius=theme.RADIUS_CONTROL, border_width=1)
        self.columnconfigure(0, weight=1)
        self.tone: str | None = None
        self.label = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=wraplength,
                                  font=font(theme.SIZE_SMALL))
        self.label.grid(row=0, column=0, sticky="ew", padx=(12, 6), pady=8)
        self.dismiss_button = None
        if dismissible:
            self.dismiss_button = AppButton(self, "Dismiss", self.hide, variant="ghost", height=28, width=80,
                                            font=font(theme.SIZE_CAPTION))
            self.dismiss_button.grid(row=0, column=1, padx=(0, 8), pady=6)
        self._removed = False

    @property
    def text(self) -> str:
        return self.label.cget("text")

    def show(self, tone: str, message: str) -> None:
        tone_style = theme.TONES[tone]
        self.tone = tone
        self.configure(fg_color=tone_style.background, border_color=tone_style.border)
        self.label.configure(text=f"{tone_style.label}: {message}", text_color=tone_style.foreground)
        if self._removed:  # grid_remove() remembered where the caller placed it
            self._removed = False
            self.grid()

    def hide(self) -> None:
        """Hide the notice (the caller grids it once; show() puts it back in the same place)."""
        self.tone = None
        if self.winfo_manager() == "grid":
            self.grid_remove()
            self._removed = True


class StateView(ctk.CTkFrame):
    """The loading / empty / error state of a view, with an optional action (e.g. Retry)."""

    def __init__(self, parent, *, wraplength: int = 420) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.state: str | None = None
        self.title_label = ctk.CTkLabel(self, text="", font=font(theme.SIZE_HEADING, "bold"),
                                        text_color=theme.TEXT_PRIMARY)
        self.message_label = ctk.CTkLabel(self, text="", font=font(theme.SIZE_BODY), text_color=theme.TEXT_MUTED,
                                          justify="center", wraplength=wraplength)
        self.action_button = AppButton(self, "Try again", None, variant="secondary", width=140)
        self.title_label.grid(row=0, column=0, pady=(24, 4))
        self.message_label.grid(row=1, column=0, padx=16)

    def _set(self, state: str, title: str, message: str, action: tuple[str, Callable[[], None]] | None) -> None:
        self.state = state
        self.title_label.configure(text=title)
        self.message_label.configure(text=message)
        if action is None:
            self.action_button.grid_remove()
        else:
            self.action_button.configure(text=action[0], command=action[1])
            self.action_button.grid(row=2, column=0, pady=(12, 24))

    def loading(self, message: str = "Loading...") -> None:
        self._set("loading", "Loading", message, None)

    def empty(self, title: str, message: str, action: tuple[str, Callable[[], None]] | None = None) -> None:
        self._set("empty", title, message, action)

    def error(self, message: str, retry: Callable[[], None] | None = None) -> None:
        self._set("error", "Something went wrong", f"Error: {message}", ("Try again", retry) if retry else None)


# -----------------------------------------------------------------------------
# Form fields
# -----------------------------------------------------------------------------


class LabeledEntry(ctk.CTkFrame):
    """A visible label, its entry, an optional hint, and an inline error message."""

    def __init__(self, parent, label: str, variable: tk.StringVar | None = None, *, placeholder: str = "",
                 hint: str = "", width: int = 0, wraplength: int = 280) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.variable = variable or tk.StringVar()
        self.label = ctk.CTkLabel(self, text=label, text_color=theme.TEXT_MUTED, font=font(theme.SIZE_SMALL, "bold"),
                                  anchor="w")
        self.label.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        options = dict(textvariable=self.variable, placeholder_text=placeholder or None, height=theme.CONTROL_HEIGHT,
                       corner_radius=theme.RADIUS_CONTROL, border_color=theme.CARD_BORDER, fg_color=theme.INPUT_BG,
                       text_color=theme.TEXT_PRIMARY)
        if width:
            options["width"] = width
        self.entry = ctk.CTkEntry(self, **options)
        self.entry.grid(row=1, column=0, sticky="ew")
        self._border = theme.CARD_BORDER
        self.entry.bind("<FocusIn>", lambda _e: self._ring(True), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self._ring(False), add="+")
        self.label.bind("<Button-1>", lambda _e: self.entry.focus_set(), add="+")
        self.hint_label = None
        if hint:
            self.hint_label = ctk.CTkLabel(self, text=hint, text_color=theme.TEXT_MUTED, font=font(theme.SIZE_CAPTION),
                                           anchor="w", justify="left", wraplength=wraplength)
            self.hint_label.grid(row=2, column=0, sticky="ew")
        self.error_label = ctk.CTkLabel(self, text="", text_color=theme.TONES["error"].foreground,
                                        font=font(theme.SIZE_CAPTION), anchor="w", justify="left", wraplength=wraplength)
        self.error = ""

    def _ring(self, focused: bool) -> None:
        if self.winfo_exists():
            self.entry.configure(border_color=theme.FOCUS_RING if focused else self._border,
                                 border_width=2 if focused else 1)

    def get(self) -> str:
        return self.variable.get()

    def set_error(self, message: str | None) -> None:
        """Show (or clear) an error under the field; the border changes too, but the text says it."""
        self.error = message or ""
        self._border = theme.TONES["error"].border if message else theme.CARD_BORDER
        self.entry.configure(border_color=self._border)
        if message:
            self.error_label.configure(text=f"Error: {message}")
            self.error_label.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        else:
            self.error_label.grid_remove()


class LabeledSelect(ctk.CTkFrame):
    """A visible label and a keyboard-operable drop-down of fixed choices."""

    def __init__(self, parent, label: str, values: Sequence[str], variable: tk.StringVar | None = None, *,
                 command: Callable[[str], None] | None = None, width: int = 0) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.values = list(values)
        self.variable = variable or tk.StringVar(value=self.values[0] if self.values else "")
        self._command = command
        self.label = ctk.CTkLabel(self, text=label, text_color=theme.TEXT_MUTED, font=font(theme.SIZE_SMALL, "bold"),
                                  anchor="w")
        self.label.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        options = dict(variable=self.variable, values=self.values, command=self._changed, height=theme.CONTROL_HEIGHT,
                       corner_radius=theme.RADIUS_CONTROL, fg_color=theme.INPUT_BG, button_color=theme.SECONDARY_BG,
                       button_hover_color=theme.SECONDARY_HOVER, text_color=theme.TEXT_PRIMARY,
                       dropdown_fg_color=theme.CARD_BG, dropdown_hover_color=theme.ACCENT_SOFT,
                       dropdown_text_color=theme.TEXT_PRIMARY)
        if width:
            options["width"] = width
        self.menu = ctk.CTkOptionMenu(self, **options)
        self.menu.grid(row=1, column=0, sticky="ew")
        target = make_keyboard_accessible(self.menu, ring=False)
        self._target = target
        target.bind("<FocusIn>", lambda _e: self._ring(True), add="+")
        target.bind("<FocusOut>", lambda _e: self._ring(False), add="+")
        for sequence, handler in (("<Up>", lambda: self.step(-1)), ("<Down>", lambda: self.step(1)),
                                  ("<Home>", lambda: self.choose(self.values[0])),
                                  ("<End>", lambda: self.choose(self.values[-1])),
                                  ("<Alt-Down>", self.open), ("<F4>", self.open), ("<Return>", self.open),
                                  ("<space>", self.open)):
            target.bind(sequence, lambda _e, h=handler: (h(), "break")[1], add="+")
        self.label.bind("<Button-1>", lambda _e: target.focus_set(), add="+")

    def _ring(self, focused: bool) -> None:
        if self.winfo_exists():
            self.menu.configure(fg_color=theme.ACCENT_SOFT if focused else theme.INPUT_BG)

    def _changed(self, value: str) -> None:
        if self._command is not None:
            self._command(value)

    def get(self) -> str:
        return self.variable.get()

    def set_values(self, values: Sequence[str], selected: str | None = None) -> None:
        """Replace the choices (keeping the current one when it is still offered)."""
        self.values = list(values)
        self.menu.configure(values=self.values)
        target = selected if selected is not None else self.get()
        self.variable.set(target if target in self.values else (self.values[0] if self.values else ""))

    def choose(self, value: str) -> None:
        if value not in self.values or value == self.get():
            return
        self.menu.set(value)
        self._changed(value)

    def step(self, offset: int) -> None:
        if not self.values:
            return
        index = self.values.index(self.get()) if self.get() in self.values else 0
        self.choose(self.values[max(0, min(len(self.values) - 1, index + offset))])

    def open(self) -> None:
        if str(self.menu.cget("state")) != "disabled":
            self.menu._open_dropdown_menu()

    def focus_set(self) -> None:
        self._target.focus_set()


# -----------------------------------------------------------------------------
# Tooltips and context menus
# -----------------------------------------------------------------------------


class Tooltip:
    """A short text shown after hovering or focusing a widget; hidden on leave, blur or destroy."""

    def __init__(self, widget, text: str, *, delay_ms: int = 450) -> None:
        self.widget = widget
        self.text = text
        self._delay_ms = delay_ms
        self._pending = None
        self._window: tk.Toplevel | None = None
        target = focus_target(widget)
        for sequence in ("<Enter>", "<FocusIn>"):
            target.bind(sequence, lambda _e: self._schedule(), add="+")
        for sequence in ("<Leave>", "<FocusOut>", "<ButtonPress>", "<Destroy>"):
            target.bind(sequence, lambda _e: self.hide(), add="+")

    def _schedule(self) -> None:
        self.hide()
        self._pending = self.widget.after(self._delay_ms, self.show)

    def show(self) -> None:
        self._pending = None
        if self._window is not None or not self.text or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 6
        y = self.widget.winfo_rooty() + max(0, self.widget.winfo_height() // 2 - 12)
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        tk.Label(window, text=self.text, background=theme.resolve(theme.TEXT_PRIMARY),
                 foreground=theme.resolve(theme.CARD_BG), padx=8, pady=4, font=(theme.FONT_FAMILY, 9)).pack()
        self._window = window

    def hide(self) -> None:
        if self._pending is not None:
            try:
                self.widget.after_cancel(self._pending)
            except tk.TclError:
                pass
            self._pending = None
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


@dataclass(frozen=True)
class MenuItem:
    label: str
    command: Callable[[], None]
    enabled: bool = True
    #: A destructive action (its label should say so, e.g. "Remove...").
    danger: bool = False


class ContextMenu:
    """
    A themed pop-up menu of actions. attach() opens it with a right-click and
    from the keyboard (the Menu key or Shift+F10); Tk's menu handles the
    arrow keys, Enter and Escape.
    """

    def __init__(self, parent) -> None:
        self.parent = parent
        self.menu: tk.Menu | None = None

    def build(self, items: Sequence[MenuItem]) -> tk.Menu:
        if self.menu is not None:
            self.menu.destroy()
        menu = tk.Menu(self.parent, tearoff=0, background=theme.resolve(theme.CARD_BG),
                       foreground=theme.resolve(theme.TEXT_PRIMARY), activebackground=theme.resolve(theme.ACCENT_SOFT),
                       activeforeground=theme.resolve(theme.TEXT_PRIMARY), borderwidth=1,
                       font=(theme.FONT_FAMILY, 10))
        for item in items:
            menu.add_command(label=item.label, command=item.command, state="normal" if item.enabled else "disabled",
                             foreground=theme.resolve(theme.DANGER) if item.danger else None)
        self.menu = menu
        return menu

    def popup(self, items: Sequence[MenuItem], x: int, y: int) -> None:
        if not items:
            return
        menu = self.build(items)
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def attach(self, widget, items: Callable[[tk.Event | None], Sequence[MenuItem]]) -> None:
        def by_mouse(event) -> None:
            self.popup(items(event), event.x_root, event.y_root)

        def by_keyboard(_event) -> str:
            x, y = widget.winfo_rootx() + 24, widget.winfo_rooty() + 24
            self.popup(items(None), x, y)
            return "break"

        widget.bind("<Button-3>", by_mouse, add="+")
        widget.bind("<Shift-F10>", by_keyboard, add="+")
        widget.bind("<App>", by_keyboard, add="+")


# -----------------------------------------------------------------------------
# Dialogs and drawers
# -----------------------------------------------------------------------------


def _current_focus(widget) -> tk.Misc | None:
    try:
        return widget.focus_get()
    except (KeyError, tk.TclError):  # e.g. focus inside a combobox popdown
        return None


def _restore_focus(target: tk.Misc | None) -> None:
    try:
        if target is not None and target.winfo_exists():
            target.focus_set()
    except tk.TclError:
        pass


class ModalDialog(ctk.CTkToplevel):
    """
    A modal dialog: Escape cancels, Enter runs the primary action, closing
    the window cancels, the focus starts inside and returns to where it was.
    Subclasses build their content in `body` and call add_buttons().
    """

    def __init__(self, parent, title: str, *, width: int = 460) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(fg_color=theme.CARD_BG)
        self.resizable(False, False)
        self._owner = parent.winfo_toplevel()
        self._return_focus = _current_focus(parent)
        self._closed = False
        self.result = None
        self.initial_focus: tk.Misc | None = None
        self.primary: Callable[[], None] | None = None
        self.columnconfigure(0, weight=1, minsize=width)
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=0, column=0, sticky="nsew", padx=theme.SPACE_L, pady=(theme.SPACE_L, theme.SPACE_S))
        self.body.columnconfigure(0, weight=1)
        self.buttons = ctk.CTkFrame(self, fg_color="transparent")
        self.buttons.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_S, theme.SPACE_L))
        self.transient(self._owner)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _e: self.cancel(), add="+")
        self.bind("<Return>", self._on_return, add="+")
        self.bind("<KP_Enter>", self._on_return, add="+")

    def add_buttons(self, primary_text: str, primary: Callable[[], None], *, cancel_text: str = "Cancel",
                    danger: bool = False) -> tuple[AppButton, AppButton]:
        self.primary = primary
        cancel_button = AppButton(self.buttons, cancel_text, self.cancel, variant="secondary", width=110)
        primary_button = AppButton(self.buttons, primary_text, primary, variant="danger" if danger else "primary",
                                   width=130)
        primary_button.pack(side="right")
        cancel_button.pack(side="right", padx=(0, theme.SPACE_S))
        self.primary_button, self.cancel_button = primary_button, cancel_button
        return primary_button, cancel_button

    def present(self) -> "ModalDialog":
        """Show centered over the owner window, take the grab and the focus."""
        self.update_idletasks()
        owner = self._owner
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - self.winfo_reqwidth()) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:  # not viewable yet on some window managers; the dialog still works
            pass
        self._focus_initial()
        # The window manager focuses the new window itself once it is shown: pass that on to the right control.
        self.bind("<FocusIn>", lambda event: self._focus_initial() if event.widget is self else None, add="+")
        return self

    def _focus_initial(self) -> None:
        target = self.initial_focus or (focus_target(self.primary_button) if hasattr(self, "primary_button") else None)
        if target is not None and not self._closed and target.winfo_exists():
            target.focus_set()

    def _on_return(self, _event=None) -> str | None:
        if self.primary is not None:
            self.primary()
            return "break"
        return None

    def cancel(self) -> None:
        self.close(None)

    def close(self, result=None) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = result
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        _restore_focus(self._return_focus)

    def wait(self):
        """Block (running the event loop) until the dialog closes; returns its result."""
        self.wait_window(self)
        return self.result


class ConfirmDialog(ModalDialog):
    """Ask to confirm an action; on_result(True/False). The primary button names the action."""

    def __init__(self, parent, *, title: str, message: str, confirm_text: str = "Continue", danger: bool = False,
                 on_result: Callable[[bool], None] | None = None) -> None:
        super().__init__(parent, title)
        self._on_result = on_result
        ctk.CTkLabel(self.body, text=message, anchor="w", justify="left", wraplength=440, font=font(theme.SIZE_BODY),
                     text_color=theme.TEXT_PRIMARY).grid(row=0, column=0, sticky="ew")
        self.add_buttons(confirm_text, lambda: self.close(True), danger=danger)
        # A destructive action is not the default: Enter must not confirm it by accident.
        if danger:
            self.primary = None
            self.initial_focus = focus_target(self.cancel_button)

    def close(self, result=None) -> None:
        already = self._closed
        super().close(bool(result))
        if not already and self._on_result is not None:
            self._on_result(bool(result))


def ask_confirm(parent, *, title: str, message: str, confirm_text: str = "Continue", danger: bool = False) -> bool:
    """Blocking confirmation (like messagebox.askyesno, with the design and keyboard behavior above)."""
    return bool(ConfirmDialog(parent, title=title, message=message, confirm_text=confirm_text, danger=danger)
                .present().wait())


class ChoiceDialog(ModalDialog):
    """Pick one of several options (radio buttons), then Continue; on_choose(value) runs after it closes."""

    def __init__(self, parent, *, title: str, prompt: str, options: list[tuple[str, str]], note: str = "",
                 on_choose: Callable[[str], None], danger: bool = False) -> None:
        super().__init__(parent, title)
        self._on_choose = on_choose
        self.choice_var = tk.StringVar(value=options[0][0])
        ctk.CTkLabel(self.body, text=prompt, anchor="w", justify="left", wraplength=440, font=font(theme.SIZE_BODY),
                     text_color=theme.TEXT_PRIMARY).grid(row=0, column=0, sticky="ew", pady=(0, theme.SPACE_S))
        self.radios = []
        for row, (value, label) in enumerate(options, start=1):
            radio = ctk.CTkRadioButton(self.body, text=label, variable=self.choice_var, value=value,
                                       text_color=theme.TEXT_PRIMARY, fg_color=theme.ACCENT)
            radio.grid(row=row, column=0, sticky="w", pady=3)
            make_keyboard_accessible(radio, activate=radio.invoke, ring=False)
            self.radios.append(radio)
        if note:
            ctk.CTkLabel(self.body, text=note, text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=440,
                         font=font(theme.SIZE_SMALL)).grid(row=len(options) + 1, column=0, sticky="ew", pady=(8, 0))
        self.add_buttons("Continue...", self._choose, danger=danger)
        self.initial_focus = focus_target(self.radios[0])
        self.present()

    def _choose(self) -> None:
        choice = self.choice_var.get()
        self.close(choice)
        self._on_choose(choice)


class Drawer(Card):
    """
    A panel that slides over the right side of `container` (placed, so the
    page layout underneath is untouched). Escape or Close hides it; the focus
    moves into it on open and back on close.
    """

    def __init__(self, container, title: str, *, width: int = 380) -> None:
        super().__init__(container, corner_radius=theme.RADIUS_CARD, width=width)
        self._container = container
        self._width = width
        self._return_focus: tk.Misc | None = None
        self.is_open = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, theme.SPACE_S))
        header.columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=title, font=font(theme.SIZE_HEADING, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, sticky="ew")
        self.close_button = AppButton(header, "Close", self.close, variant="ghost", width=80, height=30)
        self.close_button.grid(row=0, column=1)
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=theme.SPACE_S, pady=(0, theme.SPACE_L))
        self.body.columnconfigure(0, weight=1)
        # One permanent handler (Tkinter's unbind(sequence, funcid) would drop every Escape binding of the window).
        self.winfo_toplevel().bind("<Escape>", self._on_escape, add="+")

    def _on_escape(self, _event=None) -> None:
        if self.is_open and self.winfo_exists():
            self.close()

    def open(self, focus: tk.Misc | None = None) -> None:
        if self.is_open:
            return
        self.is_open = True
        self._return_focus = _current_focus(self)
        self.place(relx=1.0, rely=0.0, anchor="ne", relheight=1.0, x=-theme.SPACE_S)
        self.lift()
        (focus or focus_target(self.close_button)).focus_set()

    def close(self) -> None:
        if not self.is_open:
            return
        self.is_open = False
        self.place_forget()
        _restore_focus(self._return_focus)
