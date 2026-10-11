"""
app/ui/conflict_view.py

One synchronization conflict as widgets: both versions side by side with the
two choices (ConflictComparison), and the dialog that asks about new
conflicts as soon as a synchronization ends with them (ConflictDialog).

Widgets only. What a conflict is, which choices it allows and what a choice
does are AccountController's (conflicts, resolve) and SyncService's; the
Account page shows the same comparison for every open conflict, so a conflict
left for later here is decided there exactly as before.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

import customtkinter as ctk

from app.ui import theme
from app.ui.account_controller import RESOLUTION_LABELS, ConflictView
from app.ui.background import ControllerResult, run_in_background
from app.ui.components import AppButton, ModalDialog, Notice, focus_target, font


def conflict_summary(conflict: ConflictView) -> str:
    """What the record is, both versions' ages, and what the choices mean for it, in words."""
    dash = "—"
    versions = (f"This device: changed {conflict.local_updated_at or dash}, based on server version "
                f"{conflict.base_version or dash}. Server: version {conflict.remote_version or dash}, changed "
                f"{conflict.remote_updated_at or dash}.")
    text = f"{conflict.title} — {conflict.kind}\n{versions}"
    if conflict.remote_deleted:
        text += "\nThe server deleted this record."
    if conflict.server_message:
        text += f"\nServer: {conflict.server_message}"
    if conflict.context:
        text += "\n" + "\n".join(conflict.context)
    return text


class ConflictComparison(ctk.CTkFrame):
    """A conflict's summary, every differing field with both values, and the two choices; on_choice(choice)."""

    def __init__(self, parent, *, on_choice: Callable[[str], None], wraplength: int = 440) -> None:
        super().__init__(parent, fg_color="transparent")
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.conflict: ConflictView | None = None
        self.title = ctk.CTkLabel(self, text="", font=font(theme.SIZE_BODY, "bold"), text_color=theme.TEXT_PRIMARY,
                                  anchor="w", justify="left", wraplength=wraplength)
        self.title.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.difference_tree = ttk.Treeview(self, columns=("field", "local", "remote"), show="headings", height=5)
        for column, label, width in (("field", "Field", 120), ("local", "This device", 170),
                                     ("remote", "Server", 170)):
            self.difference_tree.heading(column, text=label)
            self.difference_tree.column(column, width=width, stretch=True)
        self.difference_tree.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 6))
        self.choice_buttons = {
            choice: AppButton(self, RESOLUTION_LABELS[choice], lambda choice=choice: on_choice(choice),
                              variant="secondary")
            for choice in ("keep_local", "accept_remote")
        }
        self.choice_buttons["keep_local"].grid(row=2, column=0, sticky="ew", padx=(0, 4))
        self.choice_buttons["accept_remote"].grid(row=2, column=1, sticky="ew", padx=(4, 0))
        self.reasons = ctk.CTkLabel(self, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
                                    anchor="w", justify="left", wraplength=wraplength)
        self.reasons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def show(self, conflict: ConflictView, *, enabled: bool = True) -> None:
        """Show the conflict; a choice it does not allow is disabled and says why. `enabled`: no choice is running."""
        self.conflict = conflict
        self.title.configure(text=conflict_summary(conflict))
        self.difference_tree.delete(*self.difference_tree.get_children())
        for index, difference in enumerate(conflict.differences):
            self.difference_tree.insert("", tk.END, iid=str(index),
                                        values=(difference.field, difference.local, difference.remote))
        reasons = []
        for choice, button in self.choice_buttons.items():
            reason = conflict.choices.get(choice, "Not available.")
            button.configure(state="normal" if reason is None and enabled else "disabled")
            if reason:
                reasons.append(f"{RESOLUTION_LABELS[choice]} is not available: {reason}")
        self.reasons.configure(text="\n".join(reasons))


class ConflictDialog(ModalDialog):
    """
    Asks about the conflicts a synchronization just ended with, one at a
    time: the comparison, the two choices, and "Decide later", which closes
    the dialog and leaves what is undecided open on the Account page.

    `resolve(conflict_id, choice)` is AccountController.resolve and runs in a
    worker; `on_resolved()` runs after each decision that was applied.
    """

    MESSAGE = ("The server's version of this record differs from yours: it changed on the server after this device "
               "last received it. Choose which version to keep. Nothing has been overwritten.")

    def __init__(self, parent, conflicts: list[ConflictView], *,
                 resolve: Callable[[str, str], ControllerResult], on_resolved: Callable[[], None] | None = None,
                 on_close: Callable[[], None] | None = None) -> None:
        super().__init__(parent, "The server's version differs from yours", width=500)
        self.conflicts = list(conflicts)
        self.index = 0
        self.busy = False
        self._resolve, self._on_resolved, self._on_close = resolve, on_resolved, on_close
        self.message = ctk.CTkLabel(self.body, text=self.MESSAGE, anchor="w", justify="left", wraplength=480,
                                    font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY)
        self.message.grid(row=0, column=0, sticky="ew")
        self.position = ctk.CTkLabel(self.body, text="", anchor="w", font=font(theme.SIZE_SMALL),
                                     text_color=theme.TEXT_MUTED)
        self.position.grid(row=1, column=0, sticky="ew", pady=(theme.SPACE_S, 0))
        self.comparison = ConflictComparison(self.body, on_choice=self.choose, wraplength=480)
        self.comparison.grid(row=2, column=0, sticky="ew", pady=(theme.SPACE_S, 0))
        self.notice = Notice(self.body, wraplength=440, dismissible=False)
        self.notice.grid(row=3, column=0, sticky="ew", pady=(theme.SPACE_S, 0))
        self.notice.hide()
        # Neither version is the default: Enter must not choose one by accident.
        self.later_button = AppButton(self.buttons, "Decide later", self.cancel, variant="secondary", width=130)
        self.later_button.pack(side="right")
        self.initial_focus = focus_target(self.later_button)
        self._show_current()
        self.present()

    @property
    def conflict(self) -> ConflictView:
        return self.conflicts[self.index]

    def _show_current(self) -> None:
        total = len(self.conflicts)
        self.position.configure(text=f"Conflict {self.index + 1} of {total}" if total > 1 else "")
        self.comparison.show(self.conflict, enabled=not self.busy)

    def choose(self, choice: str) -> None:
        conflict = self.conflict
        if self.busy or conflict.choices.get(choice) is not None:
            return
        self.busy = True
        self.notice.hide()
        self.later_button.configure(state="disabled")
        self.comparison.show(conflict, enabled=False)

        def done(result: ControllerResult) -> None:
            self.busy = False
            if self._closed:
                return
            self.later_button.configure(state="normal")
            if not result.ok:
                self.notice.show("error", result.error)
                self._show_current()
                return
            if self._on_resolved is not None:
                self._on_resolved()
            if self.index + 1 < len(self.conflicts):
                self.index += 1
                self._show_current()
            else:
                self.close(None)

        # The decision belongs to the account that was asked: the dialog, not the workspace, bounds its result.
        if not run_in_background(self, lambda: self._resolve(conflict.id, choice), done, still_current=lambda: True):
            self.busy = False
            self.close(None)

    def cancel(self) -> None:
        if not self.busy:  # a choice that is being applied is waited for
            super().cancel()

    def close(self, result=None) -> None:
        already = self._closed
        super().close(result)
        if not already and self._on_close is not None:
            self._on_close()
