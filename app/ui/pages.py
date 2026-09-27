"""
app/ui/pages.py

Shell pages that are not schedule pages (Milestone 4 desktop):

- PlaceholderPage: a page whose real content arrives in a later desktop
  step. It says so plainly and links to what already works; it never shows
  controls that do nothing.
- SettingsPage: device appearance and interface size plus persisted user
  scheduling defaults through SettingsController and the shared editor.

Both only call the callbacks they are given.
"""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from app.persistence.errors import NotSignedInError
from app.ui import theme
from app.ui.background import run_io
from app.ui.components import AppButton, Card, LabeledSelect, Notice, SectionTitle, ask_confirm, font
from app.planning.preferences import ENGINE_LABELS
from app.ui.preferences_editor import PreferencesEditor
from app.ui.ui_settings import SCALES, LANGUAGES, UISettings

APPEARANCE_LABELS = {"light": "Light", "dark": "Dark"}


def scale_label(scale: float) -> str:
    return f"{round(scale * 100)}%"


class PageHeader(ctk.CTkFrame):
    def __init__(self, parent, title: str, subtitle: str = "") -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, font=font(theme.SIZE_TITLE, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, sticky="ew")
        if subtitle:
            ctk.CTkLabel(self, text=subtitle, font=font(theme.SIZE_BODY), text_color=theme.TEXT_MUTED, anchor="w",
                         justify="left", wraplength=520).grid(row=1, column=0, sticky="ew", pady=(2, 0))


class ScrollPage(ctk.CTkScrollableFrame):
    """
    Hosts a page whose content can be taller than a small window, so every
    control stays reachable by scrolling. build(parent) makes the content;
    on_show/on_appearance_changed are passed on to it.
    """

    def __init__(self, parent, build: Callable[[ctk.CTkFrame], ctk.CTkFrame]) -> None:
        super().__init__(parent, fg_color=theme.APP_BG, corner_radius=0)
        self.columnconfigure(0, weight=1)
        self.content = build(self)
        self.content.grid(row=0, column=0, sticky="nsew")

    def on_show(self) -> None:
        if hasattr(self.content, "on_show"):
            self.content.on_show()

    def on_appearance_changed(self) -> None:
        if hasattr(self.content, "on_appearance_changed"):
            self.content.on_appearance_changed()


class PlaceholderPage(ctk.CTkFrame):
    def __init__(self, parent, title: str, message: str, links: list[tuple[str, Callable[[], None]]] = ()) -> None:
        super().__init__(parent, fg_color=theme.APP_BG, corner_radius=0)
        self.columnconfigure(0, weight=1)
        self.title = title
        PageHeader(self, title).grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(22, 12))
        card = Card(self)
        card.grid(row=1, column=0, sticky="new", padx=theme.SPACE_XL)
        card.columnconfigure(0, weight=1)
        self.message_label = ctk.CTkLabel(card, text=message, font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY,
                                          anchor="w", justify="left", wraplength=460)
        self.message_label.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, theme.SPACE_M))
        self.link_buttons = []
        if links:
            bar = ctk.CTkFrame(card, fg_color="transparent")
            bar.grid(row=1, column=0, sticky="w", padx=theme.SPACE_L, pady=(0, theme.SPACE_L))
            for column, (label, command) in enumerate(links):
                button = AppButton(bar, label, command, variant="secondary")
                button.grid(row=0, column=column, padx=(0, theme.SPACE_S))
                self.link_buttons.append(button)

    def set_message(self, message: str) -> None:
        self.message_label.configure(text=message)


