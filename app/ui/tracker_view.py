"""
app/ui/tracker_view.py

Tk-free wording of the tracker report (app/productivity/tracker.py) for the
Productivity page's three sections -- General, Specific (the task-based and
the time-based figures) and Project (app/productivity/project_stats.py).
Every figure becomes one Stat: a short label, the value shown large and a
short caption (a rate's caption carries its numerator and denominator).
Nothing is calculated here, and an unavailable figure is shown as "--" with
the reason, never as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.productivity.project_stats import ProjectPointsReport
from app.productivity.schedule_cohort import Rate
from app.productivity.tracker import ActivityTotals, PeriodView, RankedGroup, StatusCounts, TrackerReport, TypeView

SECTIONS = ("General", "Specific", "Project")
#: The tracker reports the page reads, each for its own filters; Specific shows the last two.
REPORTS = ("General", "Task-based", "Time-based")
#: (label, TypeView.periods key) of the four per-type periods.
TYPE_PERIODS = (("Today", "today"), ("This week", "week"), ("This month", "month"), ("All time", "all_time"))
AWARD_KINDS = ("highest_point_day", "best_week", "most_completed_type", "longest_green_streak",
               "current_green_streak")
UNKNOWN_TYPE_KEY = "unknown"
NO_VALUE = "--"


@dataclass(frozen=True)
class Stat:
    """One box of the page: what it is, its value, and a short qualifying caption."""

    label: str
    value: str
    caption: str = ""


def duration_text(minutes: float | None) -> str:
    if minutes is None:
        return NO_VALUE
    hours, rest = divmod(round(minutes), 60)
    if not hours:
        return f"{rest} min"
    return f"{hours}h {rest:02d}m" if rest else f"{hours}h"


def _rate_stat(label: str, rate: Rate) -> Stat:
    """A rate with its parts in the caption; unavailable (never 0%) when nothing was due."""
    if rate.value is None:
        return Stat(label, NO_VALUE, "No due tasks yet")
    return Stat(label, f"{rate.value:.0%}", f"{rate.numerator} of {rate.denominator} due tasks")


def _points_stat(label: str, activity: ActivityTotals) -> Stat:
    unknown = activity.unknown_point_completions
    return Stat(label, str(activity.known_points), f"+{unknown} task(s) without points" if unknown else "")


def _activity_caption(activity: ActivityTotals) -> str:
    return f"{activity.known_points} pts · {duration_text(activity.productive_minutes)}"


def _weekday_stat(label: str, group: RankedGroup, caption: str) -> Stat:
    if not group.available or group.value is None:
        return Stat(label, NO_VALUE, "Not enough data yet")
    value = group.winners[0][:3] if len(group.winners) > 2 else ", ".join(day[:3] for day in group.winners)
    if len(group.winners) > 2:
        value += f" +{len(group.winners) - 1} tied"
    return Stat(label, value, caption.format(group.value))


def _status_stats(counts: StatusCounts, activity: ActivityTotals) -> list[Stat]:
    return [
        _rate_stat("Completion rate", counts.due_completion),
        Stat("Planned", str(counts.planned)),
        Stat("Completed", str(activity.completions)),
        Stat("Skipped", str(counts.skipped)),
        _points_stat("Points earned", activity),
        Stat("Focused time", duration_text(activity.productive_minutes), f"{activity.timed_completions} timed task(s)"),
    ]


# -----------------------------------------------------------------------------
# General
# -----------------------------------------------------------------------------


def award_cards(report: TrackerReport) -> list[Stat]:
    """The five awards; a tie names the first winner and how many share it."""
    cards = []
    for kind in AWARD_KINDS:
        award = getattr(report.general, kind)
        if not award.available:
            reason = award.unavailable_reason or "Not available yet"
            cards.append(Stat(award.title, NO_VALUE, reason[:1].upper() + reason[1:]))
            continue
        if award.winners:
            caption = award.winners[0].label
            if award.tie_count > 1:
                caption += f" (+{award.tie_count - 1} tied)"
        else:
            caption = "No green day yet" if award.kind == "current_green_streak" else ""
        unit = award.unit[:-1] if award.value == 1 and award.unit.endswith("s") else award.unit
        cards.append(Stat(award.title, f"{award.value:g} {unit}", caption))
    return cards


def general_facts(report: TrackerReport) -> list[Stat]:
    """The headline facts of the whole recorded history."""
    general, averages = report.general, report.general.averages
    facts = [
        Stat("Tasks completed", str(general.activity.completions), "all time"),
        _points_stat("Points earned", general.activity),
        Stat("Focused time", duration_text(general.activity.productive_minutes),
             f"{general.activity.timed_completions} timed task(s)"),
        _rate_stat("Completion rate", general.counts.due_completion),
        Stat("Today", f"{averages.today.completions} done", _activity_caption(averages.today)),
        Stat("This week", f"{averages.current_week.completions} done", _activity_caption(averages.current_week)),
    ]
    if averages.available and averages.daily is not None:
        facts.append(Stat("Daily average", f"{averages.daily.completed_tasks:g} tasks",
                          f"{averages.daily.points:g} pts · {duration_text(averages.daily.productive_minutes)}"))
    else:
        facts.append(Stat("Daily average", NO_VALUE, "Needs one finished day"))
    if averages.available and averages.weekly is not None:
        facts.append(Stat("Weekly average", f"{averages.weekly.completed_tasks:g} tasks",
                          f"{averages.weekly.points:g} pts · {duration_text(averages.weekly.productive_minutes)}"))
    else:
        facts.append(Stat("Weekly average", NO_VALUE, "Needs one full week"))
    facts.append(_weekday_stat("Best weekday", report.time.highest_completion_weekday, "{:.0%} of due tasks done"))
    facts.append(_weekday_stat("Top points weekday", report.time.highest_points_weekday, "{:g} pts on average"))
    delay = general.median_start_delay_minutes
    if delay is None:
        facts.append(Stat("Typical start", NO_VALUE, "No started tasks yet"))
    else:
        value = "On time" if not delay else f"{abs(delay):g} min {'late' if delay > 0 else 'early'}"
        facts.append(Stat("Typical start", value, "compared with the plan"))
    error = general.durations.mean_absolute_error_minutes
    facts.append(Stat("Estimate accuracy", NO_VALUE, "No timed tasks yet") if error is None else
                 Stat("Estimate accuracy", f"±{error:g} min", "average estimate miss"))
    return facts


# -----------------------------------------------------------------------------
# Task-based
# -----------------------------------------------------------------------------


def type_key(view: TypeView) -> str:
    return str(view.type_id) if view.type_id is not None else UNKNOWN_TYPE_KEY


def type_choices(report: TrackerReport) -> list[tuple[str, str]]:
    """(key, label) of every type; labels that collide carry a short id so each stays selectable."""
    counts: dict[str, int] = {}
    for view in report.types:
        counts[view.label] = counts.get(view.label, 0) + 1
    return [(type_key(view), view.label if counts[view.label] == 1 or view.type_id is None
             else f"{view.label} [{str(view.type_id)[:8]}]") for view in report.types]


def type_cards(report: TrackerReport, period: str) -> list[Stat]:
    """One box per task type for `period`: its due-completion rate, and what was finished."""
    labels = dict(type_choices(report))
    cards = []
    for view in report.types:
        stats = view.periods[period]
        rate = stats.counts.due_completion
        cards.append(Stat(labels[type_key(view)], NO_VALUE if rate.value is None else f"{rate.value:.0%}",
                          f"{stats.activity.completions} completed · {_activity_caption(stats.activity)}"))
    return cards


def type_stats(report: TrackerReport, key: str, period: str) -> list[Stat]:
    """The boxes of one task type for `period` (empty when there is no such type)."""
    view = next((item for item in report.types if type_key(item) == key), None)
    if view is None:
        return []
    stats = view.periods[period]
    durations = stats.durations
    slot = view.supported_slot
    return [
        *_status_stats(stats.counts, stats.activity),
        Stat("Typical duration", NO_VALUE, "No timed tasks yet") if not durations.pairs else
        Stat("Typical duration", duration_text(durations.median_actual_minutes),
             f"planned {duration_text(durations.median_estimated_minutes)}"),
        _weekday_stat("Best weekday", view.best_weekday, "{:.0%} of due tasks done"),
        Stat("Usual time of day", NO_VALUE, "No timed tasks yet") if slot is None else
        Stat("Usual time of day", ", ".join(bucket.capitalize() for bucket in slot.buckets),
             f"{slot.sample_count} timed task(s)"),
    ]


# -----------------------------------------------------------------------------
# Time-based
# -----------------------------------------------------------------------------


def time_stats(report: TrackerReport) -> list[Stat]:
    """The totals of the selected dates, and the weekdays that stand out in them."""
    return [
        *_status_stats(report.general.counts, report.general.activity),
        _weekday_stat("Best weekday", report.time.highest_completion_weekday, "{:.0%} of due tasks done"),
        _weekday_stat("Top points weekday", report.time.highest_points_weekday, "{:g} pts on average"),
    ]


def weekday_stats(report: TrackerReport) -> list[Stat]:
    """One box per weekday: due completion (planned date) and what was finished on it."""
    stats = []
    for view in report.time.weekdays:
        rate = view.counts.due_completion
        value = NO_VALUE if rate.value is None else f"{rate.value:.0%}"
        due = "nothing due" if rate.value is None else f"{rate.numerator}/{rate.denominator} due"
        stats.append(Stat(view.weekday[:3], value, f"{due} · {view.activity.known_points} pts"))
    return stats


def _period_stat(view: PeriodView) -> Stat:
    start = view.start_date
    label = f"{start:%B %Y}" if len(view.key) == 7 else f"Week of {start:%b} {start.day}"
    rate = view.counts.due_completion
    return Stat(label + ("" if view.complete else " (so far)"), NO_VALUE if rate.value is None else f"{rate.value:.0%}",
                f"{view.activity.completions} completed · {view.activity.known_points} pts · "
                f"{view.green_days} green day(s)")


def week_stats(report: TrackerReport, *, limit: int = 4) -> list[Stat]:
    """The newest `limit` Monday-Sunday weeks, newest first."""
    return [_period_stat(view) for view in list(reversed(report.time.weeks))[:limit]]


def month_stats(report: TrackerReport, *, limit: int = 4) -> list[Stat]:
    return [_period_stat(view) for view in list(reversed(report.time.months))[:limit]]


def day_choices(report: TrackerReport) -> list[str]:
    return [view.local_date.isoformat() for view in reversed(report.time.days)]


def day_title(report: TrackerReport, day: date | str) -> str:
    wanted = date.fromisoformat(day) if isinstance(day, str) else day
    view = next((item for item in report.time.days if item.local_date == wanted), None)
    if view is None:
        return "Day"
    return f"{view.weekday}, {view.local_date:%B} {view.local_date.day}" + (" (today, so far)" if view.partial else "")


def day_stats(report: TrackerReport, day: date | str) -> list[Stat]:
    """The boxes of one day (empty when the day is not in the selection)."""
    wanted = date.fromisoformat(day) if isinstance(day, str) else day
    view = next((item for item in report.time.days if item.local_date == wanted), None)
    return [] if view is None else _status_stats(view.counts, view.activity)


def bucket_chart_rows(report: TrackerReport) -> list[tuple[str, float | None, int]]:
    """Rows for CompletionRateByBucketChart: (planned-start bucket, due-completion rate, due denominator)."""
    return [(view.bucket, view.counts.due_completion.value, view.counts.due_denominator)
            for view in report.time.buckets if view.counts.planned]


def planned_vs_actual_rows(report: TrackerReport, by: str = "category") -> list[tuple[str, float | None, float | None]]:
    """Rows for PlannedVsActualChart: (category or type, median estimate, median actual) of timed completions."""
    groups = report.time.planned_vs_actual_by_type if by == "type" else report.time.planned_vs_actual_by_category
    return [(group.label, group.durations.median_estimated_minutes, group.durations.median_actual_minutes)
            for group in groups if group.durations.pairs]


# -----------------------------------------------------------------------------
# Project
# -----------------------------------------------------------------------------


def _day_text(day: date) -> str:
    return f"{day:%a %b} {day.day}, {day.year}"


def project_period_text(report: ProjectPointsReport) -> str:
    """The averaging period in words, e.g. "Sep 1 - Oct 5, 2026 (35 days)"."""
    if report.period_start is None:
        return "No completed tasks yet"
    days = "1 day" if report.days_in_period == 1 else f"{report.days_in_period} days"
    return f"{_day_text(report.period_start)} – {_day_text(report.period_end)} ({days})"


def project_stats(report: ProjectPointsReport) -> list[Stat]:
    """Total points, the average per day (with its period and denominator) and the completed-task count."""
    scope = f"the last {report.range_days} days" if report.range_days else "all time, from its first completion"
    unknown = (f" {report.unknown_points_count} completion(s) without recorded points count as 0."
               if report.unknown_points_count else "")
    if report.average_points_per_day is None:
        average = Stat("Average points per day", NO_VALUE, "Nothing completed yet, so there is no period to average.")
    else:
        average = Stat("Average points per day", f"{report.average_points_per_day:g}",
                       f"{report.total_points} points ÷ {report.days_in_period} calendar day(s): "
                       f"{project_period_text(report)}. Days without a completion count.")
    return [
        Stat("Total points collected", str(report.total_points), f"Completed tasks of this project, {scope}.{unknown}"),
        average,
        Stat("Tasks completed", str(report.completed_count),
             "Each task once, on the day it was completed -- scheduled or completed from the project."),
    ]


def project_day_stats(report: ProjectPointsReport, *, limit: int = 28) -> list[Stat]:
    """Points collected per day, newest first (at most `limit` days; only days with a completion)."""
    return [Stat(_day_text(day.date), str(day.points),
                 f"{day.completed_count} task{'s' if day.completed_count != 1 else ''} completed")
            for day in reversed(report.by_day[-limit:])]
