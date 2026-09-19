"""
productivity_page.py

The desktop UI's Productivity page: filters, summary stats, two native-canvas
charts, best-supported time bucket per category, a recent-trend comparison,
structured insights (each carrying its own evidence label and sample count),
and a separated "Data" section for exporting or resetting local execution
history.

All data comes from one ProductivityController.build_dashboard(...) call per
refresh, run off the Tk main thread (see app/ui/background.py) so opening
this page or changing a filter never freezes the UI.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Literal

import customtkinter as ctk

from app.productivity.buckets import TimeBucket
from app.productivity.filters import ObservationFilters
from app.productivity.reporting import ProductivityDashboard
from app.ui import theme
from app.ui.background import ControllerResult, run_in_background
from app.ui.productivity_charts import CompletionRateByBucketChart, PlannedVsActualChart
from app.ui.productivity_controller import ProductivityController

_DAY_OPTIONS = {"All time": None, "Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}
_ANY = "(any)"
_TIME_BUCKET_ORDER = [bucket.value for bucket in TimeBucket]


class _StatTile(ctk.CTkFrame):
    """Small labeled stat card, styled independently of app.py's StatPill to avoid importing app.py."""

    def __init__(self, parent: tk.Widget, label: str) -> None:
        super().__init__(parent, fg_color="#FFFFFF", corner_radius=14, border_color=theme.CARD_BORDER, border_width=1)
        self.value_label = ctk.CTkLabel(
            self, text="--", font=ctk.CTkFont(size=18, weight="bold"), text_color=theme.TEXT_PRIMARY
        )
        self.value_label.pack(anchor="w", padx=14, pady=(12, 0))
        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=11), text_color=theme.TEXT_MUTED, anchor="w").pack(
            anchor="w", padx=14, pady=(0, 12)
        )

    def set_value(self, value: str) -> None:
        self.value_label.configure(text=value)


class ProductivityPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, productivity_controller: ProductivityController) -> None:
        super().__init__(parent, fg_color=theme.APP_BG)
        self._controller = productivity_controller
        self._latest_dashboard: ProductivityDashboard | None = None

        self.columnconfigure(0, weight=1)
        self._build()
        self.refresh()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._build_header()
        self._build_filters()
        self._build_summary()
        self._build_charts()
        self._build_breakdowns()
        self._build_data_section()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        ctk.CTkLabel(
            header, text="Productivity", font=ctk.CTkFont(size=26, weight="bold"), text_color=theme.TEXT_PRIMARY
        ).pack(anchor="w")
        self.range_label = ctk.CTkLabel(
            header, text="Selected range: all time", font=ctk.CTkFont(size=12), text_color=theme.TEXT_MUTED
        )
        self.range_label.pack(anchor="w", pady=(2, 0))

    def _build_filters(self) -> None:
        card = ctk.CTkFrame(self, fg_color="#FFFFFF", corner_radius=16, border_color=theme.CARD_BORDER, border_width=1)
        card.grid(row=1, column=0, sticky="ew", padx=20, pady=(6, 12))
        for column in range(6):
            card.columnconfigure(column, weight=1)

        self.days_var = tk.StringVar(value="All time")
        self.category_var = tk.StringVar(value=_ANY)
        self.tag_var = tk.StringVar(value=_ANY)
        self.day_of_week_var = tk.StringVar(value=_ANY)
        self.time_bucket_var = tk.StringVar(value=_ANY)

        self._filter_menu(card, 0, "Range", self.days_var, list(_DAY_OPTIONS))
        self.category_menu = self._filter_menu(card, 1, "Category", self.category_var, [_ANY])
        self.tag_menu = self._filter_menu(card, 2, "Tag", self.tag_var, [_ANY])
        self.day_of_week_menu = self._filter_menu(card, 3, "Day of week", self.day_of_week_var, [_ANY])
        self.time_bucket_menu = self._filter_menu(card, 4, "Time bucket", self.time_bucket_var, [_ANY])

        button_column = ctk.CTkFrame(card, fg_color="transparent")
        button_column.grid(row=1, column=5, sticky="ew", padx=10, pady=(0, 12))
        ctk.CTkButton(
            button_column, text="Apply", height=32, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            command=self.refresh,
        ).pack(fill="x", pady=(0, 4))
        ctk.CTkButton(
            button_column, text="Clear", height=28, fg_color="#E2E8F0", hover_color="#CBD5E1",
            text_color=theme.TEXT_PRIMARY, command=self._clear_filters,
        ).pack(fill="x")

    def _filter_menu(self, parent: tk.Widget, column: int, label: str, variable: tk.StringVar, values: list[str]) -> ctk.CTkOptionMenu:
        ctk.CTkLabel(parent, text=label, text_color=theme.TEXT_MUTED, font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=0, column=column, sticky="w", padx=10, pady=(10, 2)
        )
        menu = ctk.CTkOptionMenu(parent, variable=variable, values=values)
        menu.grid(row=1, column=column, sticky="ew", padx=10, pady=(0, 12))
        return menu

    def _build_summary(self) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 12))
        for column in range(6):
            row.columnconfigure(column, weight=1)

        self.completed_tile = _StatTile(row, "Completed")
        self.skipped_tile = _StatTile(row, "Skipped")
        self.completion_rate_tile = _StatTile(row, "Completion rate")
        self.productive_minutes_tile = _StatTile(row, "Productive active time")
        self.start_delay_tile = _StatTile(row, "Median start delay")
        self.duration_error_tile = _StatTile(row, "Duration estimate error")

        for column, tile in enumerate(
            (
                self.completed_tile, self.skipped_tile, self.completion_rate_tile,
                self.productive_minutes_tile, self.start_delay_tile, self.duration_error_tile,
            )
        ):
            tile.grid(row=0, column=column, sticky="ew", padx=6)

    def _build_charts(self) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 12))
        row.columnconfigure((0, 1), weight=1)

        left = self._chart_card(row, "Planned vs. actual duration by category")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.planned_vs_actual_chart = PlannedVsActualChart(left, height=170)
        self.planned_vs_actual_chart.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        right = self._chart_card(row, "Completion rate by time bucket")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.completion_rate_chart = CompletionRateByBucketChart(right, height=170)
        self.completion_rate_chart.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _chart_card(self, parent: tk.Widget, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color="#FFFFFF", corner_radius=16, border_color=theme.CARD_BORDER, border_width=1)
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=13, weight="bold"), text_color=theme.TEXT_PRIMARY).pack(
            anchor="w", padx=14, pady=(12, 6)
        )
        return card

    def _build_breakdowns(self) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 12))
        row.columnconfigure((0, 1, 2), weight=1)

        best_card = self._text_card(row, "Best-supported time bucket per category")
        best_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.best_bucket_label = ctk.CTkLabel(
            best_card, text="", justify="left", anchor="w", text_color=theme.TEXT_PRIMARY, wraplength=220
        )
        self.best_bucket_label.pack(anchor="w", padx=14, pady=(0, 12), fill="x")

        trend_card = self._text_card(row, "Recent trend (last 7 days vs. selection)")
        trend_card.grid(row=0, column=1, sticky="nsew", padx=6)
        self.trend_label = ctk.CTkLabel(
            trend_card, text="", justify="left", anchor="w", text_color=theme.TEXT_PRIMARY, wraplength=220
        )
        self.trend_label.pack(anchor="w", padx=14, pady=(0, 12), fill="x")

        insights_card = self._text_card(row, "Insights")
        insights_card.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        self.insights_label = ctk.CTkLabel(
            insights_card, text="", justify="left", anchor="w", text_color=theme.TEXT_PRIMARY, wraplength=260
        )
        self.insights_label.pack(anchor="w", padx=14, pady=(0, 12), fill="x")

    def _text_card(self, parent: tk.Widget, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color="#FFFFFF", corner_radius=16, border_color=theme.CARD_BORDER, border_width=1)
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=13, weight="bold"), text_color=theme.TEXT_PRIMARY).pack(
            anchor="w", padx=14, pady=(12, 6)
        )
        return card

    def _build_data_section(self) -> None:
        # Deliberately visually separated (its own bordered card, warning-toned header) from the
        # rest of the page's normal navigation/filtering flow, per the requirement that any
        # reset/delete control stay separate from normal navigation.
        card = ctk.CTkFrame(self, fg_color="#FFFFFF", corner_radius=16, border_color=theme.CARD_BORDER, border_width=1)
        card.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 20))
        card.columnconfigure((0, 1, 2), weight=1)

        ctk.CTkLabel(
            card, text="Data", font=ctk.CTkFont(size=13, weight="bold"), text_color=theme.TEXT_PRIMARY
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            card, text="Your execution history stays local to this device.", font=ctk.CTkFont(size=11),
            text_color=theme.TEXT_MUTED,
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 10))

        ctk.CTkButton(
            card, text="Export history (CSV)", height=32, fg_color="#334155", hover_color="#1E293B",
            command=lambda: self._export_history("csv"),
        ).grid(row=2, column=0, sticky="ew", padx=(14, 6), pady=(0, 14))
        ctk.CTkButton(
            card, text="Export history (JSON)", height=32, fg_color="#334155", hover_color="#1E293B",
            command=lambda: self._export_history("json"),
        ).grid(row=2, column=1, sticky="ew", padx=6, pady=(0, 14))
        ctk.CTkButton(
            card, text="Reset local history...", height=32, fg_color=theme.DANGER, hover_color=theme.DANGER_HOVER,
            command=self._confirm_reset,
        ).grid(row=2, column=2, sticky="ew", padx=(6, 14), pady=(0, 14))

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        filters = self._current_filters()
        run_in_background(self, lambda: self._controller.build_dashboard(filters), self._on_dashboard_loaded)

    def _clear_filters(self) -> None:
        self.days_var.set("All time")
        self.category_var.set(_ANY)
        self.tag_var.set(_ANY)
        self.day_of_week_var.set(_ANY)
        self.time_bucket_var.set(_ANY)
        self.refresh()

    def _current_filters(self) -> ObservationFilters:
        return ObservationFilters(
            days=_DAY_OPTIONS.get(self.days_var.get()),
            category=None if self.category_var.get() == _ANY else self.category_var.get(),
            tag=None if self.tag_var.get() == _ANY else self.tag_var.get(),
            day_of_week=None if self.day_of_week_var.get() == _ANY else self.day_of_week_var.get(),
            time_bucket=None if self.time_bucket_var.get() == _ANY else TimeBucket(self.time_bucket_var.get()),
        )

    def _on_dashboard_loaded(self, result: ControllerResult[ProductivityDashboard]) -> None:
        if not result.ok:
            messagebox.showerror("Productivity Data Error", result.error or "An unknown error occurred.", parent=self)
            return

        dashboard = result.value
        self._latest_dashboard = dashboard
        self._refresh_filter_options(dashboard)
        self._render_range_label()
        self._render_summary(dashboard)
        self._render_charts(dashboard)
        self._render_breakdowns(dashboard)

    def _refresh_filter_options(self, dashboard: ProductivityDashboard) -> None:
        self.category_menu.configure(values=[_ANY, *sorted(dashboard.by_category)])
        self.tag_menu.configure(values=[_ANY, *sorted(dashboard.by_tag)])
        self.day_of_week_menu.configure(values=[_ANY, *sorted(dashboard.by_day_of_week)])
        self.time_bucket_menu.configure(values=[_ANY, *sorted(dashboard.by_time_bucket)])

    def _render_range_label(self) -> None:
        parts = [self.days_var.get()]
        for label, variable in (
            ("category", self.category_var), ("tag", self.tag_var),
            ("day", self.day_of_week_var), ("time bucket", self.time_bucket_var),
        ):
            if variable.get() != _ANY:
                parts.append(f"{label}={variable.get()}")
        self.range_label.configure(text="Selected range: " + ", ".join(parts))

    def _render_summary(self, dashboard: ProductivityDashboard) -> None:
        stats = dashboard.global_stats

        completed_count = round(stats.terminal_count * stats.completion_rate) if stats.completion_rate is not None else 0
        skipped_count = stats.terminal_count - completed_count if stats.completion_rate is not None else 0

        self.completed_tile.set_value(str(completed_count))
        self.skipped_tile.set_value(str(skipped_count))
        self.completion_rate_tile.set_value(_format_rate(stats.completion_rate))
        self.productive_minutes_tile.set_value(f"{stats.productive_active_minutes:g} min")
        self.start_delay_tile.set_value(_format_minutes(stats.median_start_delay_minutes))
        self.duration_error_tile.set_value(_format_minutes(stats.duration_mae_minutes))

    def _render_charts(self, dashboard: ProductivityDashboard) -> None:
        planned_vs_actual_rows = [
            (category, stats.median_planned_duration_minutes, stats.median_actual_duration_minutes)
            for category, stats in sorted(dashboard.by_category.items())
        ]
        self.planned_vs_actual_chart.draw(planned_vs_actual_rows)

        completion_rows = [
            (bucket, dashboard.by_time_bucket[bucket].completion_rate, dashboard.by_time_bucket[bucket].observation_count)
            for bucket in _TIME_BUCKET_ORDER
            if bucket in dashboard.by_time_bucket
        ]
        self.completion_rate_chart.draw(completion_rows)

    def _render_breakdowns(self, dashboard: ProductivityDashboard) -> None:
        if not dashboard.best_supported_time_bucket_by_category:
            self.best_bucket_label.configure(text="Not enough history yet.")
        else:
            lines = []
            for category in sorted(dashboard.best_supported_time_bucket_by_category):
                time_bucket = dashboard.best_supported_time_bucket_by_category[category]
                stats = dashboard.by_category_and_time_bucket[f"{category}/{time_bucket}"]
                lines.append(
                    f"{category.capitalize()}: {time_bucket} "
                    f"(evidence: {stats.evidence_level.value}, n={stats.completed_duration_count})"
                )
            self.best_bucket_label.configure(text="\n".join(lines))

        trend = dashboard.recent_trend
        if trend.recent_observation_count == 0:
            self.trend_label.configure(
                text=f"No activity in the last 7 days (evidence: {trend.recent_evidence_level.value})."
            )
        else:
            self.trend_label.configure(
                text=(
                    f"Last 7 days: {_format_rate(trend.recent_completion_rate)} completion rate "
                    f"(n={trend.recent_observation_count}, evidence: {trend.recent_evidence_level.value})\n"
                    f"Selection baseline: {_format_rate(trend.baseline_completion_rate)} "
                    f"(n={trend.baseline_observation_count})"
                )
            )

        if not dashboard.insights:
            self.insights_label.configure(text="Not enough history yet for insights.")
        else:
            lines = [
                f"- {insight.text} [evidence: {insight.evidence_level.value}, n={insight.sample_count}]"
                for insight in dashboard.insights
            ]
            self.insights_label.configure(text="\n".join(lines))

    # ------------------------------------------------------------------
    # Data management
    # ------------------------------------------------------------------

    def _export_history(self, export_format: Literal["csv", "json"]) -> None:
        extension = f".{export_format}"
        path = filedialog.asksaveasfilename(
            title="Export Execution History",
            defaultextension=extension,
            filetypes=[(export_format.upper(), f"*{extension}"), ("All files", "*.*")],
        )
        if not path:
            return

        run_in_background(
            self,
            lambda: self._controller.export_execution_history(path, export_format),
            self._on_export_done,
        )

    def _on_export_done(self, result: ControllerResult[int]) -> None:
        if not result.ok:
            messagebox.showerror("Export Error", result.error or "An unknown error occurred.", parent=self)
            return
        messagebox.showinfo("Export Complete", f"Exported {result.value} execution record(s).", parent=self)

    def _confirm_reset(self) -> None:
        # A dedicated confirmation dialog, deliberately separate from the export buttons above,
        # for this destructive, irreversible action.
        confirmed = messagebox.askyesno(
            "Reset Local Execution History",
            "This permanently deletes all locally stored task execution history "
            "(including work sessions and feedback). This cannot be undone.\n\n"
            "Continue?",
            icon="warning",
            parent=self,
        )
        if not confirmed:
            return

        run_in_background(self, self._controller.reset_all_history, self._on_reset_done)

    def _on_reset_done(self, result: ControllerResult[int]) -> None:
        if not result.ok:
            messagebox.showerror("Reset Error", result.error or "An unknown error occurred.", parent=self)
            return
        messagebox.showinfo("History Reset", f"Deleted {result.value} execution record(s).", parent=self)
        self.refresh()


def _format_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def _format_minutes(minutes: float | None) -> str:
    return "n/a" if minutes is None else f"{minutes:g} min"
