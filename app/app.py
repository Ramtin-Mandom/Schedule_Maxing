"""
The CustomTkinter desktop app of Schedule Maxing.

Run from the project root with:

    python -m app.app                                        # local SQLite storage (default; works offline)
    python -m app.app --storage postgres --env-file .env     # direct PostgreSQL storage (docs/direct-postgres.md)

Shell (Milestone 4, app/ui/shell.py): a left sidebar that starts collapsed
and opens with its hamburger button (or Ctrl+B; Ctrl+1..9 jump to a page),
and one page at a time: Day, Week and Month Schedule, Project Schedule,
Allocation Planning, Productivity, Settings, Account and About. The app
opens on Day. Execute stays on Day and Productivity has its own page.
Appearance (light/dark) and the interface size are saved beside the database
(app/ui/ui_settings.py). docs/desktop-layout.md describes the layout.

Data flow (Milestone 2): every page is backed by the application's SQLite
database. A widget callback calls a Tk-free presenter
(app/ui/schedule_page_controller.py), which goes through PlanningController
-> PlanningService -> repository -> SQLite; the page is then redrawn from a
fresh read of what was committed. There is no widget-owned task collection.

- Startup opens the one shared database connection (app/ui/app_services.py).
  If it cannot be opened, the app shows the error instead of a scheduler
  whose edits would never be saved.
- Tasks and fixed blocks are created, edited, and deleted by UUID; the task
  list is keyboard-operable (Enter edits, Delete removes, the Menu key or
  Shift+F10 opens its actions), so nothing depends on clicking the canvas.
- Day (app/ui/day_page.py, docs/desktop-day.md) shows one date's timeline
  and makes its schedule through the shared workflow; Week and Month
  (app/ui/calendar_page.py) are real calendar periods in the planning
  timezone whose days open on Day. Greedy Optimizer v1
  (app.optimizer.optimize_day_schedule) remains the legacy/CLI baseline and
  is unchanged.
- Layout is responsive (wide, medium and narrow) and is re-laid out only
  when that mode changes, so moving/resizing the window stays smooth.
- Closing waits for background work, then closes the database.
- Direct PostgreSQL storage (--storage postgres or SCHEDULE_MAXING_STORAGE=postgres,
  app/ui/direct_services.py) replaces the local database with the server's
  schema: the Account page signs in directly (no HTTP, no JWT), every page
  works on that account's records, and there is no synchronization, local
  copy or silent fallback. DATABASE_URL alone never selects it.
"""

from __future__ import annotations

import argparse
import os
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable
from zoneinfo import ZoneInfo

try:
    import customtkinter as ctk
except ImportError as error:  # pragma: no cover - runtime dependency message
    raise ImportError(
        "This redesigned UI uses CustomTkinter. Install it with: "
        "pip install customtkinter"
    ) from error

from app.ui.paint_widgets import AppScrollableFrame
from app.execution.db import resolve_db_path
from app.sync.transport import HttpTransport, SyncTransport
from app.ui import theme
from app.ui.account_controller import AccountController, ConnectionView
from app.ui.account_page import AccountPage
from app.ui.app_services import AppServices, describe_startup_failure, open_app_services
from app.ui.direct_services import (
    DirectAccountController,
    DirectAppServices,
    describe_direct_startup_failure,
    open_direct_app_services,
)
from app.ui.background import run_in_background
from app.ui.day_controller import DayScheduleController
from app.ui.calendar_controller import CalendarController
from app.ui.calendar_page import CalendarPage
from app.ui.allocation_controller import AllocationController
from app.ui.projects_controller import ProjectsController
from app.ui.planning_pages import AllocationPage, ProjectsPage
from app.ui.day_page import DaySchedulePage
from app.ui.components import (
    AppButton,
    Card,
    LabeledEntry,
    font,
)
from app.ui.execution_controller import ExecutionController
from app.ui.guide_page import GuidePage
from app.ui.pages import PageHeader, PlaceholderPage, ScrollPage, SettingsPage
from app.ui.productivity_controller import ProductivityController
from app.ui.productivity_page import ProductivityPage
from app.ui.shell import AppShell
from app.ui.shell_state import ShellState
from app.ui.ui_settings import UISettings, UISettingsStore, settings_path_for
from app.ui.settings_controller import SettingsController, TaskDataResetController
from app.ui.tk_lifecycle import DesktopCollection, release_resources
from config import settings

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Kept for callers of the previous module constants; colors now live in app/ui/theme.py.
APP_BG, CARD_BG, CARD_BORDER = theme.APP_BG, theme.CARD_BG, theme.CARD_BORDER
TEXT_PRIMARY, TEXT_MUTED = theme.TEXT_PRIMARY, theme.TEXT_MUTED
ACCENT, ACCENT_HOVER, DANGER, DANGER_HOVER = theme.ACCENT, theme.ACCENT_HOVER, theme.DANGER, theme.DANGER_HOVER
SUCCESS, WARNING = theme.SUCCESS, theme.WARNING

