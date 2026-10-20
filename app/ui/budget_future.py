"""
app/ui/budget_future.py

Budgeting > Future (part of app/ui/budget_page.py):

    Left, future income ideas: add one with a name and an expected amount;
    selecting one shows its editable description and its progress -- the
    amount received or firmly secured so far against the expected amount
    (BudgetController.PROGRESS_DEFINITION), as a bar and in words.
    Right, the wishlist: items with a name, amount, category, optional project
    and description, and no date, so they count nowhere (not in the balance,
    Stats, Day totals or the forecast). "Turn into expense" asks for a date and
    creates exactly one expense; the item then moves to "Turned into expenses"
    and cannot be converted again.

Every action is one BudgetController call run through the page's storage
helpers (off the Tk thread; writes in the write lane); nothing is computed here.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from app.ui import theme
from app.ui.budget_controller import (
    NO_PROJECT,
    PROGRESS_DEFINITION,
    FutureView,
    IdeaRow,
    WishRow,
    categories,
)
from app.ui.components import AppButton, Card, ConfirmDialog, LabeledEntry, LabeledSelect, ModalDialog, font
from app.ui.paint_widgets import AppScrollableFrame, AppTextbox

_LIST_HEIGHT = 200
_NAME_CHARS = 30


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


class ConvertDialog(ModalDialog):
    """Ask for the date of the expense a wishlist item becomes."""

    def __init__(self, parent, row: WishRow, *, default_date: str, on_done: Callable[[str], None]) -> None:
        super().__init__(parent, f"Turn “{row.name}” into an expense")
        self._on_done = on_done
        ctk.CTkLabel(self.body, text=(f"This creates one expense of {row.amount_text} ({row.category}) on the date "
                                      "below, and moves the item to “Turned into expenses”."),
                     anchor="w", justify="left", wraplength=420, text_color=theme.TEXT_PRIMARY).grid(
            row=0, column=0, sticky="ew", pady=(0, theme.SPACE_S))
        self.date_field = LabeledEntry(self.body, "Expense date (YYYY-MM-DD)", tk.StringVar(value=default_date))
        self.date_field.grid(row=1, column=0, sticky="ew")
        self.add_buttons("Create expense", self._done)
        self.initial_focus = self.date_field.entry
        self.present()

    def _done(self) -> None:
        day = self.date_field.get()
        self.close(day)
        self._on_done(day)


class FutureSection(ctk.CTkFrame):
    def __init__(self, parent, page) -> None:
        super().__init__(parent, fg_color="transparent")
        #: The BudgetingPage: its controller and storage helpers (_read, _write, _changed).
        self.page = page
        self.view: FutureView | None = None
        self.selected_idea: IdeaRow | None = None
        self.editing_wish: WishRow | None = None
        self.columnconfigure((0, 1), weight=1, uniform="future")
        self.ideas_card = self._build_ideas()
        self.wish_card = self._build_wishlist()
        self.place_cards(stacked=False)

    def place_cards(self, stacked: bool) -> None:
        if stacked:
            self.ideas_card.grid(row=0, column=0, columnspan=2, sticky="new", padx=5, pady=(0, 12))
            self.wish_card.grid(row=1, column=0, columnspan=2, sticky="new", padx=5)
        else:
            self.ideas_card.grid(row=0, column=0, columnspan=1, sticky="new", padx=(5, 8))
            self.wish_card.grid(row=0, column=1, columnspan=1, sticky="new", padx=(8, 5))

    @staticmethod
    def _heading(card, text: str, note: str) -> None:
        ctk.CTkLabel(card, text=text, font=font(theme.SIZE_HEADING, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L,
                                      pady=(theme.SPACE_L, 2))
        ctk.CTkLabel(card, text=note, font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED, anchor="w",
                     justify="left", wraplength=360).grid(row=1, column=0, columnspan=2, sticky="ew",
                                                          padx=theme.SPACE_L, pady=(0, 6))

    @staticmethod
    def _error_label(card) -> ctk.CTkLabel:
        return ctk.CTkLabel(card, text="", font=font(theme.SIZE_SMALL), text_color=theme.TONES["error"].foreground,
                            anchor="w", justify="left", wraplength=360)

    # -- income ideas -------------------------------------------------------------------

    def _build_ideas(self) -> Card:
        card = Card(self)
        card.columnconfigure((0, 1), weight=1, uniform="field")
        self._heading(card, "Future income ideas", "Possible income, not counted anywhere. " + PROGRESS_DEFINITION)
        self.idea_name = LabeledEntry(card, "Name")
        self.idea_name.grid(row=2, column=0, sticky="ew", padx=(theme.SPACE_L, 4), pady=4)
        self.idea_expected = LabeledEntry(card, "Expected amount", placeholder="0.00")
        self.idea_expected.grid(row=2, column=1, sticky="ew", padx=(4, theme.SPACE_L), pady=4)
        self.add_idea_button = AppButton(card, "Add idea", self.add_idea, width=120)
        self.add_idea_button.grid(row=3, column=0, sticky="w", padx=theme.SPACE_L, pady=(4, 2))
        self.idea_error = self._error_label(card)
        self.idea_error.grid(row=4, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L)
        self.idea_list = AppScrollableFrame(card, fg_color="transparent", height=_LIST_HEIGHT)
        self.idea_list.grid(row=5, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_S, pady=(6, 6))
        self.idea_list.columnconfigure(0, weight=1)
        self.idea_empty = ctk.CTkLabel(self.idea_list, text="No ideas yet.", text_color=theme.TEXT_MUTED,
                                       font=font(theme.SIZE_SMALL))
        #: The idea rows: {"row": IdeaRow, "button"}.
        self.idea_rows: list[dict] = []

        # The selected idea: editable details and its progress.
        self.detail = detail = ctk.CTkFrame(card, fg_color=theme.SECONDARY_BG, corner_radius=12)
        detail.grid(row=6, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_M, pady=(0, theme.SPACE_L))
        detail.columnconfigure((0, 1), weight=1, uniform="detail")
        self.detail_name = LabeledEntry(detail, "Name")
        self.detail_name.grid(row=0, column=0, sticky="ew", padx=(theme.SPACE_M, 4), pady=(theme.SPACE_M, 4))
        self.detail_expected = LabeledEntry(detail, "Expected amount")
        self.detail_expected.grid(row=0, column=1, sticky="ew", padx=(4, theme.SPACE_M), pady=(theme.SPACE_M, 4))
        ctk.CTkLabel(detail, text="Description", font=font(theme.SIZE_SMALL, "bold"), text_color=theme.TEXT_MUTED,
                     anchor="w").grid(row=1, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_M)
        self.detail_description = AppTextbox(detail, height=80, fg_color=theme.INPUT_BG, text_color=theme.TEXT_PRIMARY,
                                             border_color=theme.CARD_BORDER, border_width=1, wrap="word")
        self.detail_description.grid(row=2, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_M, pady=(2, 6))
        ctk.CTkLabel(detail, text="Progress", font=font(theme.SIZE_SMALL, "bold"), text_color=theme.TEXT_MUTED,
                     anchor="w").grid(row=3, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_M)
        self.detail_secured = LabeledEntry(detail, "Received or secured so far", hint="Raise it as money comes in.")
        self.detail_secured.grid(row=4, column=0, sticky="ew", padx=(theme.SPACE_M, 4), pady=2)
        self.progress_bar = ctk.CTkProgressBar(detail, progress_color=theme.SUCCESS)
        self.progress_bar.grid(row=5, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_M, pady=(6, 2))
        self.progress_label = ctk.CTkLabel(detail, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_PRIMARY,
                                           anchor="w", justify="left", wraplength=340)
        self.progress_label.grid(row=6, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_M)
        buttons = ctk.CTkFrame(detail, fg_color="transparent")
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_M, pady=theme.SPACE_M)
        self.save_idea_button = AppButton(buttons, "Save idea", self.save_idea, width=110)
        self.save_idea_button.grid(row=0, column=0)
        self.delete_idea_button = AppButton(buttons, "Delete", self.delete_idea, variant="ghost", width=80)
        self.delete_idea_button.grid(row=0, column=1, padx=(8, 0))
        detail.grid_remove()
        return card

    def add_idea(self) -> None:
        name, expected = self.idea_name.get(), self.idea_expected.get()

        def done(result) -> None:
            if result.ok:
                self.idea_name.variable.set("")
                self.idea_expected.variable.set("")
                self.idea_error.configure(text="")
                self.page._changed()
            else:
                self.idea_error.configure(text=f"Error: {result.error}")

        self.page._write(lambda: self.page.controller.save_idea(None, name=name, expected=expected), done)

    def select_idea(self, row: IdeaRow | None) -> None:
        self.selected_idea = row
        for widgets in self.idea_rows:
            chosen = row is not None and widgets["row"].idea.id == row.idea.id
            widgets["button"].configure(fg_color=theme.ACCENT_SOFT if chosen else theme.SECONDARY_BG)
        if row is None:
            self.detail.grid_remove()
            return
        self.detail.grid()
        self.detail_name.variable.set(row.name)
        self.detail_expected.variable.set(row.expected_text)
        self.detail_description.delete("1.0", "end")
        self.detail_description.insert("1.0", row.idea.description or "")
        self.detail_secured.variable.set(row.secured_text)
        self.progress_bar.set(row.progress_fraction)
        self.progress_label.configure(text=f"{row.status}: {row.progress_text}.")

    def save_idea(self) -> None:
        row = self.selected_idea
        if row is None:
            return
        name, expected, secured = self.detail_name.get(), self.detail_expected.get(), self.detail_secured.get()
        description = self.detail_description.get("1.0", "end").strip() or None

        def done(result) -> None:
            if result.ok:
                self.idea_error.configure(text="")
                self.page._changed()
            else:
                self.idea_error.configure(text=f"Error: {result.error}")

        self.page._write(lambda: self.page.controller.save_idea(row.idea.id, name=name, expected=expected,
                                                                description=description, secured=secured), done)

    def delete_idea(self) -> None:
        row = self.selected_idea
        if row is None:
            return

        def confirmed(yes: bool) -> None:
            if yes:
                self.page._write(lambda: self.page.controller.delete_idea(row.idea.id),
                                 lambda result: self._after_delete(result, self.idea_error))

        self.page.dialog = ConfirmDialog(self, title=f"Delete “{row.name}”", danger=True,
                                         confirm_text="Delete", message=f"Delete the idea “{row.name}”?",
                                         on_result=confirmed)
        self.page.dialog.present()

    def _after_delete(self, result, error_label) -> None:
        if result.ok:
            error_label.configure(text="")
            self.page._changed()
        else:
            error_label.configure(text=f"Error: {result.error}")

    # -- wishlist --------------------------------------------------------------------------

    def _build_wishlist(self) -> Card:
        card = Card(self)
        card.columnconfigure((0, 1), weight=1, uniform="field")
        self._heading(card, "Wishlist", "Things you would like to buy. They have no date and count nowhere (balance, "
                                        "Stats, Day totals, forecast) until you turn one into an expense.")
        pad, pad_right = dict(padx=(theme.SPACE_L, 4), pady=4), dict(padx=(4, theme.SPACE_L), pady=4)
        self.wish_name = LabeledEntry(card, "Name")
        self.wish_name.grid(row=2, column=0, sticky="ew", **pad)
        self.wish_amount = LabeledEntry(card, "Amount", placeholder="0.00")
        self.wish_amount.grid(row=2, column=1, sticky="ew", **pad_right)
        self.wish_category = LabeledSelect(card, "Category", categories("expense"))
        self.wish_category.grid(row=3, column=0, sticky="ew", **pad)
        self.wish_project = LabeledSelect(card, "Project (optional)", [NO_PROJECT])
        self.wish_project.grid(row=3, column=1, sticky="ew", **pad_right)
        self.wish_description = LabeledEntry(card, "Description (optional)")
        self.wish_description.grid(row=4, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=4)
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L, pady=(4, 2))
        self.save_wish_button = AppButton(buttons, "Add to wishlist", self.save_wish, width=150)
        self.save_wish_button.grid(row=0, column=0)
        self.cancel_wish_button = AppButton(buttons, "Cancel", self.reset_wish_form, variant="secondary", width=90)
        self.cancel_wish_button.grid(row=0, column=1, padx=(8, 0))
        self.cancel_wish_button.grid_remove()
        self.wish_error = self._error_label(card)
        self.wish_error.grid(row=6, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L)
        self.wish_list = AppScrollableFrame(card, fg_color="transparent", height=_LIST_HEIGHT)
        self.wish_list.grid(row=7, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_S, pady=(6, 4))
        self.wish_list.columnconfigure(0, weight=1)
        self.wish_empty = ctk.CTkLabel(self.wish_list, text="Your wishlist is empty.", text_color=theme.TEXT_MUTED,
                                       font=font(theme.SIZE_SMALL))
        ctk.CTkLabel(card, text="Turned into expenses", font=font(theme.SIZE_SMALL, "bold"),
                     text_color=theme.TEXT_MUTED, anchor="w").grid(row=8, column=0, columnspan=2, sticky="ew",
                                                                   padx=theme.SPACE_L)
        self.converted_label = ctk.CTkLabel(card, text="None yet.", font=font(theme.SIZE_SMALL),
                                            text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=360)
        self.converted_label.grid(row=9, column=0, columnspan=2, sticky="ew", padx=theme.SPACE_L,
                                  pady=(0, theme.SPACE_L))
        #: The open items' rows: {"row": WishRow, "frame", "edit", "delete", "convert"}.
        self.wish_rows: list[dict] = []
        return card

    def reset_wish_form(self) -> None:
        self.editing_wish = None
        for field in (self.wish_name, self.wish_amount, self.wish_description):
            field.variable.set("")
        self.wish_category.variable.set(categories("expense")[0])
        self.wish_project.variable.set(NO_PROJECT)
        self.save_wish_button.configure(text="Add to wishlist")
        self.cancel_wish_button.grid_remove()

    def edit_wish(self, row: WishRow) -> None:
        self.editing_wish = row
        self.wish_name.variable.set(row.name)
        self.wish_amount.variable.set(row.amount_text)
        self.wish_category.variable.set(row.category)
        self.wish_project.variable.set(row.project_name or NO_PROJECT)
        self.wish_description.variable.set(row.description)
        self.save_wish_button.configure(text="Save changes")
        self.cancel_wish_button.grid()

    def save_wish(self) -> None:
        editing = self.editing_wish
        fields = dict(name=self.wish_name.get(), amount=self.wish_amount.get(), category=self.wish_category.get(),
                      project=self.wish_project.get(), description=self.wish_description.get().strip() or None)

        def done(result) -> None:
            if result.ok:
                self.wish_error.configure(text="")
                self.reset_wish_form()
                self.page._changed()
            else:
                self.wish_error.configure(text=f"Error: {result.error}")

        self.page._write(lambda: self.page.controller.save_wish(editing.wish.id if editing else None, **fields), done)

    def delete_wish(self, row: WishRow) -> None:
        def confirmed(yes: bool) -> None:
            if yes:
                self.page._write(lambda: self.page.controller.delete_wish(row.wish.id),
                                 lambda result: self._after_delete(result, self.wish_error))

        self.page.dialog = ConfirmDialog(self, title=f"Delete “{row.name}”", danger=True,
                                         confirm_text="Delete", on_result=confirmed,
                                         message=f"Remove “{row.name}” from the wishlist?")
        self.page.dialog.present()

    def convert_wish(self, row: WishRow) -> None:
        def chosen(day: str) -> None:
            def done(result) -> None:
                if result.ok:
                    self.wish_error.configure(text="")
                    if self.editing_wish is not None and self.editing_wish.wish.id == row.wish.id:
                        self.reset_wish_form()
                    self.page._changed()
                else:
                    self.wish_error.configure(text=f"Error: {result.error}")

            self.page._write(lambda: self.page.controller.convert_wish(row.wish.id, day), done)

        self.page.dialog = ConvertDialog(self, row, default_date=self.page.controller.today().isoformat(),
                                         on_done=chosen)

    # -- rendering -----------------------------------------------------------------------------

    def show(self, view: FutureView) -> None:
        self.view = view
        for widgets in self.idea_rows:
            widgets["button"].destroy()
        self.idea_rows = []
        if view.ideas:
            self.idea_empty.grid_remove()
        else:
            self.idea_empty.grid(row=0, column=0, sticky="w", padx=8, pady=8)
        for index, row in enumerate(view.ideas):
            button = AppButton(self.idea_list, f"{_clip(row.name, _NAME_CHARS)}  ·  {row.expected_text}  ·  "
                                               f"{row.progress_percent}%", lambda row=row: self.select_idea(row),
                               variant="secondary", height=34, anchor="w")
            button.grid(row=index, column=0, sticky="ew", padx=4, pady=2)
            self.idea_rows.append({"row": row, "button": button})
        selected = self.selected_idea
        self.select_idea(next((row for row in view.ideas if selected and row.idea.id == selected.idea.id), None))

        names = [name for name, _ in view.projects]
        self.wish_project.set_values([NO_PROJECT, *names])
        for widgets in self.wish_rows:
            widgets["frame"].destroy()
        self.wish_rows = []
        if view.wishes:
            self.wish_empty.grid_remove()
        else:
            self.wish_empty.grid(row=0, column=0, sticky="w", padx=8, pady=8)
        for index, row in enumerate(view.wishes):
            frame = ctk.CTkFrame(self.wish_list, fg_color=theme.SECONDARY_BG, corner_radius=10)
            frame.grid(row=index, column=0, sticky="ew", padx=4, pady=3)
            frame.columnconfigure(0, weight=1)
            ctk.CTkLabel(frame, text=_clip(row.name, _NAME_CHARS), font=font(theme.SIZE_BODY, "bold"),
                         text_color=theme.TEXT_PRIMARY, anchor="w").grid(row=0, column=0, columnspan=3, sticky="ew",
                                                                          padx=10, pady=(6, 0))
            details = [row.amount_text, row.category, *([row.project_name] if row.project_name else [])]
            ctk.CTkLabel(frame, text=" · ".join(details) + (f"\n{row.description}" if row.description else ""),
                         font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                         wraplength=320).grid(row=1, column=0, columnspan=3, sticky="ew", padx=10)
            convert = AppButton(frame, "Turn into expense", lambda row=row: self.convert_wish(row), height=28,
                                width=130)
            convert.grid(row=2, column=0, sticky="w", padx=(10, 4), pady=6)
            edit = AppButton(frame, "Edit", lambda row=row: self.edit_wish(row), variant="secondary", height=28,
                             width=56)
            edit.grid(row=2, column=1, padx=4, pady=6)
            delete = AppButton(frame, "Delete", lambda row=row: self.delete_wish(row), variant="ghost", height=28,
                               width=64)
            delete.grid(row=2, column=2, padx=(4, 10), pady=6)
            self.wish_rows.append({"row": row, "frame": frame, "edit": edit, "delete": delete, "convert": convert})
        self.converted_label.configure(text="\n".join(f"{row.name} · {row.amount_text} · {row.converted_text}"
                                                      for row in view.converted) or "None yet.")
