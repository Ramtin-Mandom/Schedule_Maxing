"""
app/ui/day_money.py

The Day page's budget widgets. They only draw a BudgetDayView
(app/ui/budget_controller.py) -- every amount and total comes from it, so
nothing here adds money up.

    MoneyTotals   "↑ income  ↓ expense" for the displayed day, beside the points
                  total; double-clicking either opens that kind's transactions
    MoneyStrip    the day's transactions under the To Do notes: narrow cards
                  (name and amount; income green, expense red) side by side,
                  scrolling sideways only when they are wider than the section
    DayMoneyDialog  the day's income (or expenses), one amount field per
                  transaction; Save hands the changed amounts back to the page
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from app.budget.models import BudgetKind
from app.ui import theme
from app.ui.budget_controller import BudgetDayView, DayItem
from app.ui.components import LabeledEntry, ModalDialog, font
from app.ui.theme import EXPENSE_COLOR, INCOME_COLOR
from app.ui.paint_widgets import AppScrollableFrame

#: A transaction card is this narrow (logical px); its name is cut to fit.
MONEY_CARD_WIDTH = 116
MONEY_CARD_HEIGHT = 46
MONEY_CARD_GAP = 6
#: At most this many cards are drawn (a bounded widget count); a last card says how many more the dialogs list.
MONEY_CARD_LIMIT = 40
_NAME_CHARS = 15
_CARD_BG = {BudgetKind.INCOME: ("#DCFCE7", "#14532D"), BudgetKind.EXPENSE: ("#FEE2E2", "#7F1D1D")}
_CARD_TEXT = {BudgetKind.INCOME: INCOME_COLOR, BudgetKind.EXPENSE: EXPENSE_COLOR}
ARROWS = {BudgetKind.INCOME: "↑", BudgetKind.EXPENSE: "↓"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _bind_double(widget, callback: Callable[[], None]) -> None:
    """Double-click on a CustomTkinter widget (its canvas and its inner label/text) runs `callback`."""
    targets = [widget, *(getattr(widget, name) for name in ("_canvas", "_label", "_text_label")
                         if isinstance(getattr(widget, name, None), tk.Misc))]
    for target in targets:
        target.bind("<Double-Button-1>", lambda _event: callback(), add="+")


class MoneyTotals(ctk.CTkFrame):
    def __init__(self, parent, *, on_open: Callable[[BudgetKind], None]) -> None:
        super().__init__(parent, fg_color="transparent")
        self.labels: dict[BudgetKind, ctk.CTkLabel] = {}
        for column, kind in enumerate(BudgetKind):
            label = ctk.CTkLabel(self, text=f"{ARROWS[kind]} 0.00", font=font(theme.SIZE_BODY, "bold"),
                                 text_color=_CARD_TEXT[kind], fg_color=theme.SECONDARY_BG, corner_radius=10,
                                 padx=10, height=30, cursor="hand2")
            label.grid(row=0, column=column, padx=(0, 6))
            _bind_double(label, lambda kind=kind: on_open(kind))
            self.labels[kind] = label

    def show(self, view: BudgetDayView | None) -> None:
        texts = {BudgetKind.INCOME: view.income_text if view else "–",
                 BudgetKind.EXPENSE: view.expense_text if view else "–"}
        for kind, label in self.labels.items():
            label.configure(text=f"{ARROWS[kind]} {texts[kind]}")

    def text(self, kind: BudgetKind) -> str:
        return self.labels[kind].cget("text")


class MoneyStrip(ctk.CTkFrame):
    def __init__(self, parent, *, on_open: Callable[[BudgetKind], None]) -> None:
        super().__init__(parent, fg_color="transparent")
        self._on_open = on_open
        self.columnconfigure(0, weight=1)
        self.caption = ctk.CTkLabel(self, text="Money", font=font(theme.SIZE_SMALL, "bold"),
                                    text_color=theme.TEXT_MUTED, anchor="w")
        self.caption.grid(row=0, column=0, sticky="w", padx=theme.SPACE_S, pady=(0, 2))
        #: The sideways-scrolling area, created with the first cards (a day without money builds none).
        self.area: AppScrollableFrame | None = None
        #: The cards shown, in order: {"item", "kind", "frame", "name", "amount"}.
        self.cards: list[dict] = []
        self.more_label: ctk.CTkLabel | None = None

    def _ensure_area(self) -> AppScrollableFrame:
        if self.area is None:
            self.area = AppScrollableFrame(self, orientation="horizontal", fg_color="transparent",
                                           height=MONEY_CARD_HEIGHT + 4)
            self.area._scrollbar.configure(width=MONEY_CARD_WIDTH)
            self.area.grid(row=1, column=0, sticky="ew")
            self.area._parent_canvas.bind("<Configure>", lambda _event: self._update_scrollbar(), add="+")
        return self.area

    def show(self, view: BudgetDayView | None) -> None:
        for card in self.cards:
            card["frame"].destroy()
        self.cards = []
        if view is None or view.empty:
            self.grid_remove()
            return
        self.grid()
        self._ensure_area()
        items: list[tuple[BudgetKind, DayItem]] = [(BudgetKind.INCOME, item) for item in view.income]
        items += [(BudgetKind.EXPENSE, item) for item in view.expenses]
        hidden = len(items) - MONEY_CARD_LIMIT if len(items) > MONEY_CARD_LIMIT else 0
        if hidden:
            items = items[:MONEY_CARD_LIMIT - 1]
            hidden += 1
        for column, (kind, item) in enumerate(items):
            frame = ctk.CTkFrame(self.area, width=MONEY_CARD_WIDTH, height=MONEY_CARD_HEIGHT, corner_radius=8,
                                 fg_color=_CARD_BG[kind])
            frame.grid(row=0, column=column, padx=(0, MONEY_CARD_GAP))
            frame.grid_propagate(False)
            frame.columnconfigure(0, weight=1)
            name = ctk.CTkLabel(frame, text=_clip(item.name, _NAME_CHARS), font=font(theme.SIZE_CAPTION),
                                text_color=_CARD_TEXT[kind], anchor="w", height=18)
            name.grid(row=0, column=0, sticky="ew", padx=8, pady=(4, 0))
            sign = "+" if kind is BudgetKind.INCOME else "−"
            amount = ctk.CTkLabel(frame, text=f"{sign}{item.amount_text}", font=font(theme.SIZE_SMALL, "bold"),
                                  text_color=_CARD_TEXT[kind], anchor="w", height=18)
            amount.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 4))
            for widget in (frame, name, amount):
                _bind_double(widget, lambda kind=kind: self._on_open(kind))
            self.cards.append({"item": item, "kind": kind, "frame": frame, "name": name, "amount": amount})
        self.more_label = None
        if hidden:
            frame = ctk.CTkFrame(self.area, width=MONEY_CARD_WIDTH, height=MONEY_CARD_HEIGHT, corner_radius=8,
                                 fg_color=theme.SECONDARY_BG)
            frame.grid(row=0, column=len(items), padx=(0, MONEY_CARD_GAP))
            frame.grid_propagate(False)
            self.more_label = ctk.CTkLabel(frame, text=f"+{hidden} more\n(double-click a total)",
                                           font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED)
            self.more_label.grid(row=0, column=0, padx=8, pady=4)
            self.cards.append({"item": None, "kind": None, "frame": frame, "name": self.more_label,
                               "amount": self.more_label})
        self.after_idle(self._update_scrollbar)

    def content_width(self) -> float:
        """The cards' total width in screen pixels."""
        scale = self._get_widget_scaling()
        return len(self.cards) * (MONEY_CARD_WIDTH + MONEY_CARD_GAP) * scale

    def needs_scrolling(self) -> bool:
        return self.area is not None and self.content_width() > self.area._parent_canvas.winfo_width() + 1

    def _update_scrollbar(self) -> None:
        if not self.winfo_exists() or self.area is None:
            return
        if self.cards and self.needs_scrolling():
            self.area._scrollbar.grid()
        else:
            self.area._scrollbar.grid_remove()
            self.area._parent_canvas.xview_moveto(0)


