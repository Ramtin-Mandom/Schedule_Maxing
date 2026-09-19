"""
duration_suggestion.py

A small widget shown under the task-entry form's duration field. On request,
it asks ProductivityController for a historical duration prediction and
shows the predicted minutes, its evidence level, and a plain-language
explanation of where the number came from -- never silently overwriting the
user's own entered duration. Filling the field only happens if the user
clicks "Use suggestion".
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

import customtkinter as ctk

from app.productivity.buckets import time_bucket_for_minutes
from app.productivity.prediction import DurationPrediction, FallbackLevel
from app.ui import theme
from app.ui.background import ControllerResult, run_in_background
from app.ui.productivity_controller import ProductivityController

_FALLBACK_SUMMARY = {
    FallbackLevel.CATEGORY_TIME_BUCKET: "matching category and time of day",
    FallbackLevel.CATEGORY: "matching category, any time of day",
    FallbackLevel.TIME_BUCKET: "this time of day, any category",
    FallbackLevel.GLOBAL: "your overall history",
    FallbackLevel.ORIGINAL_ESTIMATE: "your own estimate (not enough history)",
}


@dataclass(frozen=True)
class SuggestionContext:
    """What the duration-suggestion widget needs from the surrounding form to request a prediction."""

    category: str
    planned_start: int
    original_estimate_minutes: float


class DurationSuggestionWidget(ctk.CTkFrame):
    def __init__(
        self,
        parent: tk.Widget,
        productivity_controller: ProductivityController,
        *,
        get_context: Callable[[], SuggestionContext | None],
        apply_duration: Callable[[int], None],
    ) -> None:
        super().__init__(parent, fg_color="#F8FAFC", corner_radius=12)
        self._controller = productivity_controller
        self._get_context = get_context
        self._apply_duration = apply_duration
        self._last_prediction: DurationPrediction | None = None

        self.columnconfigure(0, weight=1)
        self._build()

    def _build(self) -> None:
        self.suggest_button = ctk.CTkButton(
            self,
            text="Suggest duration from history",
            height=30,
            fg_color="#E2E8F0",
            hover_color="#CBD5E1",
            text_color=theme.TEXT_PRIMARY,
            command=self._request_suggestion,
        )
        self.suggest_button.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        self.result_label = ctk.CTkLabel(
            self, text="", text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=260
        )
        self.result_label.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 4))

        self.use_button = ctk.CTkButton(
            self,
            text="Use suggestion",
            height=28,
            state="disabled",
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._use_suggestion,
        )
        self.use_button.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))

    def reset(self) -> None:
        """Clear any shown suggestion, e.g. after the form is submitted or cleared."""
        self._last_prediction = None
        self.result_label.configure(text="")
        self.use_button.configure(state="disabled")

    def _request_suggestion(self) -> None:
        context = self._get_context()
        if context is None:
            self.result_label.configure(text="Enter a category, start time, and duration first.")
            self.use_button.configure(state="disabled")
            return

        self.result_label.configure(text="Looking up your history...")
        self.use_button.configure(state="disabled")
        self.suggest_button.configure(state="disabled")

        time_bucket = time_bucket_for_minutes(context.planned_start)

        run_in_background(
            self,
            lambda: self._controller.predict_duration(
                category=context.category,
                time_bucket=time_bucket,
                original_estimate_minutes=context.original_estimate_minutes,
            ),
            self._on_prediction,
        )

    def _on_prediction(self, result: ControllerResult[DurationPrediction]) -> None:
        self.suggest_button.configure(state="normal")

        if not result.ok:
            self.result_label.configure(text=f"Could not load a suggestion: {result.error}")
            return

        prediction = result.value
        self._last_prediction = prediction

        if prediction.fallback_level == FallbackLevel.ORIGINAL_ESTIMATE:
            self.result_label.configure(text=f"Not enough history yet. {prediction.explanation}")
            self.use_button.configure(state="disabled")
            return

        basis = _FALLBACK_SUMMARY[prediction.fallback_level]
        self.result_label.configure(
            text=(
                f"Suggested: {prediction.predicted_duration_minutes:g} min "
                f"(based on {basis}, {prediction.sample_count} observation(s), "
                f"evidence: {prediction.evidence_level.value}).\n{prediction.explanation}"
            )
        )
        self.use_button.configure(state="normal")

    def _use_suggestion(self) -> None:
        if self._last_prediction is None:
            return
        # Explicit, user-initiated fill -- never happens automatically.
        self._apply_duration(round(self._last_prediction.predicted_duration_minutes))
