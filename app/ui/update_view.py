"""
app/ui/update_view.py

The update notice and the Settings "Updates" section (docs/windows-distribution.md, "Updates").

UpdateFlow connects the Tk-free UpdateController (app/ui/update_controller.py)
to the window. Nothing here blocks the interface: checking, downloading and
starting the installer run in worker threads (app.ui.background), and their
results come back on the Tk thread.

    automatic check (a few seconds after start, at most once a day)
        newer version -> UpdateBanner: "Update now" / "Later" / "Skip this version"
        anything else -> nothing is shown (failures are only logged)
    "Update now" -> download with progress (can be cancelled) -> verify
                 -> start the installer -> close the application normally
    any failure  -> a short message; the installed version keeps running

The banner floats over the top-right corner of the window and never takes
the focus or stops other work.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

import customtkinter as ctk

from app.ui import theme
from app.ui.background import run_in_background
from app.ui.components import AppButton, Card, Notice, SectionTitle, font
from app.ui.ui_settings import UISettings
from app.ui.update_controller import (
    UpdateController,
    UpdateView,
    automatic_check_due,
    megabytes,
    with_automatic_checks,
    with_check_recorded,
    with_skipped_version,
)

#: How long after the window opens the automatic check starts (the first paint and first sync come first).
AUTOMATIC_CHECK_DELAY_MS = 4000
PROGRESS_POLL_MS = 200


class UpdateBanner(Card):
    """The floating notice: a message and up to three actions."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.message = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=360,
                                    font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY)
        self.message.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_L, pady=(theme.SPACE_M, 6))
        self.buttons = ctk.CTkFrame(self, fg_color="transparent")
        self.buttons.grid(row=1, column=0, sticky="w", padx=theme.SPACE_L, pady=(0, theme.SPACE_M))
        self.shown = False

    def show(self, message: str, actions: list[tuple[str, Callable[[], None], str]]) -> None:
        self.message.configure(text=message)
        for child in self.buttons.winfo_children():
            child.destroy()
        for column, (label, command, variant) in enumerate(actions):
            AppButton(self.buttons, label, command, variant=variant, height=32).grid(row=0, column=column, padx=(0, 8))
        if not self.shown:
            self.place(relx=1.0, rely=0.0, anchor="ne", x=-theme.SPACE_L, y=theme.SPACE_L)
            self.lift()
            self.shown = True

    def set_message(self, message: str) -> None:
        self.message.configure(text=message)

    def hide(self) -> None:
        if self.shown:
            self.place_forget()
            self.shown = False


