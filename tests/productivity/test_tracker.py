"""The productivity tracker (app/productivity/tracker.py, docs/productivity-redesign-plan.md contracts B-E).

Pure cases build TrackerData by hand to pin the formulas: green-day streaks, ties, the partial current day and
week, averages over elapsed days, unknown points and zero denominators. Service cases run the real SQLite
planning and execution services: completion-date attribution of late work and of work whose plan was removed,
reopen / re-complete / delete, history longer than one reading window, and reads that never write."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.models import ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.productivity.reporting import ProductivityService
from app.productivity.schedule_cohort import OccurrenceState
from app.productivity.stats import ProductivityThresholds
from app.productivity.tracker import (
    CompletionItem,
    PlannedItem,
    TrackerData,
    TrackerFilters,
    build_tracker_report,
    merge_completions,
)

UTC = timezone.utc
TODAY = date(2026, 3, 18)  # a Wednesday; the week began Monday 16
AS_OF = datetime(2026, 3, 18, 20, tzinfo=UTC)
READING, WRITING = uuid.uuid4(), uuid.uuid4()
DONE, SKIP, CANCEL, OPEN = (OccurrenceState.COMPLETED, OccurrenceState.SKIPPED, OccurrenceState.CANCELLED,
                            OccurrenceState.NOT_STARTED)


def planned(day: date, state=DONE, *, type_id=READING, hour=9, due=True, category="study", estimate=30.0,
            actual=None) -> PlannedItem:
    return PlannedItem(
        placement_id=uuid.uuid4(), task_id=uuid.uuid4(), local_date=day,
        planned_start=datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
        bucket="morning" if hour < 12 else "afternoon", planned_minutes=30.0, due=due, state=state,
        execution_id=None, name="Reading", category=category, tags=("deep",), type_id=type_id,
        estimate_minutes=estimate, actual_active_minutes=actual, start_delay_minutes=None)


def completed(day: date, points: int | None = 1, *, type_id=READING, hour=10, minutes=None, key=None) -> CompletionItem:
    execution_id = str(uuid.uuid4())
    return CompletionItem(
        execution_id=execution_id, occurrence_key=key or f"execution:{execution_id}", task_id=uuid.uuid4(),
        placement_id=None, completed_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC), local_date=day,
        points=points, active_minutes=minutes, name="Reading", category="study", tags=("deep",), type_id=type_id)


def report(data: TrackerData, **kwargs):
    return build_tracker_report(data, timezone_name="UTC", as_of=kwargs.pop("as_of", AS_OF), **kwargs)


def march(day: int) -> date:
    return date(2026, 3, day)


def test_green_streaks_tie_break_on_empty_and_non_green_days_and_keep_today_provisional() -> None:
    days = {
        9: [DONE, DONE], 10: [DONE, DONE, DONE, SKIP, OPEN],  # 100% and exactly 60%: both green
        # 11: nothing scheduled -- an empty day ends the streak
        12: [DONE], 13: [DONE], 14: [DONE, CANCEL],            # a cancelled attempt is uncompleted: 50%, not green
        16: [DONE], 17: [DONE],
    }
    items = [planned(march(day), state) for day, states in days.items() for state in states]
    unfinished = report(TrackerData(planned=[*items, planned(TODAY, OPEN, due=False)]))

    longest = unfinished.general.longest_green_streak
    assert (longest.value, longest.tie_count) == (2.0, 3)
    assert [(w.start_date, w.end_date) for w in longest.winners] == [
        (march(9), march(10)), (march(12), march(13)), (march(16), march(17))]  # every tie, earliest first
    current = unfinished.general.current_green_streak
    assert current.value == 2.0 and (current.winners[0].start_date, current.winners[0].end_date) == (march(16), march(17))
    assert "Today is not finished" in current.calculation

    finished = report(TrackerData(planned=[*items, planned(TODAY, DONE)]))  # today resolved and green: it counts
    assert finished.general.current_green_streak.value == 3.0
    assert (finished.general.longest_green_streak.value, finished.general.longest_green_streak.tie_count) == (3.0, 1)

    broken = report(TrackerData(planned=[*items, planned(TODAY, SKIP)]))  # today resolved and not green: it ends it
    assert broken.general.current_green_streak.value == 0.0 and broken.general.current_green_streak.winners == []
    assert broken.general.longest_green_streak.value == 2.0  # the record stands; it is never today's partial day


def test_point_records_exclude_the_current_day_and_week_and_report_ties_and_unknown_points() -> None:
    data = TrackerData(completions=[
        completed(march(9), 5), completed(march(10), 2), completed(march(10), 3),  # two days tie at 5 points
        completed(march(12), None),                                               # legacy: points unknown
        completed(march(16), 4), completed(TODAY, 50),                             # this week / today: partial
    ])
    general = report(data).general

    day = general.highest_point_day
    assert (day.value, day.tie_count, [w.start_date for w in day.winners]) == (5.0, 2, [march(9), march(10)])
    assert day.partial.value == 50.0 and day.partial.start_date == TODAY
    assert day.qualified and "no recorded points" in day.qualifications[0]

    week = general.best_week  # only Monday 9 - Sunday 15 has ended; the current week is shown apart
    assert (week.value, week.sample_count, week.winners[0].start_date, week.winners[0].end_date) == (
        10.0, 1, march(9), march(15))
    assert week.winners[0].detail["unknown_point_completions"] == 1
    assert (week.partial.value, week.partial.start_date) == (54.0, march(16))

    averages = general.averages  # 9 elapsed days (9th..17th), one whole week; zero-work days are in the denominator
    assert (averages.first_activity_date, averages.through_date, averages.elapsed_days, averages.complete_weeks) == (
        march(9), march(17), 9, 1)
    assert (averages.daily.completed_tasks, averages.daily.points) == (round(5 / 9, 2), round(14 / 9, 2))
    assert (averages.weekly.completed_tasks, averages.weekly.points) == (4.0, 10.0)
    assert (averages.totals.known_point_completions, averages.totals.unknown_point_completions) == (4, 1)
    assert (averages.today.known_points, averages.current_week.known_points) == (50, 54)
    assert general.activity.known_points == 64 and not report(data).completeness.complete


def test_due_rates_keep_cancelled_and_future_apart_and_an_empty_denominator_is_unavailable() -> None:
    items = [planned(march(16), DONE), planned(march(16), SKIP), planned(march(16), OPEN),
             planned(march(16), CANCEL), planned(march(17), CANCEL), planned(TODAY, OPEN, due=False)]
    built = report(TrackerData(planned=items))
    counts = built.general.counts
    assert (counts.planned, counts.completed, counts.skipped, counts.cancelled, counts.future) == (6, 1, 1, 2, 1)
    assert (counts.due_denominator, counts.due_completion.numerator, counts.due_completion.value) == (3, 1, 0.3333)
    assert (counts.overdue_not_started, counts.unresolved) == (1, 2)  # overlapping views, not further addends

    by_day = {view.local_date: view for view in built.time.days}
    assert by_day[march(17)].counts.due_completion.value is None  # only a cancellation: unavailable, not 0%
    assert by_day[march(17)].counts.due_completion.unavailable_reason
    averages = built.general.averages  # the unweighted daily mean uses only days with a due denominator
    assert (averages.due_days, averages.average_daily_due_completion) == (1, 0.3333)
    empty = report(TrackerData())
    assert not empty.general.averages.available and not empty.general.highest_point_day.available
    assert empty.general.counts.planned == 0 and empty.types == [] and empty.time.days == []


def test_types_stay_separate_and_rank_by_completions_with_their_own_due_rate() -> None:
    items = [planned(march(16), DONE), planned(march(17), DONE), planned(march(17), SKIP, type_id=WRITING),
             planned(march(17), DONE, type_id=WRITING), planned(march(2), DONE, type_id=None)]
    data = TrackerData(
        planned=items, type_labels={READING: "Reading", WRITING: "Reading"},  # same label, different types
        completions=[completed(march(16)), completed(march(17)), completed(march(17), type_id=WRITING),
                     completed(march(2), type_id=None)])
    built = report(data)

    award = built.general.most_completed_type
    assert (award.value, award.tie_count, award.winners[0].key) == (2.0, 1, str(READING))
    assert award.winners[0].detail["due_completion_rate"] == 1.0 and award.qualified  # one untyped completion
    views = {view.type_id: view for view in built.types}
    assert set(views) == {READING, WRITING, None} and built.types[-1].type_id is None
    assert views[WRITING].periods["week"].counts.due_completion.value == 0.5
    assert views[READING].periods["today"].counts.planned == 0
    assert views[READING].periods["all_time"].activity.completions == 2
    assert views[None].periods["month"].counts.planned == 1  # unknown types are grouped apart, never merged

    filtered = report(data, filters=TrackerFilters(category="nothing"))  # a snapshot filter never regroups types
    assert filtered.types == [] and filtered.general.counts.planned == 0


def test_weekday_points_average_over_elapsed_dates_and_supported_slots_are_not_performance() -> None:
    thresholds = ProductivityThresholds(low=2, moderate=4, high=6)
    items = [planned(march(9), DONE, actual=40.0), planned(march(16), DONE, actual=20.0),
             planned(march(10), DONE, hour=14, actual=30.0), planned(march(17), SKIP, hour=14)]
    data = TrackerData(planned=items, completions=[completed(march(9), 6), completed(march(10), 1),
                                                   completed(march(16), 2)])
    time = report(data, thresholds=thresholds).time

    monday = time.weekdays[0]  # Mondays 9th and 16th elapsed: 8 points over 2 dates
    assert (monday.eligible_days, monday.activity.known_points, monday.average_points_per_eligible_day) == (2, 8, 4.0)
    assert (time.highest_points_weekday.winners, time.highest_points_weekday.value) == (["Monday"], 4.0)
    assert time.highest_completion_weekday.winners == ["Monday"]  # 2/2 on Mondays, 1/2 on Tuesdays
    assert time.highest_completion_weekday.detail == {"Monday": 1.0, "Tuesday": 0.5}
    slot = time.supported_slots_by_category[0]
    assert (slot.key, slot.buckets, slot.sample_count) == ("study", ["morning"], 2) and "not the best" in slot.note
    durations = time.planned_vs_actual_by_category[0].durations  # exact medians over the raw pairs
    assert (durations.pairs, durations.median_actual_minutes, durations.mean_absolute_error_minutes) == (3, 30.0, 6.67)


def test_an_occurrence_completed_under_two_placements_counts_once() -> None:
    first = completed(march(9), 3, key="placement:x")
    again = completed(march(10), 3, key="placement:x")
    merged, dropped = merge_completions([again, first, first])
    assert [item.execution_id for item in merged] == [again.execution_id] and dropped == 1


# -----------------------------------------------------------------------------
# Through the real services
# -----------------------------------------------------------------------------

VAN = "America/Vancouver"
MON = date(2026, 3, 2)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)  # Monday 07:00 in Vancouver

    def __call__(self) -> datetime:
        return self.now


class Stack:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        self.connection = get_connection(tmp_path / "app.db")
        self.planning = PlanningService(PlanningRepository(self.connection), self.clock)
        repository = ExecutionRepository(self.connection)
        self.executions = ExecutionService(repository, self.clock)
        self.productivity = ProductivityService(repository, clock=self.clock, history=self.planning, timezone_name=VAN)

    def plan(self, name: str, day: date, local_hour: int = 9, points: int = 3):
        task = self.planning.create_task(Task(name=name, category="study", estimated_duration_minutes=60, priority=5,
                                              points=points))
        start = datetime(day.year, day.month, day.day, local_hour, tzinfo=UTC) + timedelta(hours=8)
        planned_at = min(self.clock.now, start)  # the plan existed before its work
        placement = ScheduledTask(task_id=task.id, planned_date=day, timezone=VAN, planned_start=start,
                                  planned_end=start + timedelta(hours=1), created_at=planned_at,
                                  updated_at=planned_at)
        existing = self.planning.placements_for_date(day)
        self.planning.replace_placements(day, day, [*existing, placement],
                                         expected_versions={p.id: p.version for p in existing})
        return task, self.planning.get_placement(placement.id)

    def complete(self, task, placement, at: datetime) -> str:
        execution = self.executions.get_or_create_canonical_execution(task, placement)
        self.clock.now = at - timedelta(minutes=30)
        self.executions.start(execution.id)
        self.clock.now = at
        self.executions.complete(execution.id)
        return execution.id


@pytest.fixture
def stack(tmp_path):
    built = Stack(tmp_path)
    yield built
    built.connection.close()


def local(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC) + timedelta(hours=8)


def test_points_follow_the_completion_date_and_survive_edits_and_a_removed_plan(stack) -> None:
    thursday = MON + timedelta(days=3)
    task, placement = stack.plan("Essay", MON)
    execution_id = stack.complete(task, placement, local(thursday, 10))  # planned Monday, finished Thursday
    stack.planning.update_task(stack.planning.get_task(task.id).model_copy(update={"points": 9, "name": "Paper"}),
                               expected_version=stack.planning.get_task(task.id).version)
    stack.planning.replace_placements(MON, MON, [], expected_versions={placement.id: placement.version})  # plan removed
    stack.clock.now = local(thursday + timedelta(days=1), 12)

    built = stack.productivity.build_tracker_report()
    assert (built.general.activity.completions, built.general.activity.known_points) == (1, 3)  # the snapshot, not 9
    days = {view.local_date: view for view in built.time.days}
    assert days[thursday].activity.known_points == 3 and days[thursday].execution_ids == [execution_id]
    assert (days[MON].counts.planned, days[MON].activity.completions, days[MON].removed) == (0, 0, 1)
    assert built.general.counts.planned == 0 and built.completeness.removed_from_plan == 1
    assert built.general.highest_point_day.winners[0].start_date == thursday
    assert [view.label for view in built.types] == ["Essay"]  # the type keeps its label; the completion keeps its type
    assert built.types[0].periods["all_time"].activity.known_points == 3


def test_reopen_withdraws_the_award_recompletion_earns_once_and_deletion_removes_it(stack) -> None:
    task, placement = stack.plan("Essay", MON)
    execution_id = stack.complete(task, placement, local(MON, 10))
    stack.clock.now = local(MON + timedelta(days=2), 9)
    assert stack.productivity.build_tracker_report().general.activity.known_points == 3

    stack.executions.reopen(execution_id)
    reopened = stack.productivity.build_tracker_report()
    assert reopened.general.activity.completions == 0 and not reopened.general.highest_point_day.available
    assert reopened.general.counts.unresolved == 1

    stack.clock.now = local(MON + timedelta(days=2), 11)
    stack.executions.complete(execution_id)
    stack.clock.now = local(MON + timedelta(days=3), 9)
    again = stack.productivity.build_tracker_report()
    assert (again.general.activity.completions, again.general.activity.known_points) == (1, 3)  # once, same snapshot
    assert again.general.highest_point_day.winners[0].start_date == MON + timedelta(days=2)  # the new completion date

    execution = stack.executions.get_execution(execution_id)
    stack.executions.delete_execution(execution_id, expected_version=execution.version)
    deleted = stack.productivity.build_tracker_report()
    assert deleted.general.activity.completions == 0 and deleted.general.counts.overdue_not_started == 1


def test_all_time_history_is_read_in_bounded_windows_without_writing(stack) -> None:
    old_day = MON - timedelta(days=500)
    for index, day in enumerate([old_day, old_day + timedelta(days=1), MON - timedelta(days=1)]):
        for hour in (9, 11):
            task, placement = stack.plan(f"Task {index}-{hour}", day, hour)
            stack.complete(task, placement, local(day, hour + 1))
    stack.clock.now = local(MON, 12)

    statements: list[str] = []
    changes = stack.connection.total_changes
    stack.connection.set_trace_callback(statements.append)
    built = stack.productivity.build_tracker_report()
    stack.connection.set_trace_callback(None)

    assert stack.connection.total_changes == changes  # read-only
    assert not any(text.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for text in statements)
    assert built.completeness.windows_read == 2 and built.completeness.complete
    selects = [text for text in statements if text.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 20 * built.completeness.windows_read  # per window, never per record
    assert built.general.activity.completions == 6 and built.general.counts.due_completion.value == 1.0
    # A streak crosses any window boundary untouched; the record is the two old days, and yesterday is current.
    assert built.general.longest_green_streak.winners[0].start_date == old_day
    assert built.general.longest_green_streak.value == 2.0 and built.general.current_green_streak.value == 1.0
    assert built.general.averages.elapsed_days == 500  # the first day through yesterday, zero-work days included

    recent = stack.productivity.build_tracker_report(range_days=7)  # the range narrows; type periods do not
    assert recent.general.activity.completions == 2 and recent.range_start == MON - timedelta(days=6)
    assert sum(view.periods["all_time"].activity.completions for view in recent.types) == 6
