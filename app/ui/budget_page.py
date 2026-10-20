"""
app/ui/budget_page.py

The desktop Budgeting page. A section bar chooses General, Stats or Future.

General:
    - the current balance, editable (saved as today's balance; every change is
      kept and listed in Stats > History);
    - Income on the left, Expenses on the right (stacked on narrow windows):
      each with a form (name, amount, date, category, optional project, and
      a "Repeats" box that reveals Daily / Weekly / Every 2 weeks / Monthly)
      that saves to the date typed, and the saved entries below it with Edit
      and Delete. Editing or deleting a recurring entry asks which it means:
      the entire series, or only the occurrence on one date.

Stats (app/budget/stats.py, worded by BudgetController.stats_view):
    - the highest income day and the highest expense day;
    - expenses per week (up to the last 20 weeks), per month (the whole
      history) and per day (up to the last 30 days), as bar charts -- only
      from the first entry on, never padded with invented days;
    - the 90-day balance forecast: when the projected balance first reaches
      zero or below (or that it stays above zero), a line chart, and the
      assumptions it makes, in words;
    - History: every transaction up to today and every manual balance change,
      newest first, with the balance after each, filtered by period, type,
      category and project (BudgetController.history).

Future (app/ui/budget_future.py): income ideas with their progress, and the
wishlist with "Turn into expense".

Stats and Future are built the first time they are shown, so opening the
app lays out only General.

Nothing is calculated or validated here: every action is one
BudgetController call (app/ui/budget_controller.py) run off the Tk thread
(app/ui/background.run_io; writes in the serial write lane), and the page
redraws from what it returns. Errors are shown as text, never raised into Tk.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from collections.abc import Callable

import customtkinter as ctk

from app.budget.models import BudgetKind
from app.ui import theme
from app.budget.stats import DAYS, HORIZON_DAYS, WEEKS
from app.ui.background import WRITE_LANE, ControllerResult, run_io
from app.ui.budget_charts import BudgetBarChart, ForecastChart
from app.ui.budget_future import FutureSection
from app.ui.budget_controller import (
    NO_PROJECT,
    OCCURRENCE,
    REPEAT_CHOICES,
    REPEAT_LABELS,
    SERIES,
    BudgetController,
    ANY,
    HISTORY_PERIODS,
    HISTORY_TYPES,
    BudgetOverview,
    EntryForm,
    EntryRow,
    HistoryView,
    StatsView,
    categories,
    money_text,
)
from app.ui.components import AppButton, Card, ConfirmDialog, LabeledEntry, LabeledSelect, ModalDialog, font
from app.ui.paint_widgets import AppScrollableFrame

GENERAL, STATS, FUTURE = "General", "Stats", "Future"
SECTIONS = (GENERAL, STATS, FUTURE)
#: Income is green, expenses are red (theme.py; the Day page's money widgets use them without this module).
INCOME_COLOR, EXPENSE_COLOR = theme.INCOME_COLOR, theme.EXPENSE_COLOR
KIND_COLORS = {BudgetKind.INCOME: INCOME_COLOR, BudgetKind.EXPENSE: EXPENSE_COLOR}
#: Below this page width (logical px) Income and Expenses are stacked instead of side by side.
STACK_BELOW = 760
_LIST_HEIGHT = 230
_NAME_CHARS = 34


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


class ScopeDialog(ModalDialog):
    """Which part of a recurring entry an action is for: the occurrence on a date, or the entire series."""

    def __init__(self, parent, *, title: str, prompt: str, default_date: str, action: str,
                 on_done: Callable[[str, str], None], danger: bool = False) -> None:
        super().__init__(parent, title)
        self._on_done = on_done
        self.scope_var = tk.StringVar(value=OCCURRENCE)
        ctk.CTkLabel(self.body, text=prompt, anchor="w", justify="left", wraplength=420,
                     text_color=theme.TEXT_PRIMARY).grid(row=0, column=0, sticky="ew", pady=(0, theme.SPACE_S))
        self.occurrence_radio = ctk.CTkRadioButton(self.body, text="Only this occurrence, on the date below",
                                                   variable=self.scope_var, value=OCCURRENCE, fg_color=theme.ACCENT,
                                                   text_color=theme.TEXT_PRIMARY)
        self.occurrence_radio.grid(row=1, column=0, sticky="w", pady=3)
        self.date_field = LabeledEntry(self.body, "Occurrence date (YYYY-MM-DD)", tk.StringVar(value=default_date))
        self.date_field.grid(row=2, column=0, sticky="ew", padx=(28, 0), pady=(0, 6))
        self.series_radio = ctk.CTkRadioButton(self.body, text="The entire series (every occurrence)",
                                               variable=self.scope_var, value=SERIES, fg_color=theme.ACCENT,
                                               text_color=theme.TEXT_PRIMARY)
        self.series_radio.grid(row=3, column=0, sticky="w", pady=3)
        self.add_buttons(action, self._done, danger=danger)
        self.present()

    def _done(self) -> None:
        scope, day = self.scope_var.get(), self.date_field.get()
        self.close((scope, day))
        self._on_done(scope, day)


class EntryPanel(Card):
    """One side of General: the income (or expense) form and the saved entries of that kind."""

    def __init__(self, parent, kind: BudgetKind, *, today: Callable[[], str], on_save: Callable[[EntryForm], None],
                 on_delete: Callable[[EntryRow], None]) -> None:
        super().__init__(parent)
        self.kind = kind
        self._today = today
        self._on_save = on_save
        self._on_delete = on_delete
        self.editing: EntryRow | None = None
        self.columnconfigure((0, 1), weight=1, uniform="field")
        word = "Income" if kind is BudgetKind.INCOME else "Expenses"
        self.title = ctk.CTkLabel(self, text=word, font=font(theme.SIZE_HEADING, "bold"),
                                  text_color=KIND_COLORS[kind], anchor="w")
        self.title.grid(row=0, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 4))
        self.mode_label = ctk.CTkLabel(self, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
                                       anchor="w", justify="left", wraplength=360)
        self.mode_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L)

        pad = dict(padx=(theme.SPACE_L, 4), pady=4)
        pad_right = dict(padx=(4, theme.SPACE_L), pady=4)
        self.name_field = LabeledEntry(self, "Name")
        self.name_field.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(theme.SPACE_L, theme.SPACE_L), pady=4)
        self.amount_field = LabeledEntry(self, "Amount", placeholder="0.00")
        self.amount_field.grid(row=3, column=0, sticky="ew", **pad)
        self.date_field = LabeledEntry(self, "Date (YYYY-MM-DD)", tk.StringVar(value=today()))
        self.date_field.grid(row=3, column=1, sticky="ew", **pad_right)
        self.category_field = LabeledSelect(self, "Category", categories(kind))
        self.category_field.grid(row=4, column=0, sticky="ew", **pad)
        self.project_field = LabeledSelect(self, "Project (optional)", [NO_PROJECT])
        self.project_field.grid(row=4, column=1, sticky="ew", **pad_right)

        self.repeat_var = tk.BooleanVar(value=False)
        self.repeat_box = ctk.CTkCheckBox(self, text="Repeats", variable=self.repeat_var, command=self._show_repeat,
                                          fg_color=theme.ACCENT, text_color=theme.TEXT_PRIMARY)
        self.repeat_box.grid(row=5, column=0, sticky="w", padx=theme.SPACE_L, pady=(8, 4))
        self.frequency_field = LabeledSelect(self, "How often", list(REPEAT_CHOICES))
        self.frequency_field.grid(row=6, column=0, sticky="ew", **pad)
        self.frequency_field.grid_remove()

        # Editing a recurring entry: the change is for the entire series or for one occurrence.
        self.scope_var = tk.StringVar(value=SERIES)
        self.scope_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.scope_frame.grid(row=7, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=(4, 0))
        ctk.CTkLabel(self.scope_frame, text="Apply the change to:", font=font(theme.SIZE_SMALL, "bold"),
                     text_color=theme.TEXT_MUTED).grid(row=0, column=0, sticky="w")
        self.series_radio = ctk.CTkRadioButton(self.scope_frame, text="The entire series", variable=self.scope_var,
                                               value=SERIES, command=self._scope_changed, fg_color=theme.ACCENT,
                                               text_color=theme.TEXT_PRIMARY)
        self.series_radio.grid(row=1, column=0, sticky="w", pady=2)
        self.occurrence_radio = ctk.CTkRadioButton(self.scope_frame, text="Only the occurrence on the date above",
                                                   variable=self.scope_var, value=OCCURRENCE,
                                                   command=self._scope_changed, fg_color=theme.ACCENT,
                                                   text_color=theme.TEXT_PRIMARY)
        self.occurrence_radio.grid(row=2, column=0, sticky="w", pady=2)
        self.scope_frame.grid_remove()

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=8, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=(8, 4))
        self.save_button = AppButton(buttons, self._add_text(), self._save, width=150)
        self.save_button.grid(row=0, column=0, sticky="w")
        self.cancel_button = AppButton(buttons, "Cancel", self.reset_form, variant="secondary", width=90)
        self.cancel_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.cancel_button.grid_remove()
        self.error_label = ctk.CTkLabel(self, text="", font=font(theme.SIZE_SMALL),
                                        text_color=theme.TONES["error"].foreground, anchor="w", justify="left",
                                        wraplength=360)
        self.error_label.grid(row=9, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L)

        ctk.CTkLabel(self, text=f"Saved {word.lower()}", font=font(theme.SIZE_SMALL, "bold"),
                     text_color=theme.TEXT_MUTED, anchor="w").grid(row=10, column=0, columnspan=2, sticky="ew",
                                                                   padx=theme.SPACE_L, pady=(theme.SPACE_M, 2))
        self.list_area = AppScrollableFrame(self, fg_color="transparent", height=_LIST_HEIGHT)
        self.list_area.grid(row=11, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_S, pady=(0, theme.SPACE_M))
        self.list_area.columnconfigure(0, weight=1)
        self.empty_label = ctk.CTkLabel(self.list_area, text="Nothing saved yet.", text_color=theme.TEXT_MUTED,
                                        font=font(theme.SIZE_SMALL))
        #: The rows shown: {"row": EntryRow, "frame", "edit", "delete"}.
        self.rows: list[dict] = []
        self.reset_form()

    def _add_text(self) -> str:
        return "Add income" if self.kind is BudgetKind.INCOME else "Add expense"

    def _show_repeat(self) -> None:
        if self.repeat_var.get():
            self.frequency_field.grid()
        else:
            self.frequency_field.grid_remove()

    def _scope_changed(self) -> None:
        occurrence = self.scope_var.get() == OCCURRENCE
        # One occurrence keeps the series' schedule: its repeat choice is not part of the change.
        self.repeat_box.configure(state="disabled" if occurrence else "normal")
        self.frequency_field.menu.configure(state="disabled" if occurrence else "normal")
        self.date_field.label.configure(text="Occurrence date (YYYY-MM-DD)" if occurrence else "Date (YYYY-MM-DD)")
        if occurrence and self.editing is not None:
            self.date_field.variable.set(self._occurrence_default)
        elif self.editing is not None:
            self.date_field.variable.set(self.editing.entry.start_date.isoformat())

    def set_projects(self, names: list[str]) -> None:
        self.project_field.set_values([NO_PROJECT, *names])

    def form(self) -> EntryForm:
        editing = self.editing
        return EntryForm(
            kind=self.kind.value, name=self.name_field.get(), amount=self.amount_field.get(),
            date=self.date_field.get(), category=self.category_field.get(), project=self.project_field.get(),
            repeat=self.frequency_field.get() if self.repeat_var.get() else None,
            entry_id=editing.entry.id if editing is not None else None,
            scope=self.scope_var.get() if editing is not None and editing.recurring else SERIES)

    def _save(self) -> None:
        self.error_label.configure(text="")
        self._on_save(self.form())

    def show_error(self, message: str) -> None:
        self.error_label.configure(text=f"Error: {message}" if message else "")

    def reset_form(self) -> None:
        self.editing = None
        for field in (self.name_field, self.amount_field):
            field.variable.set("")
        self.date_field.variable.set(self._today())
        self.category_field.variable.set(categories(self.kind)[0])
        self.project_field.variable.set(NO_PROJECT)
        self.repeat_var.set(False)
        self.repeat_box.configure(state="normal")
        self.frequency_field.menu.configure(state="normal")
        self.frequency_field.variable.set(next(iter(REPEAT_CHOICES)))
        self._show_repeat()
        self.scope_var.set(SERIES)
        self.scope_frame.grid_remove()
        self.date_field.label.configure(text="Date (YYYY-MM-DD)")
        self.mode_label.configure(text="")
        self.save_button.configure(text=self._add_text())
        self.cancel_button.grid_remove()
        self.show_error("")

    def edit(self, row: EntryRow, occurrence_default: str) -> None:
        """Load a saved entry into the form for editing."""
        entry = row.entry
        self.editing = row
        self._occurrence_default = occurrence_default
        self.name_field.variable.set(entry.name)
        self.amount_field.variable.set(row.amount_text)
        self.date_field.variable.set(entry.start_date.isoformat())
        self.category_field.variable.set(entry.category)
        self.project_field.variable.set(row.project_name or NO_PROJECT)
        self.repeat_var.set(entry.is_recurring)
        if entry.is_recurring:
            self.frequency_field.variable.set(REPEAT_LABELS[entry.recurrence])
        self._show_repeat()
        self.scope_var.set(SERIES)
        if entry.is_recurring:
            self.scope_frame.grid()
        else:
            self.scope_frame.grid_remove()
        self._scope_changed()
        self.mode_label.configure(text=f"Editing “{entry.name}” ({row.schedule_text}).")
        self.save_button.configure(text="Save changes")
        self.cancel_button.grid()
        self.show_error("")

    def show_rows(self, rows: list[EntryRow], on_edit: Callable[[EntryRow], None]) -> None:
        for widgets in self.rows:
            widgets["frame"].destroy()
        self.rows = []
        if not rows:
            self.empty_label.grid(row=0, column=0, sticky="w", padx=8, pady=8)
            return
        self.empty_label.grid_remove()
        for index, row in enumerate(rows):
            frame = ctk.CTkFrame(self.list_area, fg_color=theme.SECONDARY_BG, corner_radius=10)
            frame.grid(row=index, column=0, sticky="ew", padx=4, pady=3)
            frame.columnconfigure(0, weight=1)
            ctk.CTkLabel(frame, text=_clip(row.name, _NAME_CHARS), font=font(theme.SIZE_BODY, "bold"),
                         text_color=theme.TEXT_PRIMARY, anchor="w").grid(row=0, column=0, sticky="ew", padx=10,
                                                                          pady=(6, 0))
            details = [row.amount_text, row.schedule_text, row.category]
            if row.project_name:
                details.append(row.project_name)
            if row.edited_occurrences:
                details.append(f"{row.edited_occurrences} occurrence edit(s)")
            ctk.CTkLabel(frame, text=" · ".join(details), font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED,
                         anchor="w", justify="left", wraplength=300).grid(row=1, column=0, sticky="ew", padx=10,
                                                                          pady=(0, 6))
            edit = AppButton(frame, "Edit", lambda row=row: on_edit(row), variant="secondary", width=56, height=28)
            edit.grid(row=0, column=1, rowspan=2, padx=(4, 2))
            delete = AppButton(frame, "Delete", lambda row=row: self._on_delete(row), variant="ghost", width=64,
                               height=28)
            delete.grid(row=0, column=2, rowspan=2, padx=(2, 8))
            self.rows.append({"row": row, "frame": frame, "edit": edit, "delete": delete})


class BudgetingPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, controller: BudgetController, *, background_io: bool = True,
                 on_changed: Callable[[], None] | None = None) -> None:
        super().__init__(parent, fg_color=theme.APP_BG)
        self.controller = controller
        self.background_io = background_io
        #: Called after a saved change (the Day page re-reads its money strip when next shown).
        self._on_changed = on_changed
        self.section = GENERAL
        self.overview: BudgetOverview | None = None
        self.dialog: ModalDialog | None = None
        self._stacked: bool | None = None
        self._requests: dict[str, int] = {}
        self.columnconfigure(0, weight=1)
        self._build_section_bar()
        self.status_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                                         wraplength=760)
        self.section_host = ctk.CTkFrame(self, fg_color="transparent")
        self.section_host.grid(row=2, column=0, sticky="ew", padx=15, pady=(0, theme.SPACE_XL))
        self.section_host.columnconfigure(0, weight=1)
        self.section_frames = {GENERAL: self._build_general(self.section_host)}
        self._build_other_sections()
        self.bind("<Configure>", self._resized, add="+")
        self.show_section(GENERAL)
        self.refresh()

    # -- layout ---------------------------------------------------------------------

    def _build_section_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(18, 12))
        self.section_buttons: dict[str, AppButton] = {}
        for column, name in enumerate(SECTIONS):
            bar.columnconfigure(column, weight=1, uniform="section")
            button = AppButton(bar, name, lambda name=name: self.show_section(name), variant="secondary", height=40)
            button.grid(row=0, column=column, sticky="ew", padx=4)
            self.section_buttons[name] = button

    def _build_other_sections(self) -> None:
        """Stats and Future are built when first shown (see _ensure_section)."""
        self._builders = {STATS: lambda: self._build_stats(self.section_host),
                          FUTURE: lambda: self._build_future(self.section_host)}

    def _build_future(self, parent) -> FutureSection:
        self.future = FutureSection(parent, self)
        if self._stacked is not None:
            self.future.place_cards(self._stacked)
        return self.future

    def _ensure_section(self, name: str) -> bool:
        if name not in self.section_frames and name in self._builders:
            self.section_frames[name] = self._builders.pop(name)()
        return name in self.section_frames

    def show_section(self, name: str) -> None:
        if not self._ensure_section(name):
            return
        self.section = name
        for key, frame in self.section_frames.items():
            if key == name:
                frame.grid(row=0, column=0, sticky="ew")
            else:
                frame.grid_remove()
        for key, button in self.section_buttons.items():
            chosen = key == name
            button.configure(fg_color=theme.ACCENT if chosen else theme.SECONDARY_BG,
                             hover_color=theme.ACCENT_HOVER if chosen else theme.SECONDARY_HOVER,
                             text_color=theme.TEXT_ON_ACCENT if chosen else theme.TEXT_PRIMARY,
                             text=("● " if chosen else "") + key)
        self._load_section(name)

    def _resized(self, event) -> None:
        if event.widget is not self:
            return
        stacked = event.width / max(self._get_widget_scaling(), 0.1) < STACK_BELOW
        if stacked != self._stacked:
            self._place_panels(stacked)

    def _place_panels(self, stacked: bool) -> None:
        self._stacked = stacked
        if hasattr(self, "future"):
            self.future.place_cards(stacked)
        if stacked:
            self.income_panel.grid(row=0, column=0, columnspan=2, sticky="new", padx=5, pady=(0, 12))
            self.expense_panel.grid(row=1, column=0, columnspan=2, sticky="new", padx=5)
        else:
            self.income_panel.grid(row=0, column=0, columnspan=1, sticky="new", padx=(5, 8))
            self.expense_panel.grid(row=0, column=1, columnspan=1, sticky="new", padx=(8, 5))

    # -- Stats ------------------------------------------------------------------------

    def _chart_card(self, parent, row: int, title: str) -> BudgetBarChart:
        card = Card(parent)
        card.grid(row=row, column=0, sticky="ew", padx=5, pady=(0, 14))
        card.columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=title, font=font(theme.SIZE_BODY, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_M, 4))
        chart = BudgetBarChart(card, empty="No expenses recorded yet.")
        chart.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_M, pady=(0, theme.SPACE_M))
        return chart

    def _build_stats(self, parent) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.columnconfigure(0, weight=1)
        tiles = ctk.CTkFrame(frame, fg_color="transparent")
        tiles.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tiles.columnconfigure((0, 1), weight=1, uniform="tile")
        #: The two record tiles: {"card", "title", "value", "detail"}.
        self.stat_tiles: list[dict] = []
        for column in range(2):
            card = Card(tiles)
            card.grid(row=0, column=column, sticky="ew", padx=5)
            card.columnconfigure(0, weight=1)
            labels = {"card": card}
            for row, (key, size, weight, color) in enumerate((
                    ("title", theme.SIZE_SMALL, "bold", theme.TEXT_MUTED),
                    ("value", theme.SIZE_HEADING, "bold", theme.TEXT_PRIMARY),
                    ("detail", theme.SIZE_SMALL, "normal", theme.TEXT_MUTED))):
                labels[key] = ctk.CTkLabel(card, text="", font=font(size, weight), text_color=color, anchor="w")
                labels[key].grid(row=row, column=0, sticky="ew", padx=theme.SPACE_L,
                                 pady=(theme.SPACE_M if row == 0 else 0, theme.SPACE_M if row == 2 else 0))
            self.stat_tiles.append(labels)
        self.history_label = ctk.CTkLabel(frame, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
                                          anchor="w", justify="left", wraplength=760)
        self.history_label.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.weekly_chart = self._chart_card(frame, 2, f"Expenses by week (up to the last {WEEKS} weeks, "
                                                       "Monday to Sunday)")
        self.monthly_chart = self._chart_card(frame, 3, "Expenses by month (all recorded history)")
        self.daily_chart = self._chart_card(frame, 4, f"Expenses by day (up to the last {DAYS} days)")

        forecast = Card(frame)
        forecast.grid(row=5, column=0, sticky="ew", padx=5, pady=(0, 14))
        forecast.columnconfigure(0, weight=1)
        ctk.CTkLabel(forecast, text=f"Balance forecast (next {HORIZON_DAYS} days)", font=font(theme.SIZE_HEADING, "bold"),
                     text_color=theme.TEXT_PRIMARY, anchor="w").grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L,
                                                                     pady=(theme.SPACE_L, 4))
        self.forecast_summary = ctk.CTkLabel(forecast, text="", font=font(theme.SIZE_BODY, "bold"),
                                             text_color=theme.TEXT_PRIMARY, anchor="w", justify="left",
                                             wraplength=760)
        self.forecast_summary.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_L)
        self.forecast_chart = ForecastChart(forecast)
        self.forecast_chart.grid(row=2, column=0, sticky="ew", padx=theme.SPACE_M, pady=theme.SPACE_S)
        ctk.CTkLabel(forecast, text="Assumptions", font=font(theme.SIZE_SMALL, "bold"), text_color=theme.TEXT_MUTED,
                     anchor="w").grid(row=3, column=0, sticky="ew", padx=theme.SPACE_L)
        self.assumptions_label = ctk.CTkLabel(forecast, text="", font=font(theme.SIZE_SMALL),
                                              text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=760)
        self.assumptions_label.grid(row=4, column=0, sticky="ew", padx=theme.SPACE_L, pady=(0, theme.SPACE_L))
        self._build_history(frame, 6)
        self.stats_view: StatsView | None = None
        return frame

    def _build_history(self, parent, row: int) -> None:
        card = Card(parent)
        card.grid(row=row, column=0, sticky="ew", padx=5, pady=(0, 14))
        card.columnconfigure((0, 1, 2, 3), weight=1, uniform="history")
        ctk.CTkLabel(card, text="History", font=font(theme.SIZE_HEADING, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, columnspan=4, sticky="ew", padx=theme.SPACE_L,
                                      pady=(theme.SPACE_L, 2))
        ctk.CTkLabel(card, text="Every transaction up to today and every time you changed the balance by hand, "
                                "newest first, with the balance after each (calculated from your first balance on).",
                     font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                     wraplength=760).grid(row=1, column=0, columnspan=4, sticky="ew", padx=theme.SPACE_L)
        reload = lambda _value=None: self._load_history()  # noqa: E731 - a filter changed
        self.history_period = LabeledSelect(card, "Period", list(HISTORY_PERIODS), command=reload)
        self.history_period.variable.set("Last 90 days")
        self.history_type = LabeledSelect(card, "Type", list(HISTORY_TYPES), command=reload)
        self.history_category = LabeledSelect(card, "Category", [ANY], command=reload)
        self.history_project = LabeledSelect(card, "Project", [ANY], command=reload)
        for column, field in enumerate((self.history_period, self.history_type, self.history_category,
                                        self.history_project)):
            field.grid(row=2, column=column, sticky="ew", padx=(theme.SPACE_L if column == 0 else 4,
                                                                theme.SPACE_L if column == 3 else 4), pady=6)
        self.history_summary = ctk.CTkLabel(card, text="", font=font(theme.SIZE_SMALL, "bold"),
                                            text_color=theme.TEXT_PRIMARY, anchor="w", justify="left", wraplength=760)
        self.history_summary.grid(row=3, column=0, columnspan=4, sticky="ew", padx=theme.SPACE_L, pady=(4, 4))
        table = ctk.CTkFrame(card, fg_color="transparent")
        table.grid(row=4, column=0, columnspan=4, sticky="ew", padx=theme.SPACE_M, pady=(0, theme.SPACE_L))
        table.columnconfigure(0, weight=1)
        columns = (("date", "Date", 90), ("type", "Type", 150), ("name", "Name", 170), ("category", "Category", 110),
                   ("amount", "Amount", 100), ("balance", "Balance after", 110), ("detail", "Details", 220))
        self.history_table = ttk.Treeview(table, columns=[key for key, _, _ in columns], show="headings", height=12,
                                          selectmode="browse")
        for key, title, width in columns:
            self.history_table.heading(key, text=title)
            self.history_table.column(key, width=width, minwidth=60,
                                      anchor="e" if key in ("amount", "balance") else "w", stretch=True)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.history_table.yview)
        self.history_table.configure(yscrollcommand=scroll.set)
        self.history_table.grid(row=0, column=0, sticky="ew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.history_view: HistoryView | None = None
        self._color_history()

    def _color_history(self) -> None:
        resolve = theme.resolve
        self.history_table.tag_configure("income", foreground=resolve(INCOME_COLOR))
        self.history_table.tag_configure("expense", foreground=resolve(EXPENSE_COLOR))
        self.history_table.tag_configure("balance", foreground=resolve(theme.ACCENT))

    def _load_history(self) -> None:
        period, kind = self.history_period.get(), self.history_type.get()
        category, project = self.history_category.get(), self.history_project.get()
        self._read("history", lambda: self.controller.history(period, kind, category, project), self._show_history)

    def _show_history(self, result: ControllerResult[HistoryView]) -> None:
        if not self._status(result):
            return
        view = self.history_view = result.value
        self.history_category.set_values([ANY, *view.categories])
        self.history_project.set_values([ANY, *view.projects])
        self.history_summary.configure(text=view.summary if view.matching or view.lines
                                       else "Nothing recorded for these filters yet.")
        self.history_table.delete(*self.history_table.get_children())
        for line in view.lines:
            self.history_table.insert("", "end", values=(line.date_text, line.type_text, line.name, line.category,
                                                         line.amount_text, line.balance_text, line.detail),
                                      tags=(line.row.kind,))

    def _show_stats(self, result: ControllerResult[StatsView]) -> None:
        if not self._status(result):
            return
        view = self.stats_view = result.value
        for tile, labels in zip(view.tiles, self.stat_tiles):
            labels["title"].configure(text=tile.title)
            labels["value"].configure(text=tile.value)
            labels["detail"].configure(text=tile.detail)
        self.history_label.configure(text=view.history_note)
        self.weekly_chart.draw(view.weekly)
        self.monthly_chart.draw(view.monthly)
        self.daily_chart.draw(view.daily)
        self.forecast_summary.configure(text=view.forecast_summary)
        self.forecast_chart.draw(view.forecast_points, view.forecast.zero_day)
        self.assumptions_label.configure(text="\n".join(f"• {line}" for line in view.assumptions))

    def on_appearance_changed(self) -> None:
        if STATS not in self.section_frames:
            return
        for chart in (self.weekly_chart, self.monthly_chart, self.daily_chart, self.forecast_chart):
            chart.redraw()
        self._color_history()

    # -- General ----------------------------------------------------------------------

    def _build_general(self, parent) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.columnconfigure(0, weight=1)
        balance = Card(frame)
        balance.grid(row=0, column=0, sticky="ew", padx=5, pady=(0, 14))
        balance.columnconfigure((0, 1, 2), weight=1)
        ctk.CTkLabel(balance, text="Current balance", font=font(theme.SIZE_HEADING, "bold"),
                     text_color=theme.TEXT_PRIMARY, anchor="w").grid(row=0, column=0, columnspan=3, sticky="ew",
                                                                     padx=theme.SPACE_L, pady=(theme.SPACE_L, 2))
        self.balance_label = ctk.CTkLabel(balance, text="", font=font(theme.SIZE_BODY), text_color=theme.TEXT_MUTED,
                                          anchor="w")
        self.balance_label.grid(row=1, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)
        self.balance_amount = LabeledEntry(balance, "Balance", placeholder="0.00",
                                           hint="Saved as today's balance; may be negative (overdrawn). Every change "
                                                "is kept in Stats > History.", wraplength=420)
        self.balance_amount.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(theme.SPACE_L, 4),
                                 pady=(6, theme.SPACE_L))
        self.balance_button = AppButton(balance, "Save balance", self.save_balance, width=130)
        self.balance_button.grid(row=2, column=2, sticky="nw", padx=(4, theme.SPACE_L), pady=(28, theme.SPACE_L))
        self.balance_error = ctk.CTkLabel(balance, text="", font=font(theme.SIZE_SMALL),
                                          text_color=theme.TONES["error"].foreground, anchor="w")
        self.balance_error.grid(row=3, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)

        columns = ctk.CTkFrame(frame, fg_color="transparent")
        columns.grid(row=1, column=0, sticky="ew")
        columns.columnconfigure((0, 1), weight=1, uniform="money")
        self.panels = {}
        for kind in BudgetKind:
            self.panels[kind] = EntryPanel(columns, kind, today=self._today, on_save=self.save_entry,
                                           on_delete=self.delete_entry)
        self.income_panel, self.expense_panel = self.panels[BudgetKind.INCOME], self.panels[BudgetKind.EXPENSE]
        self._place_panels(False)
        return frame

    def _today(self) -> str:
        return self.controller.today().isoformat()

    # -- storage calls --------------------------------------------------------------------

    def _read(self, name: str, work, done) -> None:
        """A read whose answer is shown only while it is the newest read of its kind."""
        self._requests[name] = self._requests.get(name, 0) + 1
        number = self._requests[name]

        def deliver(result) -> None:
            if number == self._requests[name]:
                done(result)

        run_io(self, work, deliver, background=self.background_io, supersede=(id(self), name))

    def _write(self, work, done) -> None:
        run_io(self, work, done, background=self.background_io, serial=WRITE_LANE)

    def _changed(self) -> None:
        if self._on_changed is not None:
            self._on_changed()
        self.refresh()

    def on_show(self) -> None:
        """Re-read the stored records each time the page is shown (another page, a sync or direct writes)."""
        self.refresh()

    def refresh(self) -> None:
        self._load_section(self.section)

    def _load_section(self, name: str) -> None:
        if name == GENERAL:
            self._read("overview", self.controller.overview, self._show_overview)
        elif name == STATS:
            self._read("stats", self.controller.stats, self._show_stats)
            self._load_history()
        elif name == FUTURE:
            self._read("future", self.controller.future,
                       lambda result: self.future.show(result.value) if self._status(result) else None)

    def _status(self, result: ControllerResult) -> bool:
        if result.ok:
            self.status_label.grid_remove()
            return True
        self.status_label.configure(text=f"Budget data is unavailable: {result.error}")
        self.status_label.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 8))
        return False

    def _show_overview(self, result: ControllerResult[BudgetOverview]) -> None:
        if not self._status(result):
            return
        overview = self.overview = result.value
        self.balance_label.configure(text=overview.balance_text)
        if overview.balance is not None and not self.balance_amount.get():
            self.balance_amount.variable.set(money_text(overview.balance.amount_minor))
        names = [name for name, _ in overview.projects]
        for kind, rows in ((BudgetKind.INCOME, overview.income), (BudgetKind.EXPENSE, overview.expenses)):
            panel = self.panels[kind]
            panel.set_projects(names)
            panel.show_rows(rows, self.edit_entry)

    # -- actions ----------------------------------------------------------------------------

    def save_balance(self) -> None:
        amount = self.balance_amount.get()

        def done(result) -> None:
            if result.ok:
                self.balance_error.configure(text="")
                self._changed()
            else:
                self.balance_error.configure(text=f"Error: {result.error}")

        self._write(lambda: self.controller.save_balance(amount), done)

    def save_entry(self, form: EntryForm) -> None:
        panel = self.panels[BudgetKind(form.kind)]

        def done(result) -> None:
            if result.ok:
                panel.reset_form()
                self._changed()
            else:
                panel.show_error(result.error or "The entry could not be saved.")

        self._write(lambda: self.controller.save_entry(form), done)

    def edit_entry(self, row: EntryRow) -> None:
        self.panels[row.entry.kind].edit(row, self.controller.next_occurrence(row.entry).isoformat())

    def delete_entry(self, row: EntryRow) -> None:
        entry = row.entry

        def delete(scope: str, day: str | None) -> None:
            panel = self.panels[entry.kind]

            def done(result) -> None:
                if result.ok:
                    if panel.editing is not None and panel.editing.entry.id == entry.id:
                        panel.reset_form()
                    self._changed()
                else:
                    panel.show_error(result.error or "The entry could not be deleted.")

            self._write(lambda: self.controller.delete_entry(entry.id, scope=scope, occurrence_date=day), done)

        if entry.is_recurring:
            self.dialog = ScopeDialog(
                self, title=f"Delete “{entry.name}”", action="Delete", danger=True,
                prompt=f"“{entry.name}” repeats ({row.schedule_text}). What do you want to delete?",
                default_date=self.controller.next_occurrence(entry).isoformat(),
                on_done=lambda scope, day: delete(scope, day if scope == OCCURRENCE else None))
        else:
            self.dialog = ConfirmDialog(
                self, title=f"Delete “{entry.name}”", danger=True, confirm_text="Delete",
                message=f"Delete “{entry.name}” ({row.amount_text}, {row.schedule_text})?",
                on_result=lambda confirmed: delete(SERIES, None) if confirmed else None)
            self.dialog.present()