#: The schedule pages, in navigation order (Week and Month are real calendar periods, not day counts).
SCHEDULE_PAGES = ("day", "week", "month")


# -----------------------------------------------------------------------------
# Reward Config Page (legacy compatibility class; not exposed by the desktop shell)
# -----------------------------------------------------------------------------


class RewardConfigPage(ctk.CTkFrame):
    """Runtime reward configuration of the legacy Greedy Optimizer v1 (unchanged behavior)."""

    REWARD_FIELDS = [
        "WEIGHT_PRIORITY",
        "WEIGHT_PREFERENCE_TIME",
        "WEIGHT_TAG_RELATION",
        "WEIGHT_SPACING",
        "WEIGHT_NO_BREAK_PENALTY",
        "PREFERENCE_TIME_DISTANCE_SCALE",
        "TAG_RELATION_MAX_GAP",
        "MIN_GOOD_BREAK",
        "MAX_GOOD_BREAK",
        "BACK_TO_BACK_GAP",
        "INITIAL_TEMPERATURE",
        "MIN_TEMPERATURE",
        "COOLING_RATE",
        "MAX_ITERATIONS",
        "NO_IMPROVEMENT_LIMIT",
    ]

    def __init__(self, parent: tk.Widget, on_back: Callable[[], None] | None = None) -> None:
        super().__init__(parent, fg_color=theme.APP_BG, corner_radius=0)
        self.vars: dict[str, tk.StringVar] = {}
        self._on_back = on_back
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build()

    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=theme.SPACE_XL, pady=(22, 12))
        header.columnconfigure(0, weight=1)
        PageHeader(header, "Reward Config (legacy)",
                   "These values are applied at runtime only and affect the legacy Greedy Optimizer v1 (CLI "
                   "baseline). The desktop scheduler reads config/task_preference.yaml instead.",
                   ).grid(row=0, column=0, sticky="ew")
        if self._on_back is not None:
            AppButton(header, "Back to Settings", self._on_back, variant="secondary").grid(row=0, column=1, sticky="ne")

        body = Card(self)
        body.grid(row=1, column=0, sticky="nsew", padx=theme.SPACE_XL, pady=(0, theme.SPACE_XL))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        scroll = AppScrollableFrame(body, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        scroll.columnconfigure((0, 1), weight=1)
        for index, field_name in enumerate(self.REWARD_FIELDS):
            var = tk.StringVar(value=str(getattr(settings, field_name)))
            self.vars[field_name] = var
            entry = LabeledEntry(scroll, field_name, var)
            entry.grid(row=index // 2, column=index % 2, sticky="ew", padx=6, pady=6)

        button_bar = ctk.CTkFrame(body, fg_color="transparent")
        button_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        button_bar.columnconfigure((0, 1), weight=1)
        AppButton(button_bar, "Apply Runtime Config", self.apply_config, height=42).grid(row=0, column=0, sticky="ew",
                                                                                          padx=(0, 6))
        AppButton(button_bar, "Reload From settings.py", self.reload_config, variant="secondary", height=42).grid(
            row=0, column=1, sticky="ew", padx=(6, 0))

    def apply_config(self) -> None:
        try:
            for field_name, var in self.vars.items():
                current_value = getattr(settings, field_name)
                raw_value = var.get().strip()
                if isinstance(current_value, int) and not isinstance(current_value, bool):
                    new_value = int(raw_value)
                elif isinstance(current_value, float):
                    new_value = float(raw_value)
                else:
                    new_value = raw_value
                setattr(settings, field_name, new_value)
                self._set_if_exists("app.reward", field_name, new_value)
                self._set_if_exists("app.optimizer", field_name, new_value)
        except ValueError as error:
            messagebox.showerror("Invalid Config", str(error))
            return
        messagebox.showinfo("Config Applied", "Reward config updated for this app session.")

    def reload_config(self) -> None:
        for field_name, var in self.vars.items():
            var.set(str(getattr(settings, field_name)))

    def _set_if_exists(self, module_name: str, field_name: str, value: object) -> None:
        module = __import__(module_name, fromlist=[field_name])
        if hasattr(module, field_name):
            setattr(module, field_name, value)


# -----------------------------------------------------------------------------
# Application Shell
# -----------------------------------------------------------------------------

_PLACEHOLDERS = {
    "about": ("About", "Schedule Maxing is a personal scheduling and productivity project created by Ramtin Rezaei "
                        "to experiment with intelligent scheduling and productivity tools."),
}


class ScheduleOptimizerApp(ctk.CTk):
    def __init__(
        self,
        *,
        db_path: str | None = None,
        timezone: str | None = None,
        project_root: str | None = None,
        today: date | None = None,
        ui_settings_path: str | Path | None = None,
        transport_factory: Callable[[str], SyncTransport] = HttpTransport,
        background_sync: bool = True,
        storage: str | None = None,
        env_file: str | None = None,
        direct_backend=None,
    ) -> None:
        # "local" unless chosen explicitly (argument or SCHEDULE_MAXING_STORAGE); DATABASE_URL alone never switches.
        self.storage = storage or settings.resolve_storage_mode()
        self.ui_store = UISettingsStore(ui_settings_path or settings_path_for(resolve_db_path(db_path)))
        self.ui_settings: UISettings = self.ui_store.load()
        ctk.set_appearance_mode(self.ui_settings.appearance)
        ctk.set_widget_scaling(self.ui_settings.ui_scale)
        super().__init__()
        self._collection = DesktopCollection(self)
        ctk.set_default_color_theme("blue")

        self.title("Schedule Maxing")
        width = min(1440, int(self.winfo_screenwidth() * 0.92))
        height = min(900, int(self.winfo_screenheight() * 0.88))
        self.geometry(f"{width}x{height}")
        # Only a floor for sanity: small windows switch to the narrow layout instead of clipping.
        self.minsize(520, 440)
        self.configure(fg_color=theme.APP_BG)

        self.services: AppServices | DirectAppServices | None = None
        self.startup_error: str | None = None
        self.shell: AppShell | None = None
        self.shell_state = ShellState()
        self._today_override = today
        self._transport_factory = transport_factory
        self._background_sync = background_sync
        self.account_controller: AccountController | DirectAccountController | None = None
        self._status_poll = None
        self._seen_report = None
        self._closing = False

        self._configure_treeview_style()
        try:
            if self.direct:
                self.services = open_direct_app_services(env_file=env_file, timezone=timezone,
                                                         project_root=project_root, backend=direct_backend)
            else:
                self.services = open_app_services(db_path, timezone=timezone, project_root=project_root,
                                                  transport_factory=transport_factory, background_sync=background_sync)
        except Exception as error:  # noqa: BLE001 - reported to the user; no scheduler without storage
            self.startup_error = (describe_direct_startup_failure(error) if self.direct
                                  else describe_startup_failure(error, db_path))

        if self.services is None:
            self._build_startup_error()
            messagebox.showerror("Database Unavailable", self.startup_error, parent=self)
        else:
            self._build_shell()
            # Direct storage starts signed out: the Account page first, while the database is checked off the Tk thread.
            self.show_page("account" if self.direct else "day")
            if self.direct:
                self._check_direct_database()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- pages and services ---------------------------------------------------------------

    @property
    def direct(self) -> bool:
        """True when this window stores its records directly in PostgreSQL (not the local database)."""
        return self.storage == "postgres"

    @property
    def pages(self) -> dict[str, ctk.CTkFrame]:
        return self.shell.pages if self.shell is not None else {}

    @property
    def execution_controller(self) -> ExecutionController | None:
        return self.services.execution_controller if self.services is not None else None

    @property
    def productivity_controller(self) -> ProductivityController | None:
        return self.services.productivity_controller if self.services is not None else None

    def show_page(self, page_name: str) -> None:
        if self.shell is not None:
            self.shell.show_page(page_name)

    def _navigated(self, page_name: str) -> None:
        """A page chosen from the navigation: Day Schedule always opens on today."""
        if page_name == "day":
            page = self.pages.get("day")
            if page is not None:
                page.show_today()

    def today(self) -> date:
        """Today's real date in the planning timezone (a fixed date when the app was opened with one)."""
        if self._today_override is not None:
            return self._today_override
        zone = self.services.timezone if self.services is not None else settings.DEFAULT_TIMEZONE
        return datetime.now(ZoneInfo(zone)).date()

    def open_day(self, day: date, *, return_to: str | None = None) -> None:
        """Show `day` on the Day page; from Week/Month, remember the way back to that page and its dates."""
        page = self.pages.get("day")
        if page is None:
            return
        context = None
        if return_to is not None and return_to in self.pages:
            source = self.pages[return_to]
            controller = getattr(source, "page_controller", getattr(source, "controller", None))
            context = (return_to, getattr(controller, "selected_date", day))
        page.page_controller.allocation_context = None
        if return_to == "allocation":
            preview = self.pages["allocation"].preview
            if preview is not None:
                page.page_controller.allocation_context = (preview.period.start, preview.period.end, preview.fingerprint, day)
        self.shell_state.remember("day_return", context)
        page.open_date(day, return_to=context)
        self.show_page("day")

    def reset_task_data(self, done: Callable) -> None:
        """
        Settings' "Reset All Task Data" (already confirmed): in a worker, the
        services reset the server first and this device only after it
        succeeded (TaskDataResetController). On success every page re-reads
        its now empty data; on failure nothing changed and done() shows why.
        """
        services = self.services

        def finished(result) -> None:
            if result.ok:
                self._task_data_was_reset()
            done(result)

        run_in_background(self, TaskDataResetController(services).reset, finished)

    def _task_data_was_reset(self) -> None:
        for key in ("day", "week", "month", "projects", "allocation", "productivity"):
            page = self.pages.get(key)
            if page is not None and hasattr(page, "on_show") and not getattr(page, "_busy", False):
                page.on_show()  # re-read now: nothing stale stays on any page
        self.refresh_status()

    def close_services(self) -> None:
        """Wait for background work, then close the database (idempotent)."""
        if self.services is not None:
            self.services.close()

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._status_poll is not None:
            try:
                self.after_cancel(self._status_poll)
            except tk.TclError:
                pass
            self._status_poll = None
        self._finish_close(timeout=10.0)

    def _finish_close(self, timeout: float = 0.0) -> None:
        if self.services is not None and not self.services.close(timeout=timeout):
            # Keep Tcl and SQLite alive for outstanding workers; retry without blocking Tk.
            self.withdraw()
            self.after(100, self._finish_close)
            return
        self.destroy()
        release_resources(self)
        self._collection.close()

    # -- workspace and synchronization --------------------------------------------------

    #: How often the status bar re-reads the (local, durable) sync status.
    STATUS_POLL_MS = 4000

    def apply_workspace(self, *, reload_only: bool = False) -> None:
        """
        After sign-in/out, association or a backend switch: work in the
        workspace SyncService now names, with fresh controllers and rebuilt
        schedule/productivity pages (each keeps its date). Results of work
        started for the previous workspace are dropped (AppServices.workspace_guard).
        With reload_only, just re-read the visible page (e.g. after a conflict was resolved).
        """
        if self.services is None or self.shell is None:
            return
        if not reload_only:
            self.services.switch_workspace()
            self._build_workspace_pages(replace=True)
            self.shell.sidebar.set_footer(self.services.workspace_label())
        else:
            self._reload_current_page()
        self.refresh_status()

    def _reload_current_page(self) -> None:
        page = self.shell.pages.get(self.shell.current) if self.shell and self.shell.current else None
        if page is not None and hasattr(page, "on_show") and not getattr(page, "_busy", False):
            page.on_show()

    def refresh_status(self) -> ConnectionView | None:
        """Update the status bar from the durable sync status; re-read the visible page after remote changes."""
        if self.account_controller is None or self.shell is None:
            return None
        result = self.account_controller.connection()
        if not result.ok:
            self.shell.status_bar.show(f"Sync status unavailable: {result.error}", can_sync=False, syncing=False)
            return None
        view = result.value
        if self.direct:  # no synchronization: PostgreSQL is the one copy
            self.shell.status_bar.show(view.headline, can_sync=False, syncing=False)
            return view
        text = view.headline if view.state == "unconfigured" else f"{view.headline} · last sync {view.last_success_text}"
        self.shell.status_bar.show(text, can_sync=view.state == "signed_in", syncing=view.in_progress)
        report = self.services.sync_service.last_report
        if report is not self._seen_report:
            self._seen_report = report
            if report.pulled:  # records changed remotely: the visible page re-reads them
                self._reload_current_page()
        return view

    def _poll_status(self) -> None:
        self._status_poll = None
        if self.services is None or self.services.closed:
            return
        self.refresh_status()
        self._status_poll = self.after(self.STATUS_POLL_MS, self._poll_status)

    def sync_now(self) -> None:
        """The status bar's Sync now (the same SyncService.sync_now the Account page uses; one at a time)."""
        if self.account_controller is None or self.direct:
            return
        self.shell.status_bar.show("Synchronizing...", can_sync=False, syncing=True)
        run_in_background(self, self.account_controller.sync_now, lambda _result: self.refresh_status(),
                          still_current=lambda: True)

    # -- appearance -----------------------------------------------------------------------

    def set_appearance(self, appearance: str) -> bool:
        """Switch light/dark now and remember it; False if it could not be saved (it still applies)."""
        self.ui_settings = self.ui_settings.with_appearance(appearance)
        ctk.set_appearance_mode(appearance)
        if self.shell is not None and "settings" in self.pages:
            self.pages["settings"].appearance_select.variable.set(appearance.title())
        self._configure_treeview_style()
        if self.shell is not None:
            self.shell.appearance_changed()
        return self.ui_store.save(self.ui_settings)

    def set_ui_scale(self, ui_scale: float) -> bool:
        """Change the interface size now and remember it; False if it could not be saved."""
        self.ui_settings = self.ui_settings.with_scale(ui_scale)
        ctk.set_widget_scaling(ui_scale)
        self._configure_treeview_style()
        if self.shell is not None:
            self.shell.layout_checks.request()  # the same window now holds fewer logical pixels
        return self.ui_store.save(self.ui_settings)

    def _configure_treeview_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        scale = self.ui_settings.ui_scale
        card, subtle = theme.resolve(theme.CARD_BG), theme.resolve(theme.SUBTLE_BG)
        text, muted = theme.resolve(theme.TEXT_PRIMARY), theme.resolve(theme.TEXT_MUTED)
        style.configure("Treeview", background=card, foreground=text, fieldbackground=card, borderwidth=0,
                        rowheight=round(30 * scale), font=(theme.FONT_FAMILY, 10))
        style.configure("Treeview.Heading", background=subtle, foreground=muted, relief="flat",
                        font=(theme.FONT_FAMILY, 9, "bold"), padding=(8, 8))
        style.map("Treeview", background=[("selected", theme.resolve(theme.ACCENT_SOFT))],
                  foreground=[("selected", text)])
        border = theme.resolve(theme.CARD_BORDER)
        for name in ("TScrollbar", "Horizontal.TScrollbar", "Vertical.TScrollbar"):
            style.configure(name, background=subtle, troughcolor=card, bordercolor=border, lightcolor=subtle,
                            darkcolor=subtle, arrowcolor=muted)

    # -- building -------------------------------------------------------------------------

    def _build_startup_error(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        panel = Card(self)
        panel.grid(row=0, column=0, padx=40, pady=40, sticky="nsew")
        ctk.CTkLabel(panel, text="Database Unavailable", font=font(theme.SIZE_TITLE, "bold"),
                     text_color=theme.DANGER, anchor="w").pack(anchor="w", padx=28, pady=(28, 12))
        ctk.CTkLabel(panel, text=self.startup_error, font=font(theme.SIZE_BODY), text_color=theme.TEXT_PRIMARY,
                     anchor="w", justify="left", wraplength=900).pack(anchor="w", padx=28, pady=(0, 28))

    def _build_shell(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        services = self.services
        self.shell = shell = AppShell(self, self.shell_state, scaling=lambda: self.ui_settings.ui_scale)
        shell.grid(row=0, column=0, sticky="nsew")
        shell.on_navigate = self._navigated
        shell.sidebar.set_footer(services.location_label())
        host = shell.host

        self._build_workspace_pages(replace=False)
        if self.direct:
            self.account_controller = DirectAccountController(services)
            shell.status_bar.sync_button.grid_remove()  # nothing to synchronize in direct storage
        else:
            self.account_controller = AccountController(services.sync_service, transport_factory=self._transport_factory,
                                                        background_sync=self._background_sync)
        shell.add_page("account", AccountPage(host, self.account_controller, on_workspace_changed=self.apply_workspace))
        shell.status_bar.sync_button.configure(command=self.sync_now)
        shell.status_bar.account_button.configure(command=lambda: self.show_page("account"))
        links = [("Open Day Schedule", lambda: self.show_page("day")),
                 ("Open Productivity", lambda: self.show_page("productivity"))]
        shell.add_page("guide", GuidePage(host))
        for key, (title, message) in _PLACEHOLDERS.items():
            shell.add_page(key, PlaceholderPage(host, title, message, links))
        self.refresh_status()
        self._status_poll = self.after(self.STATUS_POLL_MS, self._poll_status)

    def _check_direct_database(self) -> None:
        """Check the direct database's connection and schema revision in a worker; show the outcome."""

        def done(result) -> None:
            self.refresh_status()
            page = self.pages.get("account")
            if page is not None and result.ok:
                page.database_checked(result.value)

        run_in_background(self, self.account_controller.check_backend, done, still_current=lambda: True)

    def _build_workspace_pages(self, *, replace: bool) -> None:
        """The pages bound to the workspace's controllers (each schedule page keeps its remembered date)."""
        services, shell = self.services, self.shell
        add = shell.replace_page if replace else shell.add_page
        today = self.today()
        for mode_name in SCHEDULE_PAGES:
            remembered = self.shell_state.selection(mode_name)
            if mode_name == "day":
                add(mode_name, DaySchedulePage(
                    shell.host, DayScheduleController(services.planning_controller, anchor_date=remembered or today,
                                                      timezone=services.timezone, today=self.today),
                    services.execution_controller, services.productivity_controller,
                    on_anchor_changed=self.shell_state.remember, on_return=self.show_page,
                    return_context=self.shell_state.selection("day_return"), background_io=self.direct,
                ))
                continue
            add(mode_name, CalendarPage(
                shell.host, mode_name,
                CalendarController(services.planning_controller, mode=mode_name, selected=remembered or today,
                                   timezone=services.timezone, today=self.today,
                                   executions=services.execution_controller),
                services.productivity_controller, on_anchor_changed=self.shell_state.remember,
                on_open_day=lambda day, mode_name=mode_name: self.open_day(day, return_to=mode_name),
                background_io=self.direct,
            ))
        add("productivity", ScrollPage(
            shell.host, lambda parent: ProductivityPage(parent, services.productivity_controller)))
        add("projects", ProjectsPage(
            shell.host, ProjectsController(services.planning_controller, timezone=services.timezone),
            on_open_day=lambda day: self.open_day(day, return_to="projects")))
        add("allocation", AllocationPage(
            shell.host, AllocationController(services.planning_controller, timezone=services.timezone, selected=today),
            on_open_day=lambda day: self.open_day(day, return_to="allocation")))
        add("settings", SettingsPage(
            shell.host, self.ui_settings, on_appearance=self.set_appearance, on_scale=self.set_ui_scale,
            controller=SettingsController(services.planning_controller, today=self.today), background_io=self.direct,
            on_reset_task_data=self.reset_task_data))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.app", description="The Schedule Maxing desktop app.")
    parser.add_argument("--storage", choices=settings.STORAGE_MODES, default=None,
                        help=f"local (default; the SQLite database, works offline) or postgres (direct PostgreSQL "
                             f"storage, docs/direct-postgres.md). Default: {settings.STORAGE_ENV_VAR}, else local.")
    parser.add_argument("--env-file", metavar="PATH", default=None,
                        help=f"with --storage postgres: read DATABASE_URL from this file (the environment still "
                             f"wins). Default: {settings.ENV_FILE_ENV_VAR}, else the environment only.")
    args = parser.parse_args(argv)
    try:
        storage = args.storage or settings.resolve_storage_mode()
    except ValueError as error:
        parser.error(str(error))
    env_file = args.env_file or os.environ.get(settings.ENV_FILE_ENV_VAR) or None
    if env_file is not None and storage != "postgres":
        parser.error("--env-file is only used with --storage postgres (local storage reads no env file).")
    app = ScheduleOptimizerApp(storage=storage, env_file=env_file)
    app.mainloop()


if __name__ == "__main__":
    main()
