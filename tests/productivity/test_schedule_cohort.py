"""Known-answer tests of the schedule-cohort report (app/productivity/schedule_cohort.py,
docs/analytics.md) over hand-built histories: every rate with its numerator and
denominator, exclusions, lineage (moves and regeneration), cutoff reconstruction,
missing data, and reporting-timezone boundaries including DST."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.execution.models import ExecutionStatus, TaskExecution, WorkSession
from app.planning.history import ExecutionHistory, ScheduleHistory
from app.planning.models import PlacementRemovalReason, ScheduledTask
from app.productivity.schedule_cohort import (
    OccurrenceState,
    build_schedule_cohort_report,
    read_schedule_cohort_report,
    report_window,
)

UTC = timezone.utc
VAN = "America/Vancouver"
ROOT = Path(__file__).resolve().parents[2]
CREATED = datetime(2026, 2, 1, tzinfo=UTC)


def at(day: int, hour: int, minute: int = 0, month: int = 3) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def placement(start: datetime, minutes: int = 60, *, task_id=None, tz="UTC", category="study", created=CREATED,
              deleted=None, reason=None, successor=None) -> ScheduledTask:
    local = start.astimezone(ZoneInfo(tz))
    return ScheduledTask(task_id=task_id or uuid.uuid4(), planned_date=local.date(), timezone=tz, planned_start=start,
                         planned_end=start + timedelta(minutes=minutes), task_category=category, created_at=created,
                         updated_at=deleted or created, deleted_at=deleted, removal_reason=reason,
                         superseded_by_id=successor)


def run(p: ScheduledTask, status: str, sessions=(), *, final=None, actual=None, estimate=None,
        category="snapshot") -> ExecutionHistory:
    execution = TaskExecution(
        id=str(uuid.uuid4()), task_name="Task", category=category, tag="", planned_duration=estimate if estimate
        is not None else round((p.planned_end - p.planned_start).total_seconds() / 60), priority=5,
        status=ExecutionStatus(status), created_at=CREATED.isoformat(), updated_at=CREATED.isoformat(),
        task_id=p.task_id, scheduled_task_id=p.id, canonical_planned_date=p.planned_date,
        canonical_timezone=p.timezone, canonical_planned_start=p.planned_start, canonical_planned_end=p.planned_end,
        actual_first_start_at=sessions[0][0] if sessions else None, actual_final_end_at=final,
        actual_active_duration_minutes=actual,
    )
    work = tuple(WorkSession(execution_id=execution.id, started_at=s.isoformat(),
                             ended_at=e.isoformat() if e else None) for s, e in sessions)
    return ExecutionHistory(execution, work)


def report(placements, executions=(), *, start=date(2026, 3, 2), end=date(2026, 3, 2), tz="UTC", as_of=None):
    window = report_window(start, end, tz)
    history = ScheduleHistory(start_utc=window.start_utc, end_utc=window.end_utc,
                              placements={p.id: p for p in placements},
                              executions={e.execution.scheduled_task_id: e for e in executions})
    return build_schedule_cohort_report(history, window, as_of=as_of or at(3, 12))


# -----------------------------------------------------------------------------
# The due cohort
# -----------------------------------------------------------------------------


def test_the_acceptance_cohort_two_of_five_completed_and_one_of_five_skipped() -> None:
    done1, done2, skipped, untouched, working, cancelled = (placement(at(2, h)) for h in (8, 9, 10, 11, 12, 13))
    future = placement(at(4, 9))
    executions = [
        run(done1, "completed", [(at(2, 8), at(2, 9))], final=at(2, 9), actual=60.0),
        run(done2, "completed", [(at(2, 9, 5), at(2, 9, 50))], final=at(2, 9, 50), actual=45.0),
        run(skipped, "skipped", final=at(2, 10)),
        run(working, "in_progress", [(at(2, 12), None)]),
        run(cancelled, "cancelled", final=at(2, 13)),
    ]
    result = report([done1, done2, skipped, untouched, working, cancelled, future], executions,
                    end=date(2026, 3, 8))

    assert (result.due_completion.numerator, result.due_completion.denominator, result.due_completion.value) == (2, 5, 0.4)
    assert (result.due_skip.numerator, result.due_skip.denominator, result.due_skip.value) == (1, 5, 0.2)
    outcomes = result.due_outcomes
    assert (outcomes.completed, outcomes.skipped, outcomes.overdue_unattempted, outcomes.in_progress,
            outcomes.cancelled) == (2, 1, 1, 1, 1)
    assert (result.due_count, result.future_count, result.future_states) == (6, 1, {"not_started": 1})
    assert result.workload.due_scheduled_minutes == 300 and result.workload.future_scheduled_minutes == 60
    assert result.workload.completed_planned_minutes == 120 and result.workload.completed_actual_active_minutes == 105


def test_nothing_is_completed_by_elapsed_time_and_the_cutoff_reconstructs_states() -> None:
    early = placement(at(2, 8))
    finished_later = run(early, "completed", [(at(2, 8, 10), at(2, 9)), (at(3, 10), at(3, 11))],
                         final=at(3, 11), actual=110.0)
    paused = placement(at(2, 10))
    paused_run = run(paused, "completed", [(at(2, 10), at(2, 10, 30)), (at(3, 14), at(3, 15))], final=at(3, 15),
                     actual=90.0)
    untouched = placement(at(1, 9, month=1))  # long past: still only "unattempted"
    as_of = at(3, 10, 30)  # the first is working again, the second paused between its sessions

    result = report([early, paused, untouched], [finished_later, paused_run], start=date(2026, 1, 1),
                    end=date(2026, 3, 3), as_of=as_of)
    states = {o.placement_id: o.state for o in result.occurrences}
    assert states == {early.id: OccurrenceState.IN_PROGRESS, paused.id: OccurrenceState.PAUSED,
                      untouched.id: OccurrenceState.NOT_STARTED}
    assert result.due_completion.numerator == 0 and result.due_completion.denominator == 3

    later = report([early, paused, untouched], [finished_later, paused_run], start=date(2026, 1, 1),
                   end=date(2026, 3, 3), as_of=at(3, 16))
    assert later.due_completion.numerator == 2


def test_an_empty_history_has_unavailable_rates_not_zero_rates() -> None:
    result = report([])
    assert result.occurrence_count == 0
    assert result.due_completion.value is None and result.due_completion.denominator == 0
    assert "no due" in result.due_completion.unavailable_reason
    assert result.reschedules.reschedule_rate.value is None
    assert result.duration.median_signed_error_minutes is None and result.start_timing.median_signed_delay_minutes is None
    assert result.workload.due_scheduled_minutes == 0 and result.day_signals == []


# -----------------------------------------------------------------------------
# Lineage: moves and regeneration
# -----------------------------------------------------------------------------


def test_repeated_moves_are_one_occurrence_with_every_event_counted() -> None:
    task = uuid.uuid4()
    third = placement(at(2, 15), task_id=task, created=at(1, 12))
    second = placement(at(2, 11), task_id=task, created=at(1, 11), deleted=at(1, 12),
                       reason=PlacementRemovalReason.RESCHEDULED, successor=third.id)
    first = placement(at(2, 9), task_id=task, deleted=at(1, 11), reason=PlacementRemovalReason.RESCHEDULED,
                      successor=second.id)
    regenerated_new = placement(at(2, 13), created=at(1, 10))
    regenerated_old = placement(at(2, 7), task_id=regenerated_new.task_id, deleted=at(1, 10),
                                reason=PlacementRemovalReason.REGENERATED, successor=regenerated_new.id)
    unchanged = placement(at(2, 18))

    result = report([first, second, third, regenerated_old, regenerated_new, unchanged])
    assert result.occurrence_count == 3  # three intended occurrences, not six placements
    moved = next(o for o in result.occurrences if o.task_id == task)
    assert (moved.placement_id, moved.original_placement_id, moved.reschedule_events) == (third.id, first.id, 2)
    rate = result.reschedules.reschedule_rate
    assert (rate.numerator, rate.denominator, result.reschedules.reschedule_events) == (1, 3, 2)
    assert (result.reschedules.regenerated_occurrences, result.reschedules.regeneration_events) == (1, 1)


def test_the_plan_in_effect_at_the_cutoff_is_attributed_and_moved_out_work_is_reported() -> None:
    task = uuid.uuid4()
    moved_to = placement(at(5, 9), task_id=task, created=at(2, 20))  # Thursday: outside the Monday report
    original = placement(at(2, 9), task_id=task, deleted=at(2, 20), reason=PlacementRemovalReason.RESCHEDULED,
                         successor=moved_to.id)

    after = report([original], as_of=at(3, 12))  # the history read for Monday holds only the Monday placement
    assert after.occurrence_count == 0 and after.data_quality.moved_out_of_range == 1

    before = report([original], as_of=at(2, 19))  # before the move, Monday's plan was in effect
    assert before.occurrence_count == 1 and before.occurrences[0].placement_id == original.id


def test_removals_are_excluded_and_unknown_reasons_stay_unknown() -> None:
    deleted = placement(at(2, 8), deleted=at(2, 1), reason=PlacementRemovalReason.DELETED)
    reset = placement(at(2, 9), deleted=at(2, 1), reason=PlacementRemovalReason.RESET)
    old_tombstone = placement(at(2, 10), deleted=at(2, 1))  # removed before reasons were recorded
    not_yet_planned = placement(at(2, 11), created=at(4, 1))  # created after the cutoff

    result = report([deleted, reset, old_tombstone, not_yet_planned])
    assert result.occurrence_count == 0 and result.due_completion.denominator == 0
    assert result.data_quality.removed_from_plan == {"deleted": 1, "reset": 1, "unknown": 1}
    assert result.reschedules.reschedule_events == 0  # a tombstone alone is never a reschedule


# -----------------------------------------------------------------------------
# Durations, timing, categories, signals
# -----------------------------------------------------------------------------


def test_durations_pair_actuals_with_the_historical_estimate() -> None:
    items = [placement(at(2, h)) for h in (8, 10, 12, 14)]
    executions = [
        run(items[0], "completed", [(at(2, 8), at(2, 8, 20)), (at(2, 8, 30), at(2, 8, 50))], final=at(2, 8, 50),
            actual=40.0, estimate=60),
        run(items[1], "completed", [(at(2, 10), at(2, 11, 30))], final=at(2, 11, 30), actual=90.0, estimate=60),
        run(items[2], "completed", [(at(2, 12), at(2, 12, 30))], final=at(2, 12, 30), actual=None),  # missing actual
        run(items[3], "completed", [(at(2, 14), at(2, 14, 30))], final=at(2, 14, 30), actual=30.0, estimate=0),
    ]
    duration = report(items, executions).duration
    assert (duration.pairs, duration.missing_actual, duration.zero_estimate) == (3, 1, 1)
    assert (duration.total_estimated_minutes, duration.total_actual_active_minutes) == (120, 160)
    assert duration.median_signed_error_minutes == 30.0  # errors -20, +30, +30
    assert duration.median_ratio == 1.083  # ratios 0.667, 1.5 (the zero estimate has none)
    assert (duration.underestimated, duration.overestimated) == (2, 1)


def test_start_delay_is_signed_lateness_is_not_and_unknown_starts_stay_unknown() -> None:
    early, late, never = placement(at(2, 8)), placement(at(2, 10)), placement(at(2, 12))
    executions = [run(early, "completed", [(at(2, 7, 50), at(2, 8, 50))], final=at(2, 8, 50), actual=60.0),
                  run(late, "completed", [(at(2, 10, 30), at(2, 11, 30))], final=at(2, 11, 30), actual=60.0)]
    timing = report([early, late, never], executions).start_timing
    assert (timing.known, timing.unknown, timing.late_starts) == (2, 1, 1)
    assert timing.median_signed_delay_minutes == 10.0  # -10 and +30
    assert timing.median_lateness_minutes == 15.0 and timing.mean_lateness_minutes == 15.0  # 0 and 30
    assert timing.completed_after_planned_end == 1 and timing.median_completion_lateness_minutes == 30.0


def test_categories_are_historical_snapshots_and_groups_never_merge_by_name() -> None:
    same_name = [uuid.uuid4(), uuid.uuid4()]  # two different tasks, both called "Task" in their snapshots
    items = [placement(at(2, 6 + i), minutes=60, task_id=same_name[i % 2], category="deep" if i < 10 else None)
             for i in range(12)]
    executions = [run(p, "completed", [(p.planned_start, p.planned_start + timedelta(minutes=75))],
                      final=p.planned_start + timedelta(minutes=75), actual=75.0) for p in items[:11]]
    result = report(items, executions, as_of=at(3, 12))
    assert set(result.by_category) == {"deep", "snapshot", "unknown"}  # the execution snapshot stands in for one
    assert result.data_quality.category_unknown == 1  # no snapshot and no execution: unknown, never guessed
    groups = {(g.group_by, g.key): g for g in result.underestimation}
    assert groups[("category", "deep")].pairs == 10 and groups[("category", "deep")].consistently_underestimated
    assert groups[("task", str(same_name[0]))].pairs == 6 and groups[("task", str(same_name[1]))].pairs == 5
    thin = report(items[:3], executions[:3]).underestimation
    assert all(not g.consistently_underestimated and g.evidence.startswith("insufficient") for g in thin)


def test_time_of_day_uses_the_plan_timezone_and_actual_start_is_a_separate_view() -> None:
    morning_in_vancouver = placement(at(2, 17), tz=VAN)  # 09:00 PST
    started_evening = run(morning_in_vancouver, "completed", [(at(3, 3), at(3, 4))], final=at(3, 4), actual=60.0)
    result = report([morning_in_vancouver], [started_evening], start=date(2026, 3, 2), end=date(2026, 3, 2), tz=VAN)
    assert set(result.by_planned_time_bucket) == {"morning"}
    assert set(result.by_actual_start_time_bucket) == {"evening"}  # 19:00 PST, labelled separately
    assert result.occurrences[0].plan_timezone == VAN


def test_an_overloaded_day_is_a_signal_with_its_reasons_and_capacity_stays_unknown() -> None:
    heavy = [placement(at(2, 8 + i)) for i in range(4)]
    light = [placement(at(3, 8))]
    executions = [run(heavy[0], "completed", [(at(2, 8), at(2, 9))], final=at(2, 9), actual=60.0),
                  run(light[0], "completed", [(at(3, 8), at(3, 9))], final=at(3, 9), actual=60.0)]
    days = {d.local_date: d for d in report(heavy + light, executions, end=date(2026, 3, 3)).day_signals}
    monday, tuesday = days[date(2026, 3, 2)], days[date(2026, 3, 3)]
    assert (monday.unfinished_planned_minutes, monday.unfinished_share, monday.high_unfinished_workload) == (
        180, 0.75, True)
    assert monday.reasons and not tuesday.high_unfinished_workload
    assert monday.available_minutes is None and monday.capacity_note.startswith("unknown")


# -----------------------------------------------------------------------------
# Reporting timezone
# -----------------------------------------------------------------------------


def test_vancouver_midnight_is_the_date_boundary_and_a_late_completion_keeps_its_date() -> None:
    before_midnight = placement(datetime(2026, 3, 3, 7, 30, tzinfo=UTC), minutes=20, tz=VAN)  # Mon 23:30 PST
    after_midnight = placement(datetime(2026, 3, 3, 8, 10, tzinfo=UTC), minutes=20, tz=VAN)  # Tue 00:10 PST
    late = run(before_midnight, "completed", [(datetime(2026, 3, 3, 8, 30, tzinfo=UTC),
                                               datetime(2026, 3, 3, 8, 50, tzinfo=UTC))],
               final=datetime(2026, 3, 3, 8, 50, tzinfo=UTC), actual=20.0)  # finished Tue 00:50
    monday = report([before_midnight, after_midnight], [late], start=date(2026, 3, 2), end=date(2026, 3, 2), tz=VAN)
    assert [o.placement_id for o in monday.occurrences] == [before_midnight.id]
    assert monday.due_completion.value == 1.0 and monday.start_timing.completed_after_planned_end == 1
    assert monday.range_start_utc == datetime(2026, 3, 2, 8, tzinfo=UTC)

    tuesday = report([before_midnight, after_midnight], [late], start=date(2026, 3, 3), end=date(2026, 3, 3), tz=VAN)
    assert [o.placement_id for o in tuesday.occurrences] == [after_midnight.id]


# The installed tz database decides: tzdata 2026.2 has British Columbia on permanent UTC-7 after its
# 2026-03-08 spring-forward, so Vancouver's last fall-back is 2025-11-02 (and 2026-11-01 is a 24-hour day).
@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 8), 23), (date(2025, 11, 2), 25), (date(2026, 3, 9), 24)])
def test_dst_days_have_their_real_length(day, hours) -> None:
    window = report_window(day, day, VAN)
    assert window.end_utc - window.start_utc == timedelta(hours=hours)


def test_work_across_the_spring_forward_gap_is_measured_in_real_minutes() -> None:
    start = datetime(2026, 3, 8, 9, 50, tzinfo=UTC)  # 01:50 PST; clocks jump 02:00 -> 03:00
    p = placement(start, minutes=20, tz=VAN)
    ex = run(p, "completed", [(start, start + timedelta(minutes=20))], final=start + timedelta(minutes=20), actual=20.0)
    result = report([p], [ex], start=date(2026, 3, 8), end=date(2026, 3, 8), tz=VAN, as_of=at(9, 12))
    occurrence = result.occurrences[0]
    assert occurrence.planned_minutes == 20 and occurrence.local_date == date(2026, 3, 8)
    assert result.start_timing.median_signed_delay_minutes == 0.0 and result.duration.median_signed_error_minutes == 0


def test_inputs_are_validated() -> None:
    with pytest.raises(ValueError):
        report_window(date(2026, 3, 2), date(2026, 3, 1), "UTC")
    with pytest.raises(ValueError):
        report_window(date(2026, 3, 2), date(2026, 3, 2), "Mars/Olympus")
    with pytest.raises(ValueError):
        report([], as_of=datetime(2026, 3, 3, 12))  # naive cutoff

    class Source:
        def schedule_history(self, start_utc, end_utc):
            return ScheduleHistory(start_utc=start_utc, end_utc=end_utc, placements={}, executions={})

    with pytest.raises(ValueError, match="later than"):
        read_schedule_cohort_report(Source(), start_date=date(2026, 3, 2), end_date=date(2026, 3, 2),
                                    timezone_name="UTC", as_of=at(5, 0), now=at(4, 0))


def test_the_host_timezone_never_changes_a_report() -> None:
    script = (
        "import json, sys; sys.path.insert(0, %r)\n"
        "from tests.productivity.test_schedule_cohort import sample_report\n"
        "print(sample_report().model_dump_json())\n" % str(ROOT)
    )
    outputs = []
    for host_zone in ("UTC0", "PST8PDT", "JST-9"):
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT,
                                   env={**os.environ, "TZ": host_zone}, timeout=120)
        assert completed.returncode == 0, completed.stderr
        outputs.append(json.loads(completed.stdout))
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0]["due_completion"]["numerator"] == 1


def sample_report():
    """A fixed report (used by the host-timezone test in child processes)."""
    task = uuid.UUID("11111111-1111-4111-8111-111111111111")
    p = ScheduledTask(id=uuid.UUID("22222222-2222-4222-8222-222222222222"), task_id=task,
                      planned_date=date(2026, 3, 2), timezone=VAN, planned_start=datetime(2026, 3, 3, 7, 30, tzinfo=UTC),
                      planned_end=datetime(2026, 3, 3, 7, 50, tzinfo=UTC), task_category="study", created_at=CREATED,
                      updated_at=CREATED)
    ex = TaskExecution(id="e1", task_name="T", category="study", tag="", planned_duration=20, priority=5,
                       status=ExecutionStatus.COMPLETED, created_at=CREATED.isoformat(), updated_at=CREATED.isoformat(),
                       task_id=task, scheduled_task_id=p.id, actual_first_start_at=datetime(2026, 3, 3, 8, tzinfo=UTC),
                       actual_final_end_at=datetime(2026, 3, 3, 8, 20, tzinfo=UTC), actual_active_duration_minutes=20.0)
    window = report_window(date(2026, 3, 2), date(2026, 3, 2), VAN)
    history = ScheduleHistory(start_utc=window.start_utc, end_utc=window.end_utc, placements={p.id: p},
                              executions={p.id: ExecutionHistory(ex, (WorkSession(
                                  execution_id="e1", started_at="2026-03-03T08:00:00+00:00",
                                  ended_at="2026-03-03T08:20:00+00:00"),))})
    return build_schedule_cohort_report(history, window, as_of=datetime(2026, 3, 4, tzinfo=UTC))
