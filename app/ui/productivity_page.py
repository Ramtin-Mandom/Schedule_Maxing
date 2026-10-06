"""
productivity_page.py

The desktop UI's Productivity page. The section bar at the top chooses one
of exactly three sections; each shows its figures as labelled boxes:

    General      the awards and the headline facts of the whole recorded
                 history (no filters)
    Specific     two parts, each with its own filters and report:
                 Task-based  period, category, tag and task type: the
                             selected type's boxes, and one box per type
                 Time-based  date range, weekday and planned start: the
                             totals, weekdays, weeks, months, one day, charts
    Project      the workspace's projects; selecting one shows the points it
                 collected in the chosen date range (total, per day, and the
                 average per day with its period) -- show_project(id) opens it
                 with a project already selected (the Projects page's link)

Each part has its own filters and its own report, so a filter of one
never changes another. The category filter offers the task form's categories
and the tag filter every tag used so far (on a task or in the history).

Nothing is calculated here. Figures come from ProductivityController's
tracker report and are worded by app/ui/tracker_view.py. Every read runs off
the Tk main thread (app/ui/background.py, which also drops a result of a
previous account or a destroyed page); each section's request carries a
number, so an answer to an older filter selection never replaces a newer
one. A section re-reads the persisted records when it is next shown.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from app.persistence.errors import NotSignedInError
from app.productivity.buckets import TimeBucket
from app.productivity.tracker import WEEKDAYS, TrackerFilters, TrackerReport
from app.ui import theme, tracker_view
from app.ui.background import ControllerResult, run_in_background
from app.ui.components import AppButton
from app.ui.paint_widgets import AppOptionMenu
from app.ui.productivity_charts import CompletionRateByBucketChart, PlannedVsActualChart
from app.ui.productivity_controller import ProductivityController
from app.ui.task_form_model import CATEGORIES
from app.ui.tracker_view import Stat

_DAY_OPTIONS = {"All time": None, "Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}
_PERIODS = dict(tracker_view.TYPE_PERIODS)
_NO_TYPE = "(no task type yet)"
_NO_DAY = "(no day in this selection)"
_ANY = "(any)"
_TIME_BUCKETS = [bucket.value for bucket in TimeBucket]
_CHART_GROUPS = {"By category": "category", "By task type": "type"}
_SECTION_LABELS = {name: name for name in tracker_view.SECTIONS}
_GENERAL, _TASKS, _TIME = tracker_view.REPORTS
_SPECIFIC, _PROJECT = tracker_view.SECTIONS[1:]
#: What each section reads: tracker reports (by key), and the Project section's own report.
_SECTION_PARTS = {_GENERAL: (_GENERAL,), _SPECIFIC: (_TASKS, _TIME), _PROJECT: (_PROJECT,)}
_NO_PROJECT_TITLE = "Select a project"
_TYPE_PAGE_SIZE = 8
_PAGE_WIDTH = 880


class _StatTile(ctk.CTkFrame):
    """One labelled box: the label, the value shown large, and a short caption."""

    def __init__(self, parent: tk.Widget, wraplength: int) -> None:
        super().__init__(parent, fg_color=theme.CARD_BG, corner_radius=14, border_color=theme.CARD_BORDER, border_width=1)
        self.title_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=12, weight="bold"),
                                        text_color=theme.TEXT_MUTED, anchor="w", justify="left", wraplength=wraplength)
        self.title_label.pack(anchor="w", padx=14, pady=(12, 0))
        self.value_label = ctk.CTkLabel(self, text="--", font=ctk.CTkFont(size=26, weight="bold"),
                                        text_color=theme.TEXT_PRIMARY, anchor="w", justify="left", wraplength=wraplength)
        self.value_label.pack(anchor="w", padx=14, pady=(2, 0))
        self.caption_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11), text_color=theme.TEXT_MUTED,
                                          anchor="w", justify="left", wraplength=wraplength)
        self.caption_label.pack(anchor="w", padx=14, pady=(0, 12))

    def set(self, stat: Stat) -> None:
        self.title_label.configure(text=stat.label)
        self.value_label.configure(text=stat.value)
        self.caption_label.configure(text=stat.caption)


class _TileGrid(ctk.CTkFrame):
    """A titled grid of stat boxes, `columns` to a row; says so when it has nothing to show."""

    def __init__(self, parent: tk.Widget, title: str, columns: int, *, empty: str = "Nothing to show yet.") -> None:
        super().__init__(parent, fg_color="transparent")
        self._columns = columns
        self.stats: list[Stat] = []
        self.tiles: list[_StatTile] = []
        self.columnconfigure(tuple(range(columns)), weight=1, uniform="tile")
        self.title_label = ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=16, weight="bold"),
                                        text_color=theme.TEXT_PRIMARY, anchor="w")
        self.title_label.grid(row=0, column=0, columnspan=columns, sticky="w", padx=5, pady=(0, 4))
        self.empty_label = ctk.CTkLabel(self, text=empty, text_color=theme.TEXT_MUTED, anchor="w")

    def set_title(self, title: str) -> None:
        self.title_label.configure(text=title)

    def values(self) -> dict[str, str]:
        return {stat.label: stat.value for stat in self.stats}

    def show(self, stats: list[Stat]) -> None:
        self.stats = list(stats)
        while len(self.tiles) < len(stats):
            self.tiles.append(_StatTile(self, _PAGE_WIDTH // self._columns - 40))
        for index, tile in enumerate(self.tiles):
            if index < len(stats):
                tile.set(stats[index])
                tile.grid(row=1 + index // self._columns, column=index % self._columns, sticky="nsew", padx=5, pady=5)
            else:
                tile.grid_remove()
        if stats:
            self.empty_label.grid_remove()
        else:
            self.empty_label.grid(row=1, column=0, columnspan=self._columns, sticky="w", padx=5, pady=5)


class ProductivityPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, productivity_controller: ProductivityController) -> None:
        super().__init__(parent, fg_color=theme.APP_BG)
        self._controller = productivity_controller
        #: Each section's own report, built for that section's filters.
        self.reports: dict[str, TrackerReport | None] = dict.fromkeys(tracker_view.REPORTS)
        #: The newest request of each part; an answer carrying an older number is dropped.
        self._requests = dict.fromkeys((*tracker_view.REPORTS, _PROJECT), 0)
        #: The Project section: the workspace's projects, the selected one and its report.
        self.projects: list = []
        self.project_id = None
        self.project_report = None
        #: Sections whose report must be read again before it is shown.
        self._stale: set[str] = set()
        self.section = _GENERAL
        self.type_page = 0
        self._type_keys: dict[str, str] = {}
        #: Every tag seen so far in this workspace; a tag stays offered once it has been used.
        self._known_tags: set[str] = set()

        self.columnconfigure(0, weight=1)
        self._build()
        self.show_section(self.section)
        self.refresh()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._build_section_bar()
        self.status_label = ctk.CTkLabel(self, text="", text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                                         wraplength=_PAGE_WIDTH)
        self.section_host = ctk.CTkFrame(self, fg_color="transparent")
        self.section_host.grid(row=2, column=0, sticky="ew", padx=15)
        self.section_host.columnconfigure(0, weight=1)
        specific = ctk.CTkFrame(self.section_host, fg_color="transparent")
        specific.columnconfigure(0, weight=1)
        #: The two parts of Specific, each with its own filters (kept by their report's name).
        self.part_frames = {_TASKS: self._build_task_based(specific), _TIME: self._build_time_based(specific)}
        for row, (name, frame) in enumerate(self.part_frames.items()):
            ctk.CTkLabel(specific, text=name, font=ctk.CTkFont(size=20, weight="bold"),
                         text_color=theme.TEXT_PRIMARY, anchor="w").grid(row=2 * row, column=0, sticky="w", padx=5,
                                                                         pady=(0 if row == 0 else 10, 8))
            frame.grid(row=2 * row + 1, column=0, sticky="ew")
        self.section_frames = {
            _GENERAL: self._build_general(self.section_host),
            _SPECIFIC: specific,
            _PROJECT: self._build_project(self.section_host),
        }

    def _card(self, parent: tk.Widget, title: str | None = None) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=theme.CARD_BG, corner_radius=16, border_color=theme.CARD_BORDER,
                            border_width=1)
        if title:
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=13, weight="bold"),
                         text_color=theme.TEXT_PRIMARY).pack(anchor="w", padx=14, pady=(12, 6))
        return card

    def _filter_card(self, parent: tk.Widget, columns: int, reset) -> tuple[ctk.CTkFrame, AppButton]:
        """A section's own filter row: room for `columns` menus, and its reset button."""
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="ew", padx=5, pady=(0, 14))
        card.columnconfigure(tuple(range(columns + 1)), weight=1, uniform="filter")
        button = AppButton(card, "Reset filters", reset, variant="secondary", height=30)
        button.grid(row=1, column=columns, sticky="ew", padx=10, pady=(0, 12))
        return card, button

    def _filter_menu(self, parent: tk.Widget, column: int, label: str, variable: tk.StringVar, values: list[str],
                     command) -> AppOptionMenu:
        ctk.CTkLabel(parent, text=label, text_color=theme.TEXT_MUTED, font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=0, column=column, sticky="w", padx=10, pady=(10, 2)
        )
        menu = AppOptionMenu(parent, variable=variable, values=values, command=lambda _value: command())
        menu.grid(row=1, column=column, sticky="ew", padx=10, pady=(0, 12))
        return menu

    def _build_section_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(18, 12))
        self.section_buttons: dict[str, AppButton] = {}
        for column, name in enumerate(tracker_view.SECTIONS):
            bar.columnconfigure(column, weight=1, uniform="section")
            button = AppButton(bar, name, lambda name=name: self.show_section(name), variant="secondary", height=40)
            button.grid(row=0, column=column, sticky="ew", padx=4)
            self.section_buttons[name] = button

    @staticmethod
    def _mark(buttons: dict, selected, labels: dict | None = None) -> None:
        """
        Show which of a row of choice buttons is selected: by colour and, where
        `labels` gives each button's plain text, by a leading mark as well
        (colour is never the only signal).
        """
        for key, button in buttons.items():
            chosen = key == selected
            button.configure(
                fg_color=theme.ACCENT if chosen else theme.SECONDARY_BG,
                hover_color=theme.ACCENT_HOVER if chosen else theme.SECONDARY_HOVER,
                text_color=theme.TEXT_ON_ACCENT if chosen else theme.TEXT_PRIMARY)
            if labels is not None:
                button.configure(text=("● " if chosen else "") + labels[key])

    def show_section(self, name: str) -> None:
        """Show one of the three sections (the others are hidden, not destroyed)."""
        if name not in self.section_frames:
            return
        self.section = name
        for key, frame in self.section_frames.items():
            if key == name:
                frame.grid(row=0, column=0, sticky="ew")
            else:
                frame.grid_remove()
        self._mark(self.section_buttons, name, _SECTION_LABELS)
        for part in _SECTION_PARTS[name]:
            if part in self._stale:
                self._load(part)

    # -- General ------------------------------------------------------------------

    def _build_general(self, parent: tk.Widget) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.columnconfigure(0, weight=1)
        self.award_tiles = _TileGrid(frame, "Your records", 5)
        self.award_tiles.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.fact_tiles = _TileGrid(frame, "Your stats at a glance", 4)
        self.fact_tiles.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        return frame

    # -- Task-based ---------------------------------------------------------------

    def _build_task_based(self, parent: tk.Widget) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.columnconfigure(0, weight=1)

        self.period_var = tk.StringVar(value=tracker_view.TYPE_PERIODS[-1][0])
        self.category_var = tk.StringVar(value=_ANY)
        self.tag_var = tk.StringVar(value=_ANY)
        self.type_var = tk.StringVar(value=_NO_TYPE)
        card, self.task_reset_button = self._filter_card(frame, 4, self._reset_task_filters)
        self._filter_menu(card, 0, "Period", self.period_var, list(_PERIODS), self._on_period_changed)
        self.category_menu = self._filter_menu(card, 1, "Category", self.category_var, [_ANY, *CATEGORIES],
                                               lambda: self._load(_TASKS))
        self.tag_menu = self._filter_menu(card, 2, "Tag", self.tag_var, [_ANY], lambda: self._load(_TASKS))
        self.type_menu = self._filter_menu(card, 3, "Task type", self.type_var, [_NO_TYPE], self._render_types)

        self.type_tiles = _TileGrid(frame, "Selected task type", 3, empty="No task types match these filters yet.")
        self.type_tiles.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self.type_cards = _TileGrid(frame, "All task types (completion rate)", 4,
                                    empty="Plan or complete a task to see its type here.")
        self.type_cards.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        pager = ctk.CTkFrame(frame, fg_color="transparent")
        pager.grid(row=3, column=0, sticky="w", padx=5, pady=(0, 14))
        self.type_prev = AppButton(pager, "Previous", lambda: self._turn_types(-1), variant="secondary", height=28,
                                   width=90)
        self.type_prev.pack(side="left")
        self.type_page_label = ctk.CTkLabel(pager, text="", text_color=theme.TEXT_MUTED)
        self.type_page_label.pack(side="left", padx=10)
        self.type_next = AppButton(pager, "Next", lambda: self._turn_types(1), variant="secondary", height=28, width=90)
        self.type_next.pack(side="left")
        return frame

    # -- Time-based ---------------------------------------------------------------

    def _build_time_based(self, parent: tk.Widget) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.columnconfigure(0, weight=1)

        self.days_var = tk.StringVar(value="All time")
        self.day_of_week_var = tk.StringVar(value=_ANY)
        self.time_bucket_var = tk.StringVar(value=_ANY)
        card, self.time_reset_button = self._filter_card(frame, 3, self._reset_time_filters)
        self._filter_menu(card, 0, "Date range", self.days_var, list(_DAY_OPTIONS), lambda: self._load(_TIME))
        self._filter_menu(card, 1, "Day of week", self.day_of_week_var, [_ANY, *WEEKDAYS], lambda: self._load(_TIME))
        self._filter_menu(card, 2, "Time of day", self.time_bucket_var, [_ANY, *_TIME_BUCKETS],
                          lambda: self._load(_TIME))

        self.time_tiles = _TileGrid(frame, "In this selection", 4)
        self.time_tiles.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self.weekday_tiles = _TileGrid(frame, "By weekday (completion rate)", 7)
        self.weekday_tiles.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.week_tiles = _TileGrid(frame, "Recent weeks (completion rate)", 4, empty="No weeks in this selection yet.")
        self.week_tiles.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        self.month_tiles = _TileGrid(frame, "Recent months (completion rate)", 4,
                                     empty="No months in this selection yet.")
        self.month_tiles.grid(row=4, column=0, sticky="ew", pady=(0, 14))

        self.day_tiles = _TileGrid(frame, "Day", 3, empty="No planned or completed work in this selection yet.")
        self.day_tiles.grid(row=5, column=0, sticky="ew", pady=(0, 14))
        self.day_var = tk.StringVar(value=_NO_DAY)
        self.day_menu = AppOptionMenu(self.day_tiles, variable=self.day_var, values=[_NO_DAY], width=170,
                                      command=lambda _value: self._render_day())
        self.day_menu.grid(row=0, column=2, sticky="e", padx=5, pady=(0, 4))

        charts = ctk.CTkFrame(frame, fg_color="transparent")
        charts.grid(row=6, column=0, sticky="ew", padx=5, pady=(0, 14))
        charts.columnconfigure((0, 1), weight=1, uniform="chart")
        left = self._card(charts, "Planned vs. actual duration")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.chart_group_var = tk.StringVar(value="By category")
        AppOptionMenu(left, variable=self.chart_group_var, values=list(_CHART_GROUPS),
                      command=lambda _value: self._render_charts()).pack(anchor="w", padx=10, pady=(0, 6))
        self.planned_vs_actual_chart = PlannedVsActualChart(left, height=170)
        self.planned_vs_actual_chart.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        right = self._card(charts, "Completion rate by time of day")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.completion_rate_chart = CompletionRateByBucketChart(right, height=170)
        self.completion_rate_chart.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        return frame

    # -- Project ------------------------------------------------------------------

    def _build_project(self, parent: tk.Widget) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.columnconfigure(0, weight=1)
        self.project_days_var = tk.StringVar(value="All time")
        card = self._card(frame)
        card.grid(row=0, column=0, sticky="ew", padx=5, pady=(0, 14))
        card.columnconfigure((0, 1, 2), weight=1, uniform="filter")
        self._filter_menu(card, 0, "Date range", self.project_days_var, list(_DAY_OPTIONS),
                          lambda: self._load(_PROJECT))

        ctk.CTkLabel(frame, text="Projects", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=theme.TEXT_PRIMARY, anchor="w").grid(row=1, column=0, sticky="w", padx=5, pady=(0, 4))
        self.project_list = ctk.CTkFrame(frame, fg_color="transparent")
        self.project_list.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.project_list.columnconfigure((0, 1, 2, 3), weight=1, uniform="project")
        self.project_buttons: dict = {}
        self.project_empty_label = ctk.CTkLabel(
            self.project_list, text="No projects yet. Create one on the Project Schedule page.",
            text_color=theme.TEXT_MUTED, anchor="w")

        self.project_tiles = _TileGrid(frame, _NO_PROJECT_TITLE, 3,
                                       empty="Select a project above to see the points it collected.")
        self.project_tiles.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        self.project_day_tiles = _TileGrid(frame, "Points collected by day", 4,
                                           empty="No tasks of this project were completed in this date range.")
        self.project_day_tiles.grid(row=4, column=0, sticky="ew", pady=(0, 14))
        return frame

    def select_project(self, project_id) -> None:
        """Show one project's statistics (read for the section's date range)."""
        self.project_id = project_id
        self._load(_PROJECT)

    def show_project(self, project_id) -> None:
        """Open the Project section with `project_id` selected (the Projects page's "View performance")."""
        self.project_id = project_id
        self._stale.add(_PROJECT)
        self.show_section(_PROJECT)

    def _load_project(self) -> None:
        self._requests[_PROJECT] += 1
        number, project_id = self._requests[_PROJECT], self.project_id
        range_days = _DAY_OPTIONS.get(self.project_days_var.get())

        def work():
            projects = self._controller.projects()
            known = projects.ok and any(project.id == project_id for project in projects.value)
            return projects, (self._controller.build_project_points(project_id, range_days) if known else None)

        def done(loaded) -> None:
            if number == self._requests[_PROJECT]:
                self._on_project_loaded(*loaded)

        run_in_background(self, work, done)

    def _on_project_loaded(self, projects: ControllerResult, report: ControllerResult | None) -> None:
        failed = projects if not projects.ok else report if report is not None and not report.ok else None
        if failed is not None:
            if isinstance(failed.cause, NotSignedInError):
                return
            self.status_label.configure(text=f"Productivity data is unavailable: {failed.error}")
            self.status_label.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 8))
            return
        self.status_label.grid_remove()
        self.projects = list(projects.value)
        if report is None:
            self.project_id = None  # nothing selected yet, or the selected project no longer exists
        self.project_report = report.value if report is not None else None
        self._render_project()

    def _render_project(self) -> None:
        for button in self.project_buttons.values():
            button.destroy()
        self.project_buttons = {}
        labels = {}
        for index, project in enumerate(self.projects):
            labels[project.id] = project.name
            button = AppButton(self.project_list, project.name, lambda project_id=project.id:
                               self.select_project(project_id), variant="secondary", height=34)
            button.grid(row=index // 4, column=index % 4, sticky="ew", padx=5, pady=4)
            self.project_buttons[project.id] = button
        if self.projects:
            self.project_empty_label.grid_remove()
        else:
            self.project_empty_label.grid(row=0, column=0, columnspan=4, sticky="w", padx=5, pady=5)
        self._mark(self.project_buttons, self.project_id, labels)
        report = self.project_report
        if report is None:
            self.project_tiles.set_title(_NO_PROJECT_TITLE)
            self.project_tiles.show([])
            self.project_day_tiles.show([])
            self.project_day_tiles.empty_label.configure(text="")
            return
        self.project_tiles.set_title(f"{labels[self.project_id]} · {tracker_view.project_period_text(report)}")
        self.project_tiles.show(tracker_view.project_stats(report))
        self.project_day_tiles.empty_label.configure(
            text="No tasks of this project were completed in this date range.")
        self.project_day_tiles.show(tracker_view.project_day_stats(report))

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def on_appearance_changed(self) -> None:
        """Repaint the (raw Tk) charts and the selection mark in the new light/dark appearance."""
        self.planned_vs_actual_chart.redraw()
        self.completion_rate_chart.redraw()
        self._mark(self.section_buttons, self.section, _SECTION_LABELS)

    def on_show(self) -> None:
        """Re-read persisted records each time the page is shown (after actions, a sync or direct writes)."""
        self.refresh()

    def refresh(self) -> None:
        """Reload the shown section now, and each other section when it is next shown."""
        self._stale = {*tracker_view.REPORTS, _PROJECT}
        for part in _SECTION_PARTS[self.section]:
            self._load(part)

    def _choice(self, variable: tk.StringVar) -> str | None:
        return None if variable.get() == _ANY else variable.get()

    def _selection(self, section: str) -> tuple[int | None, TrackerFilters]:
        """The date range and filters of one section; another section's filters never apply to it."""
        if section == _TASKS:
            return None, TrackerFilters(category=self._choice(self.category_var), tag=self._choice(self.tag_var))
        if section == _TIME:
            return _DAY_OPTIONS.get(self.days_var.get()), TrackerFilters(
                weekday=self._choice(self.day_of_week_var), time_bucket=self._choice(self.time_bucket_var))
        return None, TrackerFilters()

    def _load(self, section: str) -> None:
        """Read one section's report off the Tk thread; deliver it only while it is still that section's newest."""
        self._stale.discard(section)
        if section == _PROJECT:
            self._load_project()
            return
        range_days, filters = self._selection(section)
        self._requests[section] += 1
        number = self._requests[section]

        def done(loaded: tuple[ControllerResult[TrackerReport], list[str]]) -> None:
            if number == self._requests[section]:
                self._on_loaded(section, *loaded)

        run_in_background(
            self, lambda: (self._controller.build_tracker(range_days, filters), self._controller.used_tags()), done)

    def _on_loaded(self, section: str, result: ControllerResult[TrackerReport], used_tags: list[str]) -> None:
        if not result.ok:
            if isinstance(result.cause, NotSignedInError):
                return  # direct storage before sign-in: the Account page says so
            self.status_label.configure(text=f"Productivity data is unavailable: {result.error}")
            self.status_label.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 8))
            return
        self.status_label.grid_remove()
        report = self.reports[section] = result.value
        # The task form's categories (then any other the history holds), and every tag used so far -- on a task
        # or in the history -- so a chosen filter never narrows its own choices.
        self.category_menu.configure(
            values=[_ANY, *CATEGORIES, *(name for name in report.categories if name not in CATEGORIES)])
        self._known_tags.update(report.tags, used_tags)
        self.tag_menu.configure(values=[_ANY, *sorted(self._known_tags, key=str.casefold)])
        {_GENERAL: self._render_general, _TASKS: self._render_task_based, _TIME: self._render_time_based}[section]()

    def _reset_task_filters(self) -> None:
        self.period_var.set(tracker_view.TYPE_PERIODS[-1][0])
        self.category_var.set(_ANY)
        self.tag_var.set(_ANY)
        self.type_page = 0
        self._load(_TASKS)

    def _reset_time_filters(self) -> None:
        self.days_var.set("All time")
        self.day_of_week_var.set(_ANY)
        self.time_bucket_var.set(_ANY)
        self._load(_TIME)

    # -- rendering ----------------------------------------------------------------

    def _render_general(self) -> None:
        report = self.reports[_GENERAL]
        self.award_tiles.show(tracker_view.award_cards(report))
        self.fact_tiles.show(tracker_view.general_facts(report))

    def _render_task_based(self) -> None:
        self._type_keys = {label: key for key, label in tracker_view.type_choices(self.reports[_TASKS])}
        labels = list(self._type_keys) or [_NO_TYPE]
        self.type_menu.configure(values=labels)
        if self.type_var.get() not in self._type_keys:
            self.type_var.set(labels[0])
        self._render_types()

    def _on_period_changed(self) -> None:
        self.type_page = 0
        self._render_types()

    def _turn_types(self, step: int) -> None:
        self.type_page += step
        self._render_types()

    def _render_types(self) -> None:
        report = self.reports[_TASKS]
        if report is None:
            return
        period = _PERIODS[self.period_var.get()]
        key = self._type_keys.get(self.type_var.get(), "")
        self.type_tiles.set_title(f"{self.type_var.get()} · {self.period_var.get()}" if key
                                  else "Selected task type")
        self.type_tiles.show(tracker_view.type_stats(report, key, period))
        cards = tracker_view.type_cards(report, period)
        pages = max(1, -(-len(cards) // _TYPE_PAGE_SIZE))
        self.type_page = min(max(self.type_page, 0), pages - 1)
        self.type_cards.show(cards[self.type_page * _TYPE_PAGE_SIZE:(self.type_page + 1) * _TYPE_PAGE_SIZE])
        self.type_page_label.configure(text=f"Page {self.type_page + 1} of {pages}")
        self.type_prev.configure(state="normal" if self.type_page > 0 else "disabled")
        self.type_next.configure(state="normal" if self.type_page < pages - 1 else "disabled")

    def _render_time_based(self) -> None:
        report = self.reports[_TIME]
        self.time_tiles.show(tracker_view.time_stats(report))
        self.weekday_tiles.show(tracker_view.weekday_stats(report))
        self.week_tiles.show(tracker_view.week_stats(report))
        self.month_tiles.show(tracker_view.month_stats(report))
        days = tracker_view.day_choices(report) or [_NO_DAY]
        self.day_menu.configure(values=days)
        if self.day_var.get() not in days:
            self.day_var.set(days[0])
        self._render_day()
        self._render_charts()

    def _render_day(self) -> None:
        report, day = self.reports[_TIME], self.day_var.get()
        if report is None:
            return
        self.day_tiles.set_title(tracker_view.day_title(report, day) if day != _NO_DAY else "Day")
        self.day_tiles.show(tracker_view.day_stats(report, day) if day != _NO_DAY else [])

    def _render_charts(self) -> None:
        report = self.reports[_TIME]
        if report is None:
            return
        group = _CHART_GROUPS.get(self.chart_group_var.get(), "category")
        self.planned_vs_actual_chart.draw(tracker_view.planned_vs_actual_rows(report, group))
        self.completion_rate_chart.draw(tracker_view.bucket_chart_rows(report))
