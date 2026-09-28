"""
app/ui/cohort_view.py

Tk-free wording of the schedule-cohort report (docs/analytics.md) for the
Productivity page: every rate with its numerator and denominator, the date
basis and timezone, exclusions, and honest empty / low-evidence states. No
ML, and nothing is inferred beyond the report.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.productivity.schedule_cohort import Rate, ScheduleCohortReport
from app.productivity.stats import EvidenceLevel


@dataclass(frozen=True)
class CohortView:
    basis: str
    tiles: dict[str, str]
    workload: str
    reschedules: str
    days: str
    underestimation: str
    notes: str
    empty: bool


def rate_text(rate: Rate) -> str:
    if rate.value is None:
        return "n/a"
    text = f"{rate.value:.0%} ({rate.numerator}/{rate.denominator})"
    if rate.evidence_level in (EvidenceLevel.INSUFFICIENT, EvidenceLevel.LOW):
        text += f" - {rate.evidence_level.value} evidence"
    return text


def cohort_view(report: ScheduleCohortReport) -> CohortView:
    outcomes = report.due_outcomes
    basis = (f"Planned dates {report.start_date:%b} {report.start_date.day} - {report.end_date:%b} "
             f"{report.end_date.day} in {report.timezone}, as of {report.as_of:%Y-%m-%d %H:%M} UTC. "
             "Each planned occurrence counts once, however often it moved.")
    if report.occurrence_count == 0:
        return CohortView(basis=basis, tiles={}, workload="", reschedules="", days="", underestimation="",
                          notes="Nothing was planned on these dates yet -- make a schedule to see follow-through.",
                          empty=True)

    tiles = {
        "Due completion": rate_text(report.due_completion),
        "Due skip rate": rate_text(report.due_skip),
        "Overdue, not started": str(outcomes.overdue_unattempted),
        "In progress / paused": f"{outcomes.in_progress} / {outcomes.paused}",
        "Cancelled (excluded)": str(outcomes.cancelled),
        "Not yet due": str(report.future_count),
    }
    workload = report.workload
    duration = report.duration
    lines = [f"Due work planned: {workload.due_scheduled_minutes:g} min; not yet due: "
             f"{workload.future_scheduled_minutes:g} min.",
             f"Completed: {workload.completed_planned_minutes:g} planned min, "
             f"{workload.completed_actual_active_minutes:g} actual active min"
             + (f" ({workload.completed_missing_actual} without a recorded actual)" if workload.completed_missing_actual
                else "") + "."]
    if duration.pairs:
        lines.append(f"Estimate vs actual over {duration.pairs} completed: median {duration.median_signed_error_minutes:+g}"
                     f" min" + (f", median ratio {duration.median_ratio:g}x" if duration.median_ratio is not None else "")
                     + ".")
    else:
        lines.append("Estimate vs actual: no completed work with a recorded actual yet.")
    timing = report.start_timing
    if timing.known:
        lines.append(f"Start: median {timing.median_signed_delay_minutes:+g} min vs plan over {timing.known} "
                     f"started ({timing.unknown} not started or unknown).")
    rescheduled = report.reschedules
    reschedules = (f"Moved at least once: {rate_text(rescheduled.reschedule_rate)} of the occurrences; "
                   f"{rescheduled.reschedule_events} move(s) in total. Automatic regeneration replaced "
                   f"{rescheduled.regenerated_occurrences} occurrence(s) ({rescheduled.regeneration_events} time(s)).")
    flagged = [signal for signal in report.day_signals if signal.high_unfinished_workload]
    days = "\n".join(f"{s.local_date:%a %b} {s.local_date.day}: {s.reasons[0]}" for s in flagged) or \
        "No day crossed the unfinished-workload thresholds."
    days += "\nSignals, not causes. Historical capacity is unknown (past day windows are not recorded)."
    groups = [g for g in report.underestimation if g.group_by == "category"]
    consistent = [g for g in groups if g.consistently_underestimated]
    if consistent:
        underestimation = "\n".join(f"{g.label}: {g.underestimated}/{g.pairs} ran long, median ratio "
                                    f"{g.median_ratio:g}x" for g in consistent)
    elif groups:
        underestimation = "No category is consistently underestimated yet (" + "; ".join(
            f"{g.label}: n={g.pairs}" for g in groups) + "; 5 completions needed per category)."
    else:
        underestimation = "Not enough completed work with recorded actuals yet."
    quality = report.data_quality
    notes = []
    if quality.category_unknown:
        notes.append(f"{quality.category_unknown} planned without a recorded category (older data).")
    removed = sum(quality.removed_from_plan.values())
    if removed:
        notes.append(f"{removed} removed from the plan (not counted).")
    if quality.moved_out_of_range:
        notes.append(f"{quality.moved_out_of_range} moved to other dates (counted there).")
    return CohortView(basis=basis, tiles=tiles, workload="\n".join(lines), reschedules=reschedules, days=days,
                      underestimation=underestimation, notes=" ".join(notes), empty=False)
