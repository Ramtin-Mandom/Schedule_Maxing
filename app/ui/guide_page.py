"""
app/ui/guide_page.py

The in-app "How to Use" documentation page, shown in the navigation right
before About. A page header, a Contents card whose buttons scroll to each
section, and one card per section of app/ui/guide_content.py (headings,
paragraphs and bullet lists in the design system's type and spacing).
Read-only: it calls nothing but its own scrolling.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from app.ui.paint_widgets import AppScrollableFrame

from app.ui import theme
from app.ui.components import AppButton, Card, font
from app.ui.guide_content import GUIDE_SECTIONS, GUIDE_SUBTITLE, GUIDE_TITLE, Block, GuideSection
from app.ui.shell_state import LayoutMode

#: Text width per layout, so a narrow window never scrolls sideways.
_WRAP = {LayoutMode.WIDE: 900, LayoutMode.MEDIUM: 620, LayoutMode.NARROW: 400}
_CONTENTS_COLUMNS = {LayoutMode.WIDE: 4, LayoutMode.MEDIUM: 3, LayoutMode.NARROW: 1}


class GuidePage(ctk.CTkFrame):
    def __init__(self, parent) -> None:
        super().__init__(parent, fg_color=theme.APP_BG, corner_radius=0)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.layout: LayoutMode | None = None
        self.body = AppScrollableFrame(self, fg_color="transparent", scrollbar_button_color=theme.SECONDARY_HOVER)
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.body, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(22, 12))
        header.columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=GUIDE_TITLE, font=font(theme.SIZE_TITLE, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, sticky="ew")
        self.subtitle = ctk.CTkLabel(header, text=GUIDE_SUBTITLE, font=font(theme.SIZE_BODY),
                                     text_color=theme.TEXT_MUTED, anchor="w", justify="left")
        self.subtitle.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.wrapped: list[tuple[ctk.CTkLabel, int]] = [(self.subtitle, 0)]

        self.contents = Card(self.body)
        self.contents.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_M))
        ctk.CTkLabel(self.contents, text="Contents", font=font(theme.SIZE_HEADING, "bold"),
                     text_color=theme.TEXT_PRIMARY, anchor="w").grid(row=0, column=0, columnspan=4, sticky="ew",
                                                                    padx=theme.SPACE_L, pady=(theme.SPACE_L, 6))
        self.contents_buttons: dict[str, AppButton] = {}
        for item in GUIDE_SECTIONS:
            self.contents_buttons[item.key] = AppButton(self.contents, item.title, lambda key=item.key: self.scroll_to(key),
                                                        variant="ghost", height=30, anchor="w",
                                                        font=font(theme.SIZE_SMALL))

        self.cards: dict[str, Card] = {}
        for index, item in enumerate(GUIDE_SECTIONS, start=2):
            card = self._section_card(item)
            card.grid(row=index, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_M))
            self.cards[item.key] = card
        ctk.CTkFrame(self.body, fg_color="transparent", height=theme.SPACE_XL).grid(
            row=len(GUIDE_SECTIONS) + 2, column=0)
        self.set_layout(LayoutMode.WIDE)

    def _section_card(self, item: GuideSection) -> Card:
        card = Card(self.body)
        card.columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=item.title, font=font(theme.SIZE_HEADING, "bold"), text_color=theme.TEXT_PRIMARY,
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_L, 4))
        row = 1
        for block in item.blocks:
            for label, indent in self._block(card, block):
                label.grid(row=row, column=0, sticky="ew", padx=(theme.SPACE_L + indent, theme.SPACE_L), pady=(4, 0))
                self.wrapped.append((label, indent))
                row += 1
        ctk.CTkFrame(card, fg_color="transparent", height=theme.SPACE_L).grid(row=row, column=0)
        return card

    @staticmethod
    def _block(card: Card, block: Block) -> list[tuple[ctk.CTkLabel, int]]:
        if isinstance(block, str):
            return [(ctk.CTkLabel(card, text=block, font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY,
                                  anchor="w", justify="left"), 0)]
        return [(ctk.CTkLabel(card, text=f"•  {line}", font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY,
                              anchor="w", justify="left"), 14) for line in block]

    # ------------------------------------------------------------------ layout

    def set_layout(self, mode: LayoutMode) -> None:
        if mode == self.layout:
            return
        self.layout = mode
        width = _WRAP[mode]
        for label, indent in self.wrapped:
            label.configure(wraplength=width - indent)
        columns = _CONTENTS_COLUMNS[mode]
        for column in range(4):
            self.contents.columnconfigure(column, weight=1 if column < columns else 0, uniform="contents")
        for index, button in enumerate(self.contents_buttons.values()):
            button.grid(row=1 + index // columns, column=index % columns, sticky="ew", padx=(theme.SPACE_S, 0), pady=2)
        last_row = 1 + (len(self.contents_buttons) - 1) // columns
        self.contents.grid_rowconfigure(last_row + 1, minsize=theme.SPACE_M)
        pad = theme.SPACE_XL if mode != LayoutMode.NARROW else theme.SPACE_M
        for card in (self.contents, *self.cards.values()):
            card.grid_configure(padx=pad)

    def scroll_to(self, key: str) -> None:
        """Bring a section's card to the top of the page."""
        card = self.cards[key]
        try:
            self.body.update_idletasks()
            self.body._parent_canvas.yview_moveto(max(0.0, card.winfo_y() / max(1, self.body.winfo_height())))
        except (AttributeError, tk.TclError):
            pass