class SettingsPage(ctk.CTkFrame):
    def __init__(
        self,
        parent,
        settings: UISettings,
        *,
        on_appearance: Callable[[str], bool],
        on_scale: Callable[[float], bool],
        controller=None,
        background_io: bool = False,
    ) -> None:
        super().__init__(parent, fg_color=theme.APP_BG, corner_radius=0)
        #: Direct PostgreSQL storage: the defaults are read and saved in workers (app/ui/background.run_io).
        self.background_io = background_io
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._on_appearance = on_appearance
        self._on_scale = on_scale
        self.controller = controller
        self.view = None
        PageHeader(self, "Settings", "Appearance on this device and scheduling defaults for your workspace."
                   ).grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(22, 12))

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=theme.SPACE_M, pady=(0, theme.SPACE_L))
        body.columnconfigure(0, weight=1)

        appearance = Card(body)
        appearance.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_S, pady=(0, theme.SPACE_M))
        appearance.columnconfigure((0, 1), weight=1, uniform="settings")
        SectionTitle(appearance, "Appearance", "Saved on this device only; it never changes your schedule data."
                     ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 8))
        self.appearance_select = LabeledSelect(
            appearance, "Theme", list(APPEARANCE_LABELS.values()),
            command=lambda label: self._appearance_chosen(label),
        )
        self.appearance_select.variable.set(APPEARANCE_LABELS[settings.appearance])
        self.appearance_select.grid(row=1, column=0, sticky="ew", padx=(theme.SPACE_L, 8), pady=(0, theme.SPACE_L))
        self.scale_select = LabeledSelect(
            appearance, "Interface size", [scale_label(scale) for scale in SCALES],
            command=lambda label: self._scale_chosen(label),
        )
        self.scale_select.variable.set(scale_label(settings.ui_scale))
        self.scale_select.grid(row=1, column=1, sticky="ew", padx=(8, theme.SPACE_L), pady=(0, theme.SPACE_L))
        self.notice = Notice(appearance, wraplength=420)
        self.notice.grid(row=2, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=(0, theme.SPACE_L))
        self.notice.hide()

        self.language_select = LabeledSelect(appearance, "Language", list(LANGUAGES.values()))
        self.language_select.grid(row=3, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=(0, 12))

        defaults = Card(body)
        defaults.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_S, pady=(0, theme.SPACE_M))
        defaults.columnconfigure(0, weight=1)
        SectionTitle(defaults, "Default scheduling preferences",
                     "Applies to inheriting dates. Date overrides stay unchanged; nothing is scheduled automatically.",
                     wraplength=440).grid(row=0, column=0, sticky="ew", padx=16, pady=12)
        self.engine_select = LabeledSelect(defaults, "Default engine", list(ENGINE_LABELS.values()),
                                           command=self.choose_engine)
        self.engine_select.grid(row=1, column=0, sticky="ew", padx=16)
        self.engine_note = ctk.CTkLabel(defaults, text="", wraplength=450, justify="left", anchor="w",
                                       text_color=theme.TEXT_MUTED, font=font(theme.SIZE_SMALL))
        self.engine_note.grid(row=2, column=0, sticky="ew", padx=16)
        AppButton(defaults, "Use inherited engine", lambda: self.choose_engine(None), variant="ghost").grid(
            row=3, column=0, sticky="w", padx=16, pady=6)
        self.preference_notice = Notice(defaults, wraplength=450)
        self.preference_notice.grid(row=4, column=0, sticky="ew", padx=16)
        self.preference_notice.hide()
        self.editor = PreferencesEditor(defaults, on_save=lambda key, value: self.change(key, "save", value),
                                        on_inherit=lambda key: self.change(key, "inherit"),
                                        on_clear=lambda key: self.change(key, "clear"), height=460)
        self.editor.grid(row=5, column=0, sticky="ew", padx=8)
        self.reset_button = AppButton(defaults, "Reset default overrides…", self.reset_defaults, variant="danger")
        self.reset_button.grid(row=6, column=0, sticky="w", padx=16, pady=12)

    def _io(self, work, done):
        run_io(self, work, done, background=self.background_io)

    def on_show(self):
        if self.controller is not None:
            self._io(self.controller.load, self._loaded)

    def _loaded(self, result):
        if result.ok:
            self.render_defaults(result.value)
        elif not isinstance(result.cause, NotSignedInError):
            self.preference_notice.show("error", result.error)

    def render_defaults(self, view):
        self.view = view
        self.engine_select.variable.set(ENGINE_LABELS[view.engine])
        source = "set in your defaults" if view.layer.optimizer_mode is not None else "inherited from the app template"
        self.engine_note.configure(text=f"{ENGINE_LABELS[view.engine]} — {source}. Timezone: {view.timezone}. "
                                  "Timezone is configured at startup. Only active engine settings are shown.")
        self.editor.render(view.rows)
        self.reset_button.configure(state="normal" if view.version is not None else "disabled")

    def apply_result(self, result, key=None):
        if result.ok:
            self.render_defaults(result.value)
            self.preference_notice.show("success", "Defaults saved. Date overrides were kept; no schedule was generated.")
        else:
            pending = {name: self.editor.value_of(name) for name in self.editor.inputs}

            def reloaded(latest):
                if latest.ok:
                    self.render_defaults(latest.value)
                    for name, value in pending.items():
                        if name in self.editor.inputs:
                            self.editor.set_input(name, value)
                if key is not None:
                    self.editor.show_error(key, result.error)
                self.preference_notice.show("error", f"{result.error} Your typed edits are kept; review before saving "
                                                     "again.")

            self._io(self.controller.load, reloaded)

    def change(self, key, action, value=None):
        if self.view is not None:
            view = self.view
            self._io(lambda: self.controller.change(view, key, action, value), lambda result: self.apply_result(
                result, key))

    def choose_engine(self, label):
        if self.view is not None:
            view = self.view
            mode = next((key for key, value in ENGINE_LABELS.items() if value == label), None)
            self._io(lambda: self.controller.set_engine(view, mode), self.apply_result)

    def reset_defaults(self):
        if self.view is not None and ask_confirm(self, title="Reset defaults?",
                message="Remove your default overrides and inherit app defaults? Date overrides and history stay.",
                confirm_text="Reset defaults", danger=True):
            view = self.view
            self._io(lambda: self.controller.reset(view), self.apply_result)

    def _appearance_chosen(self, label: str) -> None:
        mode = next(key for key, value in APPEARANCE_LABELS.items() if value == label)
        saved = self._on_appearance(mode)
        self._report(saved, f"{label} theme")

    def _scale_chosen(self, label: str) -> None:
        scale = next(value for value in SCALES if scale_label(value) == label)
        saved = self._on_scale(scale)
        self._report(saved, f"Interface size {label}")

    def _report(self, saved: bool, what: str) -> None:
        if saved:
            self.notice.show("success", f"{what} applied and saved for next time.")
        else:
            self.notice.show("warning", f"{what} applied, but it could not be saved (the settings file is not "
                                        "writable); it lasts until you close the app.")
