"""app/ui/tracker_view.py: the Productivity page's wording of the tracker report, without a window. It only
formats the report into labelled boxes -- ties, partial periods, unknown coverage, a rate's parts and truthful
empty states -- and the controller's tracker call fails safely when there is no schedule history source."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from app.productivity.schedule_cohort import OccurrenceState
from app.productivity.tracker import CompletionItem, PlannedItem, TrackerData, build_tracker_report
from app.ui import tracker_view
from app.ui.productivity_controller import ProductivityController

UTC = timezone.utc
AS_OF = datetime(2026, 3, 18, 20, tzinfo=UTC)  # Wednesday
READING, WRITING = uuid.uuid4(), uuid.uuid4()


def march(day: int) -> date:
    return date(2026, 3, day)


def planned(day: date, state: OccurrenceState, *, type_id=READING, name="Reading", actual=None) -> PlannedItem:
    return PlannedItem(
        placement_id=uuid.uuid4(), task_id=uuid.uuid4(), local_date=day,
        planned_start=datetime(day.year, day.month, day.day, 9, tzinfo=UTC), bucket="morning", planned_minutes=30.0,
        due=True, state=state, execution_id=None, name=name, category="study", tags=("deep",), type_id=type_id,
        estimate_minutes=30.0, actual_active_minutes=actual, start_delay_minutes=5.0 if actual else None)


def completed(day: date, points: int | None, *, type_id=READING, name="Reading") -> CompletionItem:
    execution_id = str(uuid.uuid4())
    return CompletionItem(
        execution_id=execution_id, occurrence_key=f"execution:{execution_id}", task_id=uuid.uuid4(), placement_id=None,
        completed_at=datetime(day.year, day.month, day.day, 10, tzinfo=UTC), local_date=day, points=points,
        active_minutes=25.0, name=name, category="study", tags=("deep",), type_id=type_id)


def sample():
    data = TrackerData(
        planned=[planned(march(9), OccurrenceState.COMPLETED, actual=40.0), planned(march(10), OccurrenceState.SKIPPED),
                 planned(march(10), OccurrenceState.CANCELLED, type_id=WRITING, name="Essay"),
                 planned(march(17), OccurrenceState.NOT_STARTED, type_id=WRITING, name="Essay")],
        completions=[completed(march(9), 5), completed(march(10), 5, type_id=WRITING, name="Essay"),
                     completed(march(12), None), completed(march(18), 9)],
        type_labels={READING: "Reading", WRITING: "Reading"},  # two types that share a label
    )
    return build_tracker_report(data, timezone_name="UTC", as_of=AS_OF)


def values(stats) -> dict[str, str]:
    return {stat.label: stat.value for stat in stats}


def captions(stats) -> dict[str, str]:
    return {stat.label: stat.caption for stat in stats}


def test_award_cards_show_ties_and_singular_units() -> None:
    cards = tracker_view.award_cards(sample())
    assert [card.label for card in cards] == ["Highest-point day", "Best completed week", "Most completed task type",
                                              "Longest green-day streak", "Current green-day streak"]
    assert values(cards)["Highest-point day"] == "5 points"
    assert captions(cards)["Highest-point day"] == "2026-03-09 (+1 tied)"  # a tie names the first and counts the rest
    assert values(cards)["Longest green-day streak"] == "1 day"
    assert (values(cards)["Current green-day streak"], captions(cards)["Current green-day streak"]) == (
        "0 days", "No green day yet")


def test_general_facts_use_actual_counts_and_carry_each_rates_parts() -> None:
    facts = tracker_view.general_facts(sample())
    shown, notes = values(facts), captions(facts)
    assert (shown["Tasks completed"], shown["Points earned"], shown["Focused time"]) == ("4", "19", "1h 40m")
    assert notes["Points earned"] == "+1 task(s) without points"  # unknown points are said, never counted as zero
    assert (shown["Completion rate"], notes["Completion rate"]) == ("33%", "1 of 3 due tasks")
    assert (shown["Today"], shown["Daily average"], shown["Weekly average"]) == ("1 done", "0.33 tasks", "3 tasks")
    assert shown["Best weekday"] == "--" and shown["Top points weekday"] == "Mon, Tue"  # too little due work; a tie
    assert shown["Typical start"] == "5 min late" and shown["Estimate accuracy"] == "\u00b110 min"
    assert tracker_view.duration_text(None) == "--" and tracker_view.duration_text(120) == "2h"


def test_type_boxes_keep_same_named_types_apart() -> None:
    report = sample()
    choices = tracker_view.type_choices(report)
    assert len(choices) == 2 and len({label for _, label in choices}) == 2  # same label, still two selectable types
    cards = tracker_view.type_cards(report, "all_time")
    assert [card.label for card in cards] == [label for _, label in choices]
    assert sorted(card.value for card in cards) == ["0%", "50%"]
    assert {card.value for card in tracker_view.type_cards(report, "today")} == {"--"}  # nothing due: not 0%

    stats = tracker_view.type_stats(report, str(WRITING), "all_time")
    shown = values(stats)
    assert (shown["Planned"], shown["Completed"], shown["Skipped"], shown["Points earned"]) == ("2", "1", "0", "5")
    assert (shown["Completion rate"], captions(stats)["Completion rate"]) == ("0%", "0 of 1 due tasks")  # no cancelled
    assert shown["Typical duration"] == "--" and shown["Usual time of day"] == "--"
    reading = values(tracker_view.type_stats(report, str(READING), "all_time"))
    assert reading["Typical duration"] == "40 min" and reading["Usual time of day"] == "Morning"
    assert tracker_view.type_stats(report, "missing", "all_time") == []


def test_days_weeks_and_weekdays_are_worded_from_the_report() -> None:
    report = sample()
    shown = values(tracker_view.time_stats(report))
    assert (shown["Completion rate"], shown["Planned"], shown["Skipped"]) == ("33%", "4", "1")
    weekdays = tracker_view.weekday_stats(report)
    assert [stat.label for stat in weekdays] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert (weekdays[0].value, weekdays[1].caption, weekdays[2].value) == ("100%", "0/2 due \u00b7 5 pts", "--")
    weeks = tracker_view.week_stats(report)
    assert [stat.label for stat in weeks] == ["Week of Mar 16 (so far)", "Week of Mar 9"]  # newest first
    assert weeks[1].value == "50%" and len(tracker_view.week_stats(report, limit=1)) == 1
    assert tracker_view.month_stats(report)[0].label == "March 2026 (so far)"

    assert tracker_view.day_choices(report)[0] == "2026-03-18"
    assert tracker_view.day_title(report, "2026-03-18") == "Wednesday, March 18 (today, so far)"
    day = tracker_view.day_stats(report, "2026-03-10")
    assert (values(day)["Planned"], values(day)["Skipped"], values(day)["Points earned"]) == ("2", "1", "5")
    assert captions(day)["Completion rate"] == "0 of 1 due tasks"
    assert tracker_view.day_stats(report, "2020-01-01") == []
    assert tracker_view.planned_vs_actual_rows(report) == [("study", 30.0, 40.0)]
    assert tracker_view.bucket_chart_rows(report)[0][0] == "morning"
    assert (report.categories, report.tags) == (["study"], ["deep"])  # the filter choices, whatever the filters


def test_an_empty_report_says_so_instead_of_showing_numbers() -> None:
    report = build_tracker_report(TrackerData(), timezone_name="UTC", as_of=AS_OF)
    assert {card.value for card in tracker_view.award_cards(report)} == {"--"}
    assert all(card.caption for card in tracker_view.award_cards(report))  # each says why
    shown = values(tracker_view.general_facts(report))
    assert shown["Completion rate"] == "--" and shown["Daily average"] == "--" and shown["Tasks completed"] == "0"
    assert tracker_view.type_cards(report, "all_time") == [] and tracker_view.week_stats(report) == []
    assert tracker_view.day_choices(report) == [] and {s.value for s in tracker_view.weekday_stats(report)} == {"--"}
    assert tracker_view.planned_vs_actual_rows(report) == [] and tracker_view.bucket_chart_rows(report) == []
    assert (report.categories, report.tags) == ([], [])


def test_the_controller_reports_a_missing_history_source_safely(productivity_controller: ProductivityController) -> None:
    result = productivity_controller.build_tracker()
    assert not result.ok and "history" in result.error
    assert productivity_controller.used_tags() == []  # no task source: only the history's tags are known


def test_used_tags_are_every_tag_on_a_task_once_and_never_an_error(productivity_service, execution_controller) -> None:
    controller = ProductivityController(productivity_service, execution_controller,
                                        task_tags=lambda: ["math", "deep", "math"])
    assert controller.used_tags() == ["deep", "math"]

    def unreadable():
        raise RuntimeError("storage is closed")

    assert ProductivityController(productivity_service, execution_controller, task_tags=unreadable).used_tags() == []