class DayMoneyDialog(ModalDialog):
    """A day's income (or expenses): one amount field per transaction, saved all together."""

    def __init__(self, parent, view: BudgetDayView, kind: BudgetKind, *,
                 on_save: Callable[["DayMoneyDialog", dict], None]) -> None:
        word = "Income" if kind is BudgetKind.INCOME else "Expenses"
        super().__init__(parent, f"{word} on {view.day:%A, %B} {view.day.day}")
        self.kind = kind
        self._on_save = on_save
        self.items = view.items(kind)
        #: {DayItem.key: (item, field)} -- one amount field per transaction.
        self.fields: dict = {}
        total = view.income_text if kind is BudgetKind.INCOME else view.expense_text
        ctk.CTkLabel(self.body, text=f"{word} total: {total}", font=font(theme.SIZE_BODY, "bold"),
                     text_color=_CARD_TEXT[kind], anchor="w").grid(row=0, column=0, sticky="ew",
                                                                   pady=(0, theme.SPACE_S))
        if not self.items:
            ctk.CTkLabel(self.body, text=f"No {word.lower()} on this date. Add it on the Budgeting page.",
                         text_color=theme.TEXT_MUTED, anchor="w").grid(row=1, column=0, sticky="ew")
            self.add_buttons("Close", self.cancel)
            self.cancel_button.pack_forget()
            self.present()
            return
        for row, item in enumerate(self.items, start=1):
            note = " (one occurrence of a repeating item)" if item.recurring else ""
            field = LabeledEntry(self.body, f"{_clip(item.name, 40)} · {item.occurrence.category}{note}",
                                 tk.StringVar(value=item.amount_text))
            field.grid(row=row, column=0, sticky="ew", pady=3)
            self.fields[item.key] = (item, field)
        self.error_label = ctk.CTkLabel(self.body, text="", text_color=theme.TONES["error"].foreground, anchor="w",
                                        justify="left", wraplength=420, font=font(theme.SIZE_SMALL))
        self.error_label.grid(row=len(self.items) + 1, column=0, sticky="ew")
        self.add_buttons("Save amounts", self.save)
        self.initial_focus = self.fields[self.items[0].key][1].entry
        self.present()

    def amounts(self) -> dict:
        """The amounts that differ from what is stored ({DayItem.key: text})."""
        return {key: field.get() for key, (item, field) in self.fields.items()
                if field.get().strip() != item.amount_text}

    def save(self) -> None:
        self.show_error("")
        self.primary_button.configure(state="disabled")
        self._on_save(self, self.amounts())

    def show_error(self, message: str) -> None:
        if hasattr(self, "error_label"):
            self.error_label.configure(text=f"Error: {message}" if message else "")
        if hasattr(self, "primary_button") and message:
            self.primary_button.configure(state="normal")
