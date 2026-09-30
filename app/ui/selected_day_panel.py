"""
app/ui/selected_day_panel.py

Widgets of the Week and Month pages' right side (replacing the former
"Tasks this week/month" list):

- SelectedDayPanel: the selected date's scheduled tasks and where each
  stands (Completed / Uncompleted / Tasks), its counts, planned hours and
  points, its historical status in words (past dates), and the two bulk
  actions "All Tasks Complete" and "No Tasks Complete". Widgets only: the
  page runs app/ui/day_outcomes.DayOutcomeController and passes the result.
- DayStatusLegend: what each historical colour means, in words.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import customtkinter as ctk

from app.execution.lifecycle import TaskOutcome
from app.productivity.day_summary import DAY_STATUS_TEXT, DayStatusClass
from app.ui import theme
from app.ui.components import AppButton, Card, Notice, SectionTitle, font
from app.ui.day_outcomes import DayDetail
from app.ui.time_fields import format_duration

_OUTCOME_WORDS = {TaskOutcome.COMPLETED: "Completed", TaskOutcome.UNCOMPLETED: "Uncompleted",
                  TaskOutcome.PENDING: "Tasks (pending)"}
_OUTCOME_MARKS = {TaskOutcome.COMPLETED: "✓", TaskOutcome.UNCOMPLETED: "✗", TaskOutcome.PENDING: "•"}
#: Rows listed before "+N more".
MAX_ROWS = 12


class SelectedDayPanel(Card):
    def __init__(self, parent, *, on_all_complete: Callable[[], None], on_none_complete: Callable[[], None],
                 wraplength: int = 420) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.detail: DayDetail | None = None
        self._rows: list[ctk.CTkLabel] = []
        self.title = SectionTitle(self, "Selected day", "The scheduled tasks of the day you selected, and what "
                                                        "happened to them.", wraplength=wraplength)
        self.title.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 4))
        self.day_label = ctk.CTkLabel(self, text="", font=font(theme.SIZE_HEADING, "bold"),
                                      text_color=theme.TEXT_PRIMARY, anchor="w")
        self.day_label.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        self.status_label = ctk.CTkLabel(self, text="", font=font(theme.SIZE_SMALL, "bold"), anchor="w",
                                         justify="left", corner_radius=10, padx=10, wraplength=wraplength)
        self.status_label.grid(row=2, column=0, sticky="w", padx=theme.SPACE_L, pady=(4, 0))
        self.stats_label = ctk.CTkLabel(self, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_PRIMARY,
                                        anchor="w", justify="left", wraplength=wraplength)
        self.stats_label.grid(row=3, column=0, sticky="ew", padx=theme.SPACE_L, pady=(6, 0))
        self.list_frame = ctk.CTkFrame(self, fg_color=theme.SUBTLE_BG, corner_radius=theme.RADIUS_CONTROL)
        self.list_frame.grid(row=4, column=0, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.list_frame.columnconfigure(0, weight=1)
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="ew", padx=theme.SPACE_L, pady=(10, 0))
        buttons.columnconfigure((0, 1), weight=1, uniform="bulk")
        self.all_button = AppButton(buttons, "All Tasks Complete", on_all_complete)
        self.all_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.none_button = AppButton(buttons, "No Tasks Complete", on_none_complete, variant="secondary")
        self.none_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.hint = ctk.CTkLabel(self, text="Marks every scheduled task of this day at once; tasks the scheduler "
                                           "could not place are not affected. Single tasks: Open Day.",
                                 font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED, anchor="w",
                                 justify="left", wraplength=wraplength)
        self.hint.grid(row=6, column=0, sticky="ew", padx=theme.SPACE_L, pady=(4, 0))
        self.notice = Notice(self, wraplength=wraplength)
        self.notice.grid(row=7, column=0, sticky="ew", padx=theme.SPACE_L, pady=(6, 0))
        self.notice.hide()
        ctk.CTkFrame(self, fg_color="transparent", height=theme.SPACE_M).grid(row=8, column=0)
        self._busy = False
        self.set_enabled(False)

    def set_wraplength(self, width: int) -> None:
        for label in (self.status_label, self.stats_label, self.hint):
            label.configure(wraplength=width)

    def show_loading(self, day: date) -> None:
        self.day_label.configure(text=f"{day:%A}, {day:%B} {day.day}, {day.year}")
        self.stats_label.configure(text="Loading...")

    def render(self, detail: DayDetail, *, today: date) -> None:
        self.detail = detail
        day, summary = detail.day, detail.summary
        self.day_label.configure(text=f"{day:%A}, {day:%B} {day.day}, {day.year}")
        if day < today:
            status = summary.status_class
            colour, meaning = DAY_STATUS_TEXT[status]
            self.status_label.configure(text=f"{colour}: {meaning}", fg_color=theme.DAY_STATUS_FILLS[status.value],
                                        text_color=theme.TEXT_PRIMARY)
        else:
            self.status_label.configure(text="Today" if day == today else "Upcoming",
                                        fg_color=theme.SECONDARY_BG, text_color=theme.TEXT_PRIMARY)
        if summary.scheduled_count:
            self.stats_label.configure(text=(
                f"{summary.scheduled_count} scheduled · {summary.completed_count} completed · "
                f"{summary.uncompleted_count} uncompleted · {summary.pending_count} pending\n"
                f"Planned time {format_duration(summary.scheduled_minutes)}, completed "
                f"{_minutes(summary.completed_minutes)} · Points {summary.points_completed} of "
                f"{summary.points_scheduled} completed"))
        else:
            self.stats_label.configure(text="No scheduled tasks on this day. Open Day and Make Schedule to plan it.")
        for row in self._rows:
            row.destroy()
        self._rows = []
        cards = detail.board.cards
        for index, card in enumerate(cards[:MAX_ROWS]):
            text = f"{_OUTCOME_MARKS[card.outcome]}  {card.time_text}  {card.name} — {_OUTCOME_WORDS[card.outcome]}"
            row = ctk.CTkLabel(self.list_frame, text=text, font=font(theme.SIZE_SMALL), text_color=theme.TEXT_PRIMARY,
                               anchor="w", justify="left")
            row.grid(row=index, column=0, sticky="ew", padx=10, pady=(6 if index == 0 else 1, 1))
            self._rows.append(row)
        if len(cards) > MAX_ROWS:
            more = ctk.CTkLabel(self.list_frame, text=f"+{len(cards) - MAX_ROWS} more (Open Day)",
                                font=font(theme.SIZE_SMALL, "bold"), text_color=theme.TEXT_MUTED, anchor="w")
            more.grid(row=MAX_ROWS, column=0, sticky="ew", padx=10, pady=(1, 6))
            self._rows.append(more)
        if cards:
            self.list_frame.grid()
        else:
            self.list_frame.grid_remove()
        self.set_enabled(summary.scheduled_count > 0)

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled and not self._busy else "disabled"
        self.all_button.configure(state=state)
        self.none_button.configure(state=state)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.set_enabled(self.detail is not None and self.detail.summary.scheduled_count > 0)


def _minutes(minutes: int) -> str:
    return format_duration(minutes) if minutes else "0 min"


class DayStatusLegend(ctk.CTkFrame):
    """The historical colours of past days and their meaning, in words (the colour is never the only cue)."""

    def __init__(self, parent) -> None:
        super().__init__(parent, fg_color="transparent")
        ctk.CTkLabel(self, text="Past days:", font=font(theme.SIZE_SMALL, "bold"), text_color=theme.TEXT_MUTED
                     ).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        self.entries: dict[DayStatusClass, ctk.CTkLabel] = {}
        for index, status in enumerate(DayStatusClass, start=1):
            _, meaning = DAY_STATUS_TEXT[status]
            swatch = ctk.CTkFrame(self, width=14, height=14, corner_radius=4, fg_color=theme.DAY_STATUS_FILLS[status.value],
                                  border_width=1, border_color=theme.CARD_BORDER)
            label = ctk.CTkLabel(self, text=meaning, font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_PRIMARY)
            row, column = divmod(index - 1, 4)
            swatch.grid(row=row, column=1 + column * 2, padx=(8, 4), pady=2)
            label.grid(row=row, column=2 + column * 2, sticky="w", pady=2)
            self.entries[status] = label
