"""
feedback_dialog.py

A small modal for the optional feedback (focus rating, energy rating,
interruption count, note) collected when completing or skipping a task.
Every field is optional -- "Skip" submits with nothing filled in, matching
ExecutionController.record_feedback's "only provided fields are changed"
contract (app/execution/service.py).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox

import customtkinter as ctk

from app.ui import theme

RATING_OPTIONS = ["(none)", "1", "2", "3", "4", "5"]


class FeedbackDialog(ctk.CTkToplevel):
    """Modal dialog collecting optional post-task feedback."""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        title: str,
        on_submit: Callable[[int | None, int | None, int | None, str | None], None],
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.geometry("360x420")
        self.resizable(False, False)
        self.configure(fg_color=theme.APP_BG)
        self.transient(parent)
        self.grab_set()

        self._on_submit = on_submit

        self.focus_var = tk.StringVar(value=RATING_OPTIONS[0])
        self.energy_var = tk.StringVar(value=RATING_OPTIONS[0])
        self.interruptions_var = tk.StringVar(value="")
        self.note_var = tk.StringVar(value="")

        self._build()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text="Optional feedback",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=theme.TEXT_PRIMARY,
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(18, 4))

        ctk.CTkLabel(
            self,
            text="All fields are optional. This stays on your device.",
            font=ctk.CTkFont(size=11),
            text_color=theme.TEXT_MUTED,
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 12))

        self._add_option_row(2, "Focus (1-5)", self.focus_var)
        self._add_option_row(3, "Energy (1-5)", self.energy_var)
        self._add_entry_row(4, "Interruptions", self.interruptions_var, "0")
        self._add_entry_row(5, "Note", self.note_var, "Optional short note")

        button_bar = ctk.CTkFrame(self, fg_color="transparent")
        button_bar.grid(row=6, column=0, sticky="ew", padx=18, pady=(20, 18))
        button_bar.columnconfigure((0, 1), weight=1)

        ctk.CTkButton(
            button_bar,
            text="Skip",
            fg_color="#E2E8F0",
            hover_color="#CBD5E1",
            text_color=theme.TEXT_PRIMARY,
            command=self._skip,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            button_bar,
            text="Submit",
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._submit,
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _add_option_row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ctk.CTkLabel(self, text=label, text_color=theme.TEXT_MUTED, anchor="w").grid(
            row=row, column=0, sticky="ew", padx=18, pady=(4, 0)
        )
        ctk.CTkOptionMenu(self, variable=variable, values=RATING_OPTIONS).grid(
            row=row, column=0, sticky="e", padx=18, pady=(0, 6)
        )

    def _add_entry_row(self, row: int, label: str, variable: tk.StringVar, placeholder: str) -> None:
        ctk.CTkLabel(self, text=label, text_color=theme.TEXT_MUTED, anchor="w").grid(
            row=row, column=0, sticky="ew", padx=18, pady=(4, 0)
        )
        ctk.CTkEntry(self, textvariable=variable, placeholder_text=placeholder).grid(
            row=row, column=0, sticky="ew", padx=18, pady=(0, 6)
        )

    def _skip(self) -> None:
        self._on_submit(None, None, None, None)
        self.destroy()

    def _submit(self) -> None:
        try:
            focus_rating = self._parse_rating(self.focus_var.get())
            energy_rating = self._parse_rating(self.energy_var.get())
            interruption_count = self._parse_interruptions(self.interruptions_var.get())
        except ValueError as error:
            messagebox.showerror("Invalid Feedback", str(error), parent=self)
            return

        note = self.note_var.get().strip() or None
        self._on_submit(focus_rating, energy_rating, interruption_count, note)
        self.destroy()

    def _parse_rating(self, raw_value: str) -> int | None:
        if raw_value == RATING_OPTIONS[0]:
            return None
        return int(raw_value)

    def _parse_interruptions(self, raw_value: str) -> int | None:
        raw_value = raw_value.strip()
        if not raw_value:
            return None
        try:
            value = int(raw_value)
        except ValueError as error:
            raise ValueError("Interruptions must be a whole number.") from error
        if value < 0:
            raise ValueError("Interruptions cannot be negative.")
        return value