class UpdateFlow:
    def __init__(self, root, controller: UpdateController, *, get_settings: Callable[[], UISettings],
                 save_settings: Callable[[UISettings], bool], close_app: Callable[[], None],
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.root, self.controller = root, controller
        self._get, self._save, self._close_app, self._now = get_settings, save_settings, close_app, now
        self.banner = UpdateBanner(root)
        self.checking = False
        self.updating = False
        self._progress = (0, 0)

    @property
    def automatic(self) -> bool:
        return self._get().check_for_updates

    def set_automatic(self, enabled: bool) -> bool:
        return self._save(with_automatic_checks(self._get(), enabled))

    # -- checking ------------------------------------------------------------------------------

    def schedule_automatic_check(self) -> None:
        """Called once by the production entry point after the window exists (app/desktop.py)."""
        self.root.after(AUTOMATIC_CHECK_DELAY_MS, self._automatic_check)

    def _automatic_check(self) -> None:
        if automatic_check_due(self._get(), self._now(), enabled=self.controller.enabled):
            self.check(manual=False)

    def check(self, *, manual: bool, done: Callable[[UpdateView | None], None] | None = None) -> bool:
        """Start a check in a worker; `done` gets its view (None if the worker failed). False if one is running."""
        if self.checking or self.updating:
            return False
        self.checking = True
        skipped = None if manual else self._get().skipped_version

        def finished(result) -> None:
            self.checking = False
            view = result.value if result.ok else None
            if not manual and view is not None and view.state != "unavailable":
                self._save(with_check_recorded(self._get(), self._now()))  # a failed check is tried again next start
            if view is not None and view.offer:
                self.offer(view)
            if done is not None:
                done(view)

        if not run_in_background(self.root, lambda: self.controller.check(skipped_version=skipped), finished,
                                 still_current=lambda: True):
            self.checking = False
            return False
        return True

    # -- offering and installing ---------------------------------------------------------------

    def offer(self, view: UpdateView) -> None:
        release = view.release
        actions = [("Later", self.banner.hide, "secondary"),
                   ("Skip this version", lambda: self.skip(release.version), "ghost")]
        if self.controller.can_install:
            actions.insert(0, ("Update now", lambda: self.update_now(view), "primary"))
            note = "\nUpdating closes the app, installs the new version and opens it again. Windows may ask for permission."
        else:
            note = "\nThis copy runs from source code; update it with git."
        self.banner.show(view.message + note, actions)

    def skip(self, version: str) -> None:
        self._save(with_skipped_version(self._get(), version))
        self.banner.hide()

    def update_now(self, view: UpdateView) -> None:
        if self.updating:
            return
        self.updating = True
        release = view.release
        self._progress = (0, release.installer_size)
        self.banner.show(f"Downloading Schedule Maxing {release.version}…",
                         [("Cancel", self.controller.cancel_download, "secondary")])
        self._poll_progress(release.version)

        def progress(written: int, total: int) -> None:  # worker thread: only stores numbers
            self._progress = (written, total)

        def downloaded(result) -> None:
            if not result.ok:
                self._failed(result.error)
                return
            self.banner.show(f"Schedule Maxing {release.version} was downloaded and verified. Starting the installer…", [])
            run_in_background(self.root, lambda: self.controller.install(result.value), self._installer_started,
                              still_current=lambda: True)

        if not run_in_background(self.root, lambda: self.controller.download(release, progress), downloaded,
                                 still_current=lambda: True):
            self._failed("The app is closing.")

    def _poll_progress(self, version: str) -> None:
        if not self.updating:
            return
        written, total = self._progress
        if written and total:
            self.banner.set_message(f"Downloading Schedule Maxing {version}… {megabytes(written)} of {megabytes(total)} "
                                    f"({100 * written // total}%)")
        self.root.after(PROGRESS_POLL_MS, lambda: self._poll_progress(version))

    def _installer_started(self, result) -> None:
        if not result.ok:
            self._failed(result.error)
            return
        self.banner.show("The installer is starting. Schedule Maxing closes now and opens again when it is done.", [])
        self.updating = False
        self.root.after(600, self._close_app)  # the normal shutdown: the database is closed cleanly first

    def _failed(self, message: str | None) -> None:
        self.updating = False
        self.banner.show(message or "The update did not finish. Nothing was changed.",
                         [("Dismiss", self.banner.hide, "secondary")])


class UpdatesCard(Card):
    """Settings > Updates: the automatic-check option and "Check now"."""

    def __init__(self, parent, flow: UpdateFlow) -> None:
        super().__init__(parent)
        self.flow = flow
        self.columnconfigure(0, weight=1)
        controller = flow.controller
        SectionTitle(self, "Updates", f"You have Schedule Maxing {controller.installed_version}. New versions are "
                     "downloaded from the project's published releases and installed only when you choose to.",
                     wraplength=440).grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
        self.automatic = ctk.BooleanVar(value=flow.automatic)
        self.automatic_box = ctk.CTkCheckBox(self, text="Automatically check for updates", variable=self.automatic,
                                             command=self._automatic_changed, text_color=theme.TEXT_PRIMARY,
                                             font=font(theme.SIZE_BODY))
        self.automatic_box.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))
        self.check_button = AppButton(self, "Check now", self.check_now, variant="secondary")
        self.check_button.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 8))
        self.notice = Notice(self, wraplength=450)
        self.notice.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 12))
        self.notice.hide()
        if not controller.enabled:
            self.automatic_box.configure(state="disabled")
            self.check_button.configure(state="disabled")
            self.notice.show("info", "Update checks are turned off for this installation.")

    def _automatic_changed(self) -> None:
        enabled = bool(self.automatic.get())
        if not self.flow.set_automatic(enabled):
            self.notice.show("warning", "The choice applies now, but it could not be saved (the settings file is not writable).")
        else:
            self.notice.show("success", "Automatic update checks are " + ("on." if enabled else "off."))

    def check_now(self) -> None:
        if not self.flow.check(manual=True, done=self._checked):
            return
        self.check_button.configure(state="disabled")
        self.notice.show("info", "Checking for updates…")

    def _checked(self, view: UpdateView | None) -> None:
        if not self.winfo_exists():
            return
        self.check_button.configure(state="normal")
        if view is None:
            self.notice.show("warning", "Could not check for updates. Nothing was changed.")
        elif view.offer:
            self.notice.show("info", view.message)
        elif view.state == "up_to_date":
            self.notice.show("success", view.message)
        else:
            self.notice.show("warning", view.message)
