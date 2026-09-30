"""
app/ui/account_page.py

The Account page (Milestone 4, Prompt 2): backend connection, registration,
sign-in and sign-out, the explicit association of this device's ownerless
records, synchronization status with Sync now, and conflict review.

Widgets only: everything goes through AccountController (Tk-free), and every
network call runs in a background worker (app/ui/background.py) with its
button disabled until the answer arrives, so a slow backend never freezes
the window and a double click never sends twice. Results reach the page on
the Tk thread and are dropped if the page is gone or the app is closing.
After anything that changes whose records the desktop works on (sign-in,
sign-out, association, another backend), on_workspace_changed() lets the app
switch the workspace and rebuild its pages.

Nothing here stores a password: the password field is cleared after every
attempt and the session token stays inside SyncService.

Direct PostgreSQL storage (a controller with storage_mode == "postgres",
app/ui/direct_services.DirectAccountController): the same page registers,
signs in, shows the profile and signs out directly against the database.
The backend address, association, synchronization and conflict cards are
hidden -- there is no local copy to associate or synchronize -- and
"Check connection" checks the database and its schema revision.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

import customtkinter as ctk

from app.ui.paint_widgets import AppScrollableFrame

from app.sync.engine import AssociationError, AssociationPreview
from app.ui import theme
from app.ui.account_controller import (
    ENTITY_LABELS,
    RESOLUTION_LABELS,
    AccountController,
    ConflictView,
    ConnectionView,
    InvalidInput,
    masked_email,
    profile_summary,
)
from app.ui.background import ControllerResult, run_in_background
from app.ui.components import AppButton, Card, ConfirmDialog, LabeledEntry, Notice, SectionTitle, font
from app.ui.pages import PageHeader


def _always() -> bool:
    return True


class AccountPage(ctk.CTkFrame):
    def __init__(self, parent, controller: AccountController, *, on_workspace_changed: Callable[..., None]) -> None:
        super().__init__(parent, fg_color=theme.APP_BG, corner_radius=0)
        self.controller = controller
        self._on_workspace_changed = on_workspace_changed
        #: Direct PostgreSQL storage: no backend address, synchronization, association or conflicts.
        self.direct = getattr(controller, "storage_mode", None) == "postgres"
        self.view: ConnectionView | None = None
        self.preview: AssociationPreview | None = None
        self.conflicts: list[ConflictView] = []
        self.selected_conflict: ConflictView | None = None
        self.mode = "sign_in"
        self.busy: set[str] = set()
        self.dialog: ConfirmDialog | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        subtitle = ("Sign in to your account in the PostgreSQL database. This mode needs the network; nothing is "
                    "kept on this computer." if self.direct else
                    "Connect to a backend to sign in and synchronize. Everything also works offline.")
        PageHeader(self, "Account", subtitle).grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(22, 12))
        self.body = AppScrollableFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=theme.SPACE_M, pady=(0, theme.SPACE_L))
        self.body.columnconfigure(0, weight=1)

        self._build_connection()
        self._build_account()
        self._build_association()
        self._build_sync()
        self._build_conflicts()
        performance = self._card(5, "Performance")
        ctk.CTkLabel(performance, text="Performance analytics will appear here in a later milestone.",
                     wraplength=440, anchor="w", justify="left", text_color=theme.TEXT_MUTED).grid(
                         row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        if self.direct:
            for widget in (self.backend_field, self.save_backend_button, self.offline_button, self.association_card,
                           self.sync_card, self.conflicts_card):
                widget.grid_remove()
            self.check_button.configure(text="Check database")
        self.refresh()

    # ----------------------------------------------------------------- building

    def _card(self, row: int, title: str, subtitle: str = "") -> Card:
        card = Card(self.body)
        card.grid(row=row, column=0, sticky="ew", padx=theme.SPACE_S, pady=(0, theme.SPACE_M))
        card.columnconfigure(0, weight=1)
        SectionTitle(card, title, subtitle, wraplength=460).grid(row=0, column=0, columnspan=3, sticky="ew",
                                                                 padx=theme.SPACE_L, pady=(theme.SPACE_L, 8))
        return card

    def _build_connection(self) -> None:
        card = self.connection_card = self._card(0, "Connection")
        self.headline = ctk.CTkLabel(card, text="", font=font(theme.SIZE_BODY, "bold"), text_color=theme.TEXT_PRIMARY,
                                     anchor="w", justify="left", wraplength=460)
        self.headline.grid(row=1, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)
        self.detail = ctk.CTkLabel(card, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED, anchor="w",
                                   justify="left", wraplength=460)
        self.detail.grid(row=2, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L, pady=(2, 10))
        self.backend_field = LabeledEntry(card, "Backend address", placeholder="https://your-backend.example.com",
                                          hint="https:// (or http:// for a server on this computer). No password here.")
        self.backend_field.grid(row=3, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)
        self.backend_field.entry.bind("<Return>", lambda _e: self.save_backend(), add="+")
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=4, column=0, columnspan=3, sticky="w", padx=theme.SPACE_L, pady=(10, 0))
        self.save_backend_button = AppButton(buttons, "Save address", self.save_backend)
        self.save_backend_button.grid(row=0, column=0, padx=(0, 8), pady=4)
        self.check_button = AppButton(buttons, "Check connection", self.check_backend, variant="secondary")
        self.check_button.grid(row=0, column=1, padx=(0, 8), pady=4)
        self.offline_button = AppButton(buttons, "Work offline", self.remove_backend, variant="ghost")
        self.offline_button.grid(row=0, column=2, pady=4)
        self.connection_notice = Notice(card, wraplength=440)
        self.connection_notice.grid(row=5, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.connection_notice.hide()
        ctk.CTkFrame(card, height=theme.SPACE_L, fg_color="transparent").grid(row=6, column=0)

    def _build_account(self) -> None:
        card = self.account_card = self._card(1, "Account")
        self.signed_in_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.signed_in_frame.columnconfigure(0, weight=1)
        self.signed_in_label = ctk.CTkLabel(self.signed_in_frame, text="", font=font(theme.SIZE_BODY, "bold"),
                                            text_color=theme.TEXT_PRIMARY, anchor="w", justify="left", wraplength=440)
        self.signed_in_label.grid(row=0, column=0, sticky="ew")
        self.profile_label = ctk.CTkLabel(self.signed_in_frame, text="", font=font(theme.SIZE_SMALL),
                                          text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=440)
        self.profile_label.grid(row=1, column=0, sticky="ew", pady=(2, 8))
        self.sign_out_button = AppButton(self.signed_in_frame, "Sign out", self.sign_out, variant="secondary")
        self.sign_out_button.grid(row=2, column=0, sticky="w")
        sign_out_note = ("Signing out closes access to your records on this computer until you sign in again. They "
                         "stay in the database." if getattr(self, "direct", False) else
                         "Signing out forgets the session on this device. Your records stay here; the backend's token "
                         "simply expires (it is not revoked).")
        ctk.CTkLabel(self.signed_in_frame, text=sign_out_note,
                     font=font(theme.SIZE_CAPTION), text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                     wraplength=440).grid(row=3, column=0, sticky="ew", pady=(6, 0))

        self.form_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.form_frame.columnconfigure((0, 1), weight=1, uniform="mode")
        self.mode_buttons = {
            "sign_in": AppButton(self.form_frame, "Sign in", lambda: self.set_mode("sign_in"), variant="secondary"),
            "register": AppButton(self.form_frame, "Create account", lambda: self.set_mode("register"),
                                  variant="secondary"),
        }
        self.mode_buttons["sign_in"].grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.mode_buttons["register"].grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.email_field = LabeledEntry(self.form_frame, "Email", placeholder="name@example.com")
        self.email_field.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.password_field = LabeledEntry(self.form_frame, "Password")
        self.password_field.entry.configure(show="•")
        self.password_field.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)
        self.name_field = LabeledEntry(self.form_frame, "Display name (optional)")
        self.submit_button = AppButton(self.form_frame, "Sign in", self.submit_account)
        self.submit_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        for entry in (self.email_field.entry, self.password_field.entry, self.name_field.entry):
            entry.bind("<Return>", lambda _e: self.submit_account(), add="+")
        self.account_notice = Notice(card, wraplength=440)
        self.account_notice.grid(row=3, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.account_notice.hide()
        ctk.CTkFrame(card, height=theme.SPACE_L, fg_color="transparent").grid(row=4, column=0)
        self.set_mode("sign_in")

    def _build_association(self) -> None:
        card = self.association_card = self._card(
            2, "Records on this device without an account",
            "Signing in never uploads or claims them. Associating makes them your account's, with the same ids and "
            "history, and they are synchronized from then on.")
        self.association_label = ctk.CTkLabel(card, text="", font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY,
                                              anchor="w", justify="left", wraplength=460)
        self.association_label.grid(row=1, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="w", padx=theme.SPACE_L, pady=(10, 0))
        self.associate_button = AppButton(buttons, "Associate these records...", self.review_association)
        self.associate_button.grid(row=0, column=0, padx=(0, 8))
        self.preview_button = AppButton(buttons, "Refresh", self.load_preview, variant="secondary")
        self.preview_button.grid(row=0, column=1)
        self.association_notice = Notice(card, wraplength=440)
        self.association_notice.grid(row=3, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.association_notice.hide()
        ctk.CTkFrame(card, height=theme.SPACE_L, fg_color="transparent").grid(row=4, column=0)

    def _build_sync(self) -> None:
        card = self.sync_card = self._card(3, "Synchronization")
        self.sync_lines = ctk.CTkLabel(card, text="", font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY,
                                       anchor="w", justify="left", wraplength=460)
        self.sync_lines.grid(row=1, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)
        self.sync_button = AppButton(card, "Sync now", self.sync_now)
        self.sync_button.grid(row=2, column=0, sticky="w", padx=theme.SPACE_L, pady=(10, 0))
        self.sync_notice = Notice(card, wraplength=440)
        self.sync_notice.grid(row=3, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.sync_notice.hide()
        ctk.CTkFrame(card, height=theme.SPACE_L, fg_color="transparent").grid(row=4, column=0)

    def _build_conflicts(self) -> None:
        card = self.conflicts_card = self._card(
            4, "Conflicts", "A record changed both here and on the server, or the server refused a change. Choose "
                            "which version to keep; nothing is overwritten silently.")
        list_frame = ctk.CTkFrame(card, fg_color="transparent")
        list_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)
        list_frame.columnconfigure(0, weight=1)
        self.conflict_tree = ttk.Treeview(list_frame, columns=("record", "what"), show="headings", height=4,
                                          selectmode="browse")
        self.conflict_tree.heading("record", text="Record")
        self.conflict_tree.heading("what", text="What happened")
        self.conflict_tree.column("record", width=180, stretch=True)
        self.conflict_tree.column("what", width=260, stretch=True)
        self.conflict_tree.grid(row=0, column=0, sticky="ew")
        self.conflict_tree.bind("<<TreeviewSelect>>", lambda _e: self._conflict_selected(), add="+")
        self.conflicts_empty = ctk.CTkLabel(card, text="", font=font(theme.SIZE_SMALL), text_color=theme.TEXT_MUTED,
                                            anchor="w", justify="left", wraplength=460)
        self.conflicts_empty.grid(row=2, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L, pady=(6, 0))

        self.detail_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.detail_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.detail_frame.columnconfigure(0, weight=1)
        self.detail_title = ctk.CTkLabel(self.detail_frame, text="", font=font(theme.SIZE_BODY, "bold"),
                                         text_color=theme.TEXT_PRIMARY, anchor="w", justify="left", wraplength=440)
        self.detail_title.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.difference_tree = ttk.Treeview(self.detail_frame, columns=("field", "local", "remote"), show="headings",
                                            height=5)
        for column, label, width in (("field", "Field", 120), ("local", "This device", 170),
                                     ("remote", "Server", 170)):
            self.difference_tree.heading(column, text=label)
            self.difference_tree.column(column, width=width, stretch=True)
        self.difference_tree.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 6))
        self.choice_buttons = {
            "keep_local": AppButton(self.detail_frame, RESOLUTION_LABELS["keep_local"],
                                    lambda: self.resolve("keep_local"), variant="secondary"),
            "accept_remote": AppButton(self.detail_frame, RESOLUTION_LABELS["accept_remote"],
                                       lambda: self.resolve("accept_remote"), variant="secondary"),
        }
        self.choice_buttons["keep_local"].grid(row=2, column=0, sticky="ew", padx=(0, 4))
        self.choice_buttons["accept_remote"].grid(row=2, column=1, sticky="ew", padx=(4, 0))
        self.choice_reasons = ctk.CTkLabel(self.detail_frame, text="", font=font(theme.SIZE_SMALL),
                                           text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=440)
        self.choice_reasons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.detail_frame.grid_remove()
        self.conflict_notice = Notice(card, wraplength=440)
        self.conflict_notice.grid(row=4, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L, pady=(8, 0))
        self.conflict_notice.hide()
        ctk.CTkFrame(card, height=theme.SPACE_L, fg_color="transparent").grid(row=5, column=0)

    # ----------------------------------------------------------------- work helpers

    def _run(self, name: str, work: Callable[[], ControllerResult], done: Callable[[ControllerResult], None],
             buttons: tuple = ()) -> None:
        """Run `work` off the Tk thread, once at a time per name, with its buttons disabled meanwhile."""
        if name in self.busy:
            return
        self.busy.add(name)
        for button in buttons:
            button.configure(state="disabled")

        def finished(result: ControllerResult) -> None:
            self.busy.discard(name)
            for button in buttons:
                if button.winfo_exists():
                    button.configure(state="normal")
            done(result)
            self._render_buttons()

        # Account work changes the workspace on purpose, so its result is bound to this page, not the workspace.
        if not run_in_background(self, work, finished, still_current=_always):
            self.busy.discard(name)

    # ----------------------------------------------------------------- refresh

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        """Re-read the connection status (local, fast); then the preview and conflicts in the background."""
        result = self.controller.connection()
        if not result.ok:
            self.headline.configure(text=f"Error: {result.error}")
            return
        self.render(result.value)
        if self.view.state == "signed_in":
            if not self.direct:
                self.load_preview()
                self.load_conflicts()
            self._load_profile()

    def database_checked(self, view: ConnectionView) -> None:
        """Direct storage: show the outcome of the startup/explicit database check."""
        self.render(view)
        if view.last_error:
            self.connection_notice.show("error", view.last_error)
        else:
            self.connection_notice.show("success", "The database answered and its schema is current.")

    def render(self, view: ConnectionView) -> None:
        self.view = view
        self.headline.configure(text=view.headline)
        self.detail.configure(text=view.detail)
        if not self.backend_field.get() and view.backend_url:
            self.backend_field.variable.set(view.backend_url)
        if view.state == "signed_in":
            self.form_frame.grid_remove()
            self.signed_in_label.configure(text=f"Signed in as {masked_email(view.signed_in_email)}")
            self.signed_in_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)
            if not self.direct:
                self.association_card.grid()
        else:
            self.profile_label.configure(text="")
            self.signed_in_frame.grid_remove()
            self.form_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=theme.SPACE_L)
            if view.state == "session_ended" and not self.email_field.get():
                self.email_field.variable.set(view.workspace_email or "")
            self.association_card.grid_remove()
            self.preview = None
        lines = [
            f"Last successful sync: {view.last_success_text}",
            f"Changes waiting to be sent: {view.pending if view.pending is not None else 'none (no account)'}",
            f"Open conflicts: {view.conflicts}",
            "Status: synchronizing now" if view.in_progress else "Status: idle",
        ]
        if view.last_error:
            lines.append(f"Last problem: {view.last_error}")
        self.sync_lines.configure(text="\n".join(lines))
        self._render_buttons()

    def _render_buttons(self) -> None:
        view = self.view
        if view is None:
            return
        configured = self.direct or view.state != "unconfigured"
        self._enable(self.check_button, configured and "check" not in self.busy)
        self._enable(self.offline_button, configured and "backend" not in self.busy)
        self._enable(self.submit_button, configured and "account" not in self.busy)
        self._enable(self.sync_button, view.can_sync and "sync" not in self.busy)
        can_associate = bool(self.preview and self.preview.total and not self.preview.problems)
        self._enable(self.associate_button, can_associate and "associate" not in self.busy)
        if not configured:
            self.submit_button.configure(text="Enter a backend address first")
        elif "account" in self.busy:
            self.submit_button.configure(text="Please wait...")
        else:
            self.submit_button.configure(text="Sign in" if self.mode == "sign_in" else "Create account")
        self.sync_button.configure(text="Synchronizing..." if ("sync" in self.busy or view.in_progress) else "Sync now")

    @staticmethod
    def _enable(button: AppButton, enabled: bool) -> None:
        button.configure(state="normal" if enabled else "disabled")

    # ----------------------------------------------------------------- connection

    def save_backend(self) -> None:
        address = self.backend_field.get().strip()
        self.backend_field.set_error(None)
        self._run("backend", lambda: self.controller.configure_backend(address), self._backend_saved,
                  (self.save_backend_button,))

    def remove_backend(self) -> None:
        self.backend_field.variable.set("")
        self._run("backend", lambda: self.controller.configure_backend(None), self._backend_saved,
                  (self.offline_button,))

    def _backend_saved(self, result: ControllerResult[ConnectionView]) -> None:
        if not result.ok:
            self.backend_field.set_error(result.error)
            self.connection_notice.show("error", result.error)
            return
        self.connection_notice.show("success", "Backend saved." if result.value.backend_url else
                                    "Working offline; no backend is configured.")
        self._on_workspace_changed()  # a backend switch ends the session: the workspace may change
        self.render(result.value)

    def check_backend(self) -> None:
        def done(result: ControllerResult[ConnectionView]) -> None:
            if not result.ok:
                self.connection_notice.show("error", result.error)
                return
            if self.direct:
                self.database_checked(result.value)
                return
            self.render(result.value)
            if result.value.reachable:
                self.connection_notice.show("success", "The backend answered.")
            else:
                self.connection_notice.show("warning", "The backend did not answer. Check the address and your "
                                                       "network; everything keeps working offline.")

        self._run("check", self.controller.check_backend, done, (self.check_button,))

    # ----------------------------------------------------------------- account

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        for key, button in self.mode_buttons.items():
            active = key == mode
            button.configure(text=("✓ " if active else "") + ("Sign in" if key == "sign_in" else "Create account"),
                             fg_color=theme.ACCENT if active else theme.SECONDARY_BG,
                             text_color=theme.TEXT_ON_ACCENT if active else theme.TEXT_PRIMARY)
        if mode == "register":
            self.name_field.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        else:
            self.name_field.grid_remove()
        self._render_buttons()

    def submit_account(self) -> None:
        if "account" in self.busy or self.view is None or self.view.state == "unconfigured":
            return
        email, password = self.email_field.get(), self.password_field.get()
        name = self.name_field.get()
        for field_widget in (self.email_field, self.password_field, self.name_field):
            field_widget.set_error(None)
        self.password_field.variable.set("")  # never kept, whatever happens next
        if self.mode == "register":
            self._run("account", lambda: self.controller.register(email, password, display_name=name),
                      lambda result: self._registered(result, email), (self.submit_button,))
        else:
            self._run("account", lambda: self.controller.sign_in(email, password), self._signed_in,
                      (self.submit_button,))

    def _field_errors(self, result: ControllerResult) -> None:
        if isinstance(result.cause, InvalidInput):
            fields = {"email": self.email_field, "password": self.password_field, "display_name": self.name_field}
            for name, message in result.cause.errors.items():
                fields[name].set_error(message)
        self.account_notice.show("error", result.error)

    def _registered(self, result: ControllerResult, email: str) -> None:
        if not result.ok:
            self._field_errors(result)
            return
        self.set_mode("sign_in")
        self.email_field.variable.set(email)
        self.account_notice.show("success", "Account created. Sign in with it now.")

    def _signed_in(self, result: ControllerResult) -> None:
        if not result.ok:
            self._field_errors(result)
            return
        outcome = result.value
        note = ("Signed in. Your schedule is read from the database." if self.direct else
                "Signed in. Nothing on this device was uploaded or claimed.")
        if outcome.unassociated:
            note += f" {outcome.unassociated} record(s) without an account are listed below if you want them here."
        self.account_notice.show("success", note)
        self._on_workspace_changed()
        self.refresh()
        self._load_profile()

    def _load_profile(self) -> None:
        identity = (self.view.backend_url, self.view.signed_in_email)

        def done(result: ControllerResult[dict]) -> None:
            if self.view.state != "signed_in" or identity != (self.view.backend_url, self.view.signed_in_email):
                return
            if result.ok:
                self.profile_label.configure(text=profile_summary(result.value))
            else:
                self.profile_label.configure(text=f"Profile unavailable: {result.error}")

        self._run("profile", self.controller.profile, done)

    def sign_out(self) -> None:
        def done(result: ControllerResult[ConnectionView]) -> None:
            if not result.ok:
                self.account_notice.show("error", result.error)
                return
            self.account_notice.show("info", "Signed out. Your records stay in the database; sign in again to use "
                                             "them." if self.direct else "Signed out. Your records stay on this device.")
            self._on_workspace_changed()
            self.render(result.value)

        self._run("account", self.controller.sign_out, done, (self.sign_out_button,))

    # ----------------------------------------------------------------- association

    def load_preview(self) -> None:
        def done(result: ControllerResult[AssociationPreview]) -> None:
            if not result.ok:
                self.preview = None
                self.association_label.configure(text=f"Error: {result.error}")
                return
            self.preview = preview = result.value
            if not preview.total:
                self.association_label.configure(text="There are no records without an account on this device.")
                return
            parts = [f"{count} {ENTITY_LABELS.get(kind, kind).lower()}(s)"
                     for kind, count in sorted(preview.live_counts.items()) if count]
            deleted = preview.total - sum(preview.live_counts.values())
            text = f"{preview.total} record(s) on this device have no account: " + ", ".join(parts or ["none live"])
            if deleted:
                text += f" (plus {deleted} deleted record(s))"
            if preview.problems:
                text += "\n\nCannot be associated as they are:\n" + "\n".join(
                    f"- {problem.get('message') or problem}" for problem in preview.problems)
            self.association_label.configure(text=text)

        self._run("preview", self.controller.association_preview, done, (self.preview_button,))

    def review_association(self) -> None:
        if self.preview is None or not self.preview.total or self.preview.problems:
            return
        preview = self.preview
        self.dialog = ConfirmDialog(
            self, title="Associate local records",
            message=(f"Make these {preview.total} record(s) part of {masked_email(self.view.signed_in_email)}'s account? "
                     "They keep their ids and history and are synchronized from now on. This cannot be undone from the app."),
            confirm_text="Associate", on_result=lambda confirmed: self._association_confirmed(confirmed, preview.token),
        ).present()

    def _association_confirmed(self, confirmed: bool, token: str) -> None:
        self.dialog = None
        if not confirmed:
            self.association_notice.show("info", "Nothing was changed.")
            return

        def done(result: ControllerResult[dict[str, int]]) -> None:
            if not result.ok:
                self.association_notice.show("error", result.error)
                if isinstance(result.cause, AssociationError):
                    self.load_preview()  # a stale preview: show the current one to review again
                return
            self.association_notice.show("success", f"Associated {sum(result.value.values())} record(s).")
            self._on_workspace_changed()
            self.refresh()

        self._run("associate", lambda: self.controller.associate(token), done, (self.associate_button,))

    # ----------------------------------------------------------------- sync and conflicts

    def sync_now(self) -> None:
        def done(result: ControllerResult) -> None:
            if not result.ok:
                self.sync_notice.show("error", result.error)
            else:
                report = result.value
                tone = {"ok": "success", "offline": "warning", "auth_required": "warning"}.get(report.status, "error")
                text = {"ok": f"Synchronized: sent {report.pushed}, received {report.pulled}.",
                        "offline": "The backend could not be reached; your changes wait and are retried.",
                        "auth_required": "Your session ended; sign in again to synchronize.",
                        "inert": "Sign in to synchronize."}.get(report.status, report.message)
                if report.conflicts:
                    text += f" {report.conflicts} conflict(s) need a decision below."
                self.sync_notice.show(tone, text)
            self.refresh()

        self._run("sync", self.controller.sync_now, done, (self.sync_button,))

    def load_conflicts(self) -> None:
        def done(result: ControllerResult[list[ConflictView]]) -> None:
            if not result.ok:
                self.conflict_notice.show("error", result.error)
                return
            self.show_conflicts(result.value)

        self._run("conflicts", self.controller.conflicts, done)

    def show_conflicts(self, conflicts: list[ConflictView]) -> None:
        self.conflicts = conflicts
        selected = self.selected_conflict.id if self.selected_conflict else None
        self.conflict_tree.delete(*self.conflict_tree.get_children())
        for conflict in conflicts:
            self.conflict_tree.insert("", tk.END, iid=conflict.id, values=(conflict.title, conflict.kind))
        self.conflicts_empty.configure(text="No conflicts." if not conflicts else
                                       "Select a conflict to compare both versions.")
        if selected in {conflict.id for conflict in conflicts}:
            self.conflict_tree.selection_set(selected)
        else:
            self.selected_conflict = None
            self.detail_frame.grid_remove()

    def _conflict_selected(self) -> None:
        selection = self.conflict_tree.selection()
        conflict = next((c for c in self.conflicts if selection and c.id == selection[0]), None)
        self.selected_conflict = conflict
        if conflict is None:
            self.detail_frame.grid_remove()
            return
        dash = "—"
        versions = (f"This device: changed {conflict.local_updated_at or dash}, based on server version "
                    f"{conflict.base_version or dash}. Server: version {conflict.remote_version or dash}, changed "
                    f"{conflict.remote_updated_at or dash}.")
        title = f"{conflict.title} — {conflict.kind}\n{versions}"
        if conflict.remote_deleted:
            title += "\nThe server deleted this record."
        if conflict.server_message:
            title += f"\nServer: {conflict.server_message}"
        self.detail_title.configure(text=title)
        self.difference_tree.delete(*self.difference_tree.get_children())
        for index, difference in enumerate(conflict.differences):
            self.difference_tree.insert("", tk.END, iid=str(index),
                                        values=(difference.field, difference.local, difference.remote))
        reasons = []
        for choice, button in self.choice_buttons.items():
            reason = conflict.choices.get(choice, "Not available.")
            self._enable(button, reason is None and "resolve" not in self.busy)
            if reason:
                reasons.append(f"{RESOLUTION_LABELS[choice]} is not available: {reason}")
        self.choice_reasons.configure(text="\n".join(reasons))
        self.detail_frame.grid()

    def resolve(self, choice: str) -> None:
        conflict = self.selected_conflict
        if conflict is None or conflict.choices.get(choice) is not None:
            return

        def done(result: ControllerResult[ConflictView]) -> None:
            if not result.ok:
                self.conflict_notice.show("error", result.error)
            else:
                self.conflict_notice.show("success", f"Resolved: {RESOLUTION_LABELS[choice].lower()}.")
                self._on_workspace_changed(reload_only=True)
            self.refresh()

        self._run("resolve", lambda: self.controller.resolve(conflict.id, choice), done,
                  tuple(self.choice_buttons.values()))
