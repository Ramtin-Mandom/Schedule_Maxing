"""
app/ui/preferences_editor.py

Native preference controls (Milestone 4, Prompt 4) -- not a YAML editor.
PreferencesEditor shows one row per field of app/ui/preferences_model.py:
the value scheduling uses, where it comes from (set for this layer,
inherited, or explicitly cleared), an input pre-filled with it, and the
actions Save, Use inherited and -- for per-category fields -- No
preference. It only calls back; saving goes through the page's presenter.
DayPreferencesDialog hosts it for one date; the later Settings page hosts
the same editor for the default (user) layer.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from app.planning.errors import PlanningError
from app.ui import theme
from app.ui.background import run_io
from app.ui.components import AppButton, LabeledEntry, ModalDialog, Notice, font
from app.ui.preferences_model import FieldInput, PreferenceRow

_STATE_WORDS = {"set": "set here", "inherited": "inherited", "cleared": "cleared here"}


class PreferencesEditor(ctk.CTkScrollableFrame):
    """The field rows; on_save(key, value), on_inherit(key) and on_clear(key) do the work."""

    def __init__(self, parent, *, on_save: Callable[[str, FieldInput], None], on_inherit: Callable[[str], None],
                 on_clear: Callable[[str], None], height: int = 420) -> None:
        super().__init__(parent, fg_color="transparent", height=height)
        self.columnconfigure(0, weight=1)
        self._on_save, self._on_inherit, self._on_clear = on_save, on_inherit, on_clear
        self.rows: dict[str, PreferenceRow] = {}
        self.inputs: dict[str, tuple[LabeledEntry, ...]] = {}
        self.buttons: dict[str, dict[str, AppButton]] = {}
        self.error_labels: dict[str, ctk.CTkLabel] = {}
        self.value_labels: dict[str, ctk.CTkLabel] = {}

    def render(self, rows: list[PreferenceRow]) -> None:
        for child in self.winfo_children():
            child.destroy()
        self.rows = {row.spec.key: row for row in rows}
        self.inputs.clear()
        self.buttons.clear()
        self.error_labels.clear()
        self.value_labels.clear()
        grid_row, group = 0, None
        for row in rows:
            if row.spec.group != group:
                group = row.spec.group
                ctk.CTkLabel(self, text=group, font=font(theme.SIZE_BODY, "bold"), text_color=theme.TEXT_PRIMARY,
                             anchor="w").grid(row=grid_row, column=0, sticky="ew", padx=4, pady=(12, 2))
                grid_row += 1
            self._row(row).grid(row=grid_row, column=0, sticky="ew", padx=2, pady=3)
            grid_row += 1

    def _row(self, row: PreferenceRow) -> ctk.CTkFrame:
        key, spec = row.spec.key, row.spec
        frame = ctk.CTkFrame(self, fg_color=theme.SUBTLE_BG, corner_radius=theme.RADIUS_CONTROL)
        frame.columnconfigure(0, weight=1)
        ctk.CTkLabel(frame, text=spec.label, font=font(theme.SIZE_SMALL, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 0))
        ctk.CTkLabel(frame, text=spec.help, font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED, anchor="w",
                     justify="left", wraplength=420).grid(row=1, column=0, columnspan=2, sticky="ew", padx=10)
        state = _STATE_WORDS[row.state]
        value = ctk.CTkLabel(frame, text=f"Uses: {row.effective_text} — {row.source} ({state})",
                             font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_PRIMARY, anchor="w", justify="left",
                             wraplength=420)
        value.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(2, 0))
        self.value_labels[key] = value
        if row.state != "inherited":
            ctk.CTkLabel(frame, text=f"Without this override: {row.inherited_text}", font=font(theme.SIZE_CAPTION),
                         text_color=theme.TEXT_MUTED, anchor="w").grid(row=3, column=0, columnspan=2, sticky="ew",
                                                                      padx=10)

        inputs = ctk.CTkFrame(frame, fg_color="transparent")
        inputs.grid(row=4, column=0, columnspan=2, sticky="ew", padx=6, pady=(2, 6))
        inputs.columnconfigure((0, 1), weight=1)
        if isinstance(row.edit, tuple):
            start = LabeledEntry(inputs, "From", tk.StringVar(value=row.edit[0]), placeholder="9:00 AM")
            end = LabeledEntry(inputs, "Until", tk.StringVar(value=row.edit[1]), placeholder="5:00 PM")
            start.grid(row=0, column=0, sticky="ew", padx=4)
            end.grid(row=0, column=1, sticky="ew", padx=4)
            self.inputs[key] = (start, end)
        else:
            entry = LabeledEntry(inputs, "Value" + (" (minutes)" if spec.kind == "minutes" else ""),
                                 tk.StringVar(value=row.edit))
            entry.grid(row=0, column=0, sticky="ew", padx=4)
            self.inputs[key] = (entry,)

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 8))
        buttons = {"save": AppButton(actions, "Save", lambda: self.save(key), height=30, width=70)}
        buttons["inherit"] = AppButton(actions, "Use inherited", lambda: self._on_inherit(key), variant="ghost",
                                       height=30, width=110)
        if row.state == "inherited":
            buttons["inherit"].configure(state="disabled")
        if spec.clearable:
            buttons["clear"] = AppButton(actions, "No preference", lambda: self._on_clear(key), variant="ghost",
                                         height=30, width=110)
            if row.state == "cleared":
                buttons["clear"].configure(state="disabled")
        for column, button in enumerate(buttons.values()):
            button.grid(row=0, column=column, padx=(4, 0))
        self.buttons[key] = buttons
        error = ctk.CTkLabel(frame, text="", font=font(theme.SIZE_CAPTION), text_color=theme.DANGER, anchor="w",
                             justify="left", wraplength=520)
        error.grid(row=6, column=0, columnspan=2, sticky="ew", padx=10)
        self.error_labels[key] = error
        for field in self.inputs[key]:
            field.entry.bind("<Return>", lambda _e: (self.save(key), "break")[1], add="+")
        return frame

    def value_of(self, key: str) -> FieldInput:
        fields = self.inputs[key]
        return (fields[0].get(), fields[1].get()) if len(fields) == 2 else fields[0].get()

    def set_input(self, key: str, value: FieldInput) -> None:
        fields = self.inputs[key]
        values = value if isinstance(value, tuple) else (value,)
        for field, text in zip(fields, values):
            field.variable.set(text)

    def save(self, key: str) -> None:
        self._on_save(key, self.value_of(key))

    def show_error(self, key: str, message: str) -> None:
        if key in self.error_labels:
            self.error_labels[key].configure(text=f"Error: {message}")


class DayPreferencesDialog(ModalDialog):
    """
    One date's preferences. Every action saves immediately through the
    presenter (DayScheduleController) with the version it last read; the
    page re-reads itself when the dialog closes (freshness may change).
    """

    def __init__(self, parent, controller, *, on_closed: Callable[[], None], background_io: bool = False) -> None:
        super().__init__(parent, "Day Preferences", width=640)
        self._controller = controller
        self._background_io = background_io
        self._on_closed = on_closed
        self.view = None
        self.header = ctk.CTkLabel(self.body, text="", anchor="w", justify="left", wraplength=600,
                                   font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY)
        self.header.grid(row=0, column=0, sticky="ew")
        self.engine_label = ctk.CTkLabel(self.body, text="", anchor="w", justify="left", wraplength=600,
                                         font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED)
        self.engine_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.editor = PreferencesEditor(self.body, on_save=self.save, on_inherit=self.inherit, on_clear=self.clear)
        self.editor.grid(row=2, column=0, sticky="nsew", pady=(8, 4))
        self.notice = Notice(self.body, wraplength=560)
        self.notice.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.notice.hide()
        self.reset_button = AppButton(self.buttons, "Reset this date...", self.reset_all, variant="danger", width=150)
        self.reset_button.pack(side="left")
        self.add_buttons("Done", self.close, cancel_text="Close")
        self.refresh()

    def _io(self, work, done) -> None:
        """A storage call: at once locally, in a worker with direct PostgreSQL storage (app/ui/background.run_io)."""
        run_io(self, work, done, background=self._background_io, still_current=lambda: not self._closed)

    def refresh(self, then: Callable[[], None] | None = None) -> None:
        def done(result) -> None:
            if not result.ok:
                self.notice.show("error", result.error or "The preferences could not be read.")
                return
            self._show(result.value)
            if then is not None:
                then()

        self._io(self._controller.preferences, done)

    def _show(self, view) -> None:
        self.view = view
        self.header.configure(text=(
            f"Preferences for {view.day:%A, %B} {view.day.day} (times in {view.timezone}). A value set here applies "
            "to this date only; everything else is inherited from your defaults."))
        self.engine_label.configure(text=(
            f"Engine: {view.engine.summary}. Change it with the Engine choice next to Make Schedule. Only settings "
            "the current engine uses are shown."))
        self.editor.render(view.rows)
        self.reset_button.configure(state="normal" if view.layer_version is not None else "disabled")

    def _apply(self, result, key: str | None, done: str) -> None:
        if result.ok:
            self._show(result.value)
            self.notice.show("success", done)
            return
        if key is not None and result.cause is not None and not isinstance(result.cause, PlanningError):
            self.editor.show_error(key, result.error or "Not saved.")
            self.notice.show("error", f"Not saved: {result.error}")
            return
        pending = {name: self.editor.value_of(name) for name in self.editor.inputs}

        def restore() -> None:
            for name, value in pending.items():
                if name in self.editor.inputs:
                    self.editor.set_input(name, value)
            self.notice.show("error", f"{result.error} Saved values were refreshed; your typed edits are kept for "
                                      "review.")

        self.refresh(then=restore)

    def _label(self, key: str) -> str:
        return self.editor.rows[key].spec.label if key in self.editor.rows else key

    def save(self, key: str, value: FieldInput) -> None:
        version = self.view.layer_version
        self._io(lambda: self._controller.save_preference(key, value, expected_version=version),
                 lambda result: self._apply(result, key, f"{self._label(key)} saved for this date."))

    def inherit(self, key: str) -> None:
        version = self.view.layer_version
        self._io(lambda: self._controller.inherit_preference(key, expected_version=version),
                 lambda result: self._apply(result, key, f"{self._label(key)} now uses the inherited value."))

    def clear(self, key: str) -> None:
        version = self.view.layer_version
        self._io(lambda: self._controller.clear_preference(key, expected_version=version),
                 lambda result: self._apply(result, key, f"{self._label(key)}: no preference for this date."))

    def reset_all(self) -> None:
        version = self.view.layer_version
        self._io(lambda: self._controller.reset_date_preferences(expected_version=version),
                 lambda result: self._apply(result, None, "This date's own preferences and engine were removed; it "
                                                          "inherits your defaults."))

    def close(self, result=None) -> None:
        already = self._closed
        super().close(result)
        if not already:
            self._on_closed()
