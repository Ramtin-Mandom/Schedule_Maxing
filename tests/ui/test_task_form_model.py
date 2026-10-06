"""The reusable task form's rules, headless (Milestone 4, Prompt 3): minute-exact time and
duration input (typed and stepped alike, AM/PM, noon, midnight, the following midnight),
drafts <-> canonical Task/FixedBlock (required fields, 10:13 and a 13-minute duration, DST
refusals without rounding, overnight refusal), and the page presenter's saves: fixed-block
window/overlap refusals that write nothing and name the other block, edits that keep
recurrence and every hidden field, duplicate names, stale versions, owner isolation,
project/dependency references by id, delete descriptions and refusals."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.planning.models import FixedBlock, Project, RecurrenceFrequency, RecurrenceSpec, Task
from app.planning.scope import OwnerScope
from app.ui import background
from app.ui.app_services import open_app_services
from app.ui.schedule_page_controller import RowRef, SchedulePageController
from app.ui.task_form_model import (
    FormErrors,
    TaskDraft,
    build_block,
    build_task,
    categories_for,
    draft_from_block,
    draft_from_task,
)
from app.ui.time_fields import (
    FieldError,
    clock_parts_text,
    clock_to_minutes,
    format_clock,
    format_duration,
    minutes_to_clock,
    parse_clock,
    parse_clock_parts,
    parse_duration,
    toggle_meridiem,
)

DAY = date(2026, 9, 23)
NY = "America/New_York"


# -----------------------------------------------------------------------------
# Times and durations
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("text, minutes", [
    ("10:13", 613), ("10:13 AM", 613), ("10:13am", 613), ("10:13 a.m.", 613), ("10:13 PM", 1333), ("22:13", 1333),
    ("10 am", 600), ("12:00 PM", 720), ("noon", 720), ("12:00 AM", 0), ("midnight", 0), ("0:00", 0), ("11:59 PM", 1439),
])
def test_typed_times_are_exact_minutes(text: str, minutes: int) -> None:
    assert parse_clock(text) == minutes


def test_end_times_may_be_the_following_midnight_and_everything_displays_as_am_pm() -> None:
    for text in ("12:00 AM", "midnight", "24:00", "12:00 AM (next day)"):
        assert parse_clock(text, end_of_interval=True) == 1440
    assert format_clock(0) == "12:00 AM" and format_clock(720) == "12:00 PM" and format_clock(613) == "10:13 AM"
    assert format_clock(1333) == "10:13 PM" and format_clock(1440) == "12:00 AM (next day)"
    assert parse_clock(format_clock(1440), end_of_interval=True) == 1440  # what is shown can be typed back


@pytest.mark.parametrize("text, message", [
    ("", "Enter a time"), ("25:00", "hour goes from 0 to 23"), ("10:60", "Minutes go from 00 to 59"),
    ("13:00 PM", "With AM/PM"), ("tea time", "is not a time"), ("1013", "is not a time"), ("24:00", "start time"),
])
def test_unreadable_times_say_what_to_type(text: str, message: str) -> None:
    with pytest.raises(FieldError, match=message):
        parse_clock(text)


# -----------------------------------------------------------------------------
# [ Hour ] : [ Minute ] [ AM/PM ] -- the shared time input's conversions
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("hour, minute, meridiem, minutes", [
    (12, 0, "AM", 0), (12, 30, "AM", 30), (1, 0, "AM", 60), (11, 59, "AM", 719), (12, 0, "PM", 720),
    (12, 59, "PM", 779), (1, 15, "PM", 795), (11, 59, "PM", 1439), (10, 13, "AM", 613),
])
def test_twelve_hour_parts_convert_to_minutes_from_midnight_and_back(hour, minute, meridiem, minutes) -> None:
    assert clock_to_minutes(hour, minute, meridiem) == minutes
    assert minutes_to_clock(minutes) == (hour, minute, meridiem)
    assert parse_clock_parts(str(hour), f"{minute:02d}", meridiem) == minutes
    assert parse_clock(clock_parts_text(str(hour), str(minute), meridiem)) == minutes  # the text the forms carry


def test_midnight_and_noon_edges() -> None:
    assert clock_to_minutes(12, 0, "AM") == 0  # 12 AM is the start of the day, not noon
    assert clock_to_minutes(12, 0, "PM") == 720  # 12 PM is noon, not midnight
    assert clock_to_minutes(12, 0, "AM", end_of_interval=True) == 1440  # an end at 12 AM is the next midnight
    assert clock_to_minutes(12, 1, "AM", end_of_interval=True) == 1  # only exactly midnight moves
    assert minutes_to_clock(1440) == (12, 0, "AM")
    assert clock_parts_text("12", "00", "AM", end_of_interval=True) == "12:00 AM (next day)"
    assert parse_clock(clock_parts_text("12", "00", "AM", end_of_interval=True), end_of_interval=True) == 1440


def test_am_pm_toggles_and_a_blank_minute_is_on_the_hour() -> None:
    assert toggle_meridiem("AM") == "PM" and toggle_meridiem(toggle_meridiem("AM")) == "AM"
    assert parse_clock_parts("9", "", "PM") == 1260
    assert parse_clock_parts(" 9 ", " 5 ", "AM") == 545  # a one-digit minute is minute 5, shown as 9:05
    assert clock_parts_text("9", "5", "AM") == "9:05 AM"
    assert clock_parts_text("", "", "PM") == ""  # nothing typed: no time, whatever the toggle says


@pytest.mark.parametrize("hour, minute, meridiem, message", [
    ("0", "30", "AM", "hour goes from 1 to 12"), ("13", "00", "PM", "hour goes from 1 to 12"),
    ("24", "00", "AM", "hour goes from 1 to 12"), ("7", "60", "AM", "Minutes go from 00 to 59"),
    ("7", "123", "AM", "Minutes go from 00 to 59"), ("x", "00", "AM", "hour is a number"),
    ("7", "3o", "PM", "minutes are a number"), ("", "30", "AM", "Enter the hour"), ("", "", "AM", "Enter a time"),
    ("-1", "00", "AM", "hour is a number"), ("7", "00", "XM", "Choose AM or PM"),
])
def test_invalid_hours_and_minutes_are_refused_with_what_to_type(hour, minute, meridiem, message) -> None:
    with pytest.raises(FieldError, match=message):
        parse_clock_parts(hour, minute, meridiem)


def test_invalid_parts_are_never_turned_into_a_time() -> None:
    for hour, minute in (("13", "00"), ("7", "60"), ("x", "5")):
        text = clock_parts_text(hour, minute, "PM")
        with pytest.raises(FieldError):
            parse_clock(text)  # the form refuses it; nothing is rounded or guessed


@pytest.mark.parametrize("text, minutes", [
    ("13", 13), ("13 min", 13), ("13m", 13), ("1 h", 60), ("1h13m", 73), ("1 h 13 min", 73), ("1:13", 73),
    ("2 hours 5 minutes", 125), ("1", 1),
])
def test_durations_are_whole_minutes_from_one_minute(text: str, minutes: int) -> None:
    assert parse_duration(text) == minutes
    assert parse_duration(format_duration(minutes)) == minutes


@pytest.mark.parametrize("text", ["0", "", "1.5h", "abc", "25 h", "1:75"])
def test_bad_durations_are_refused_not_rounded(text: str) -> None:
    with pytest.raises(FieldError):
        parse_duration(text)


# -----------------------------------------------------------------------------
# Drafts <-> models
# -----------------------------------------------------------------------------


def valid_draft(**changes) -> TaskDraft:
    base = TaskDraft(name="Read", category="study", date=DAY.isoformat(), duration="13 min", priority="7",
                     window_start="10:13 AM", window_end="11:00 AM", tags=("reading", "deep"))
    return TaskDraft(**{**base.__dict__, **changes})


def test_a_minute_precise_task_is_built_from_the_form() -> None:
    task = build_task(valid_draft(), timezone_name="UTC")
    assert task.estimated_duration_minutes == 13 and task.priority == 7
    assert (task.preferred_time_window.start_minute, task.preferred_time_window.end_minute) == (613, 660)
    assert task.preferred_dates == [DAY] and task.required_date is None and task.tags == ["reading", "deep"]
    pinned = build_task(valid_draft(pin_to_date=True, required=True), timezone_name="UTC")
    assert pinned.required_date == DAY and pinned.required and pinned.preferred_dates == []
    undated = build_task(valid_draft(date=""), timezone_name="UTC")
    assert undated.preferred_dates == [] and undated.required_date is None


def test_every_invalid_field_is_reported_at_once() -> None:
    with pytest.raises(FormErrors) as info:
        build_task(TaskDraft(name=" ", duration="0", priority="11", pin_to_date=True, window_start="9:00 AM",
                             deadline_date="2026-09-30"), timezone_name="UTC")
    assert set(info.value.errors) == {"name", "duration", "priority", "date", "window_end", "deadline_time"}


def test_windows_may_end_at_midnight_but_never_run_overnight() -> None:
    task = build_task(valid_draft(window_start="10:00 PM", window_end="12:00 AM"), timezone_name="UTC")
    assert task.preferred_time_window.end_minute == 1440
    with pytest.raises(FormErrors, match="continue past midnight"):
        build_task(valid_draft(window_start="10:00 PM", window_end="2:00 AM"), timezone_name="UTC")


def test_deadlines_and_blocks_refuse_times_skipped_or_repeated_by_daylight_saving() -> None:
    spring, fall = date(2026, 3, 8), date(2026, 11, 1)
    with pytest.raises(FormErrors, match="does not exist") as skipped:
        build_task(valid_draft(deadline_date=spring.isoformat(), deadline_time="2:30 AM"), timezone_name=NY)
    assert set(skipped.value.errors) == {"deadline_time"}
    with pytest.raises(FormErrors, match="happens twice"):
        build_task(valid_draft(deadline_date=fall.isoformat(), deadline_time="1:30 AM"), timezone_name=NY)
    deadline = build_task(valid_draft(deadline_date=DAY.isoformat(), deadline_time="5:13 PM"), timezone_name=NY).deadline
    assert deadline == datetime(2026, 9, 23, 17, 13, tzinfo=ZoneInfo(NY))  # exact, in the planning zone

    with pytest.raises(FormErrors) as gap:
        build_block(TaskDraft(kind="block", name="Gym", date=spring.isoformat(), start="2:30 AM", end="3:30 AM"),
                    timezone_name=NY)
    assert "does not exist" in gap.value.errors["start"]
    with pytest.raises(FormErrors, match="split it into two blocks"):
        build_block(TaskDraft(kind="block", name="Night", date=spring.isoformat(), start="1:00 AM", end="4:00 AM"),
                    timezone_name=NY)


def test_blocks_are_exact_and_may_end_at_the_following_midnight() -> None:
    block = build_block(TaskDraft(kind="block", name="Lab", category="lab", date=DAY.isoformat(), start="10:13 AM",
                                  end="10:26 AM"), timezone_name="UTC")
    assert block.planned_start == datetime(2026, 9, 23, 10, 13, tzinfo=timezone.utc)
    assert block.planned_end - block.planned_start == timedelta(minutes=13) and block.category == "lab"
    late = build_block(TaskDraft(kind="block", name="Late", date=DAY.isoformat(), start="11:00 PM", end="12:00 AM"),
                       timezone_name="UTC")
    assert late.planned_end == datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert draft_from_block(late).end == "12:00 AM (next day)"
    with pytest.raises(FormErrors, match="continue past midnight"):
        build_block(TaskDraft(kind="block", name="Night", date=DAY.isoformat(), start="10:00 PM", end="2:00 AM"),
                    timezone_name="UTC")


def test_editing_keeps_recurrence_hidden_fields_and_unknown_categories() -> None:
    project = uuid.uuid4()
    stored = Task(name="Gym", category="Volunteering", estimated_duration_minutes=45, priority=4,
                  preferred_dates=[DAY, DAY + timedelta(days=2)], tags=["a", "b", "c"], project_id=project,
                  recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.WEEKLY, weekdays=[0, 2]), version=3)
    draft = draft_from_task(stored, "UTC")
    assert draft.category == "Volunteering" and "Volunteering" in categories_for(draft.category)
    edited = build_task(TaskDraft(**{**draft.__dict__, "name": "Gym (evening)"}), timezone_name="UTC", existing=stored)
    assert edited.id == stored.id and edited.version == 3 and edited.created_at == stored.created_at
    assert edited.recurrence == stored.recurrence and edited.project_id == project
    assert edited.tags == ["a", "b", "c"] and edited.category == "Volunteering"
    assert edited.preferred_dates == [DAY, DAY + timedelta(days=2)]  # the further preferred date survives


def test_tags_keep_their_order_and_ignore_blanks_and_repeats() -> None:
    draft = TaskDraft().with_tag("  focus ").with_tag("math").with_tag("").with_tag("focus")
    assert draft.tags == ("focus", "math")
    assert draft.without_tag("focus").tags == ("math",)
    with pytest.raises(FieldError):
        draft.with_tag("x" * 61)


# -----------------------------------------------------------------------------
# The page presenter's saves
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


@pytest.fixture
def services(tmp_path: Path):
    opened = open_app_services(tmp_path / "form.db", timezone="UTC", project_root=str(tmp_path))
    yield opened
    opened.close()


def page(services) -> SchedulePageController:
    return SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=DAY, timezone="UTC")


def ok(result):
    assert result.ok, result.error
    return result.value


def block_draft(name: str, start: str, end: str) -> TaskDraft:
    return TaskDraft(kind="block", name=name, category="class", date=DAY.isoformat(), start=start, end=end)


def rows(controller) -> dict[str, RowRef]:
    return {row.name: row.ref for row in ok(controller.load()).rows}


@pytest.mark.parametrize("new_date", ["", "2026-09-24"])
def test_edit_date_replaces_or_clears_persisted_calendar_date(services, new_date):
    from app.planning.application import task_planned_date

    controller = page(services)
    ok(controller.save_draft(valid_draft()))
    ref = rows(controller)["Read"]
    draft = ok(controller.draft_for(ref))
    ok(controller.save_draft(replace(draft, date=new_date), editing=ref))
    saved = ok(services.planning_controller.get_task(ref.id))
    expected = date.fromisoformat(new_date) if new_date else None
    assert saved.preferred_dates == ([expected] if expected else [])
    assert task_planned_date(saved) == expected
    assert ok(controller.draft_for(RowRef("task", saved.id, saved.version))).date == new_date


def test_date_edit_preserves_extra_preferences_except_explicit_clear():
    stored = build_task(valid_draft(), timezone_name="UTC")
    later = DAY + timedelta(days=7)
    stored = stored.model_copy(update={"preferred_dates": [DAY, later]})
    draft = draft_from_task(stored, "UTC")
    moved = build_task(replace(draft, date=(DAY + timedelta(days=1)).isoformat()),
                       timezone_name="UTC", existing=stored)
    assert moved.preferred_dates == [DAY + timedelta(days=1), later]
    cleared = build_task(replace(draft, date=""), timezone_name="UTC", existing=stored)
    assert cleared.preferred_dates == []


def test_fixed_block_refusals_write_nothing_and_name_the_other_block(services) -> None:
    controller = page(services)
    ok(controller.save_draft(block_draft("Lecture", "9:00 AM", "10:30 AM")))
    before = [tuple(r) for r in services.connection.execute("SELECT * FROM fixed_blocks")]
    dirty = [tuple(r) for r in services.connection.execute("SELECT * FROM sync_dirty")]

    refused = controller.save_draft(block_draft("Gym", "10:13 AM", "11:00 AM"))
    assert not refused.ok and "overlaps the fixed block “Lecture” (9:00 AM – 10:30 AM" in refused.error
    assert [tuple(r) for r in services.connection.execute("SELECT * FROM fixed_blocks")] == before
    assert [tuple(r) for r in services.connection.execute("SELECT * FROM sync_dirty")] == dirty
    assert set(rows(controller)) == {"Lecture"}  # no phantom row

    ok(controller.save_draft(block_draft("Gym", "10:30 AM", "10:43 AM")))  # adjacent, 13 minutes: fine
    lecture = rows(controller)["Lecture"]
    ok(controller.save_draft(block_draft("Lecture", "9:13 AM", "10:30 AM"), editing=lecture))  # excluding itself
    invalid = controller.save_draft(block_draft("Gym", "11:00 AM", "10:00 AM"))
    assert not invalid.ok and set(invalid.cause.errors) == {"end"}


def test_duplicate_names_references_by_id_and_edits_that_keep_everything(services) -> None:
    controller = page(services)
    project = services.planning_controller._service.create_project(Project(name="Thesis"))
    ok(controller.save_draft(valid_draft(name="Study")))
    ok(controller.save_draft(valid_draft(name="Study", date=(DAY + timedelta(days=1)).isoformat())))
    options = ok(controller.editor_options())
    labels = [choice.label for choice in options.dependencies]
    assert len(labels) == 2 and len(set(labels)) == 2  # duplicate names are told apart
    assert [choice.label for choice in options.projects] == ["Thesis"]

    first = options.dependencies[0].id
    ok(controller.save_draft(valid_draft(name="Review", dependency_ids=(first,), project_id=project.id)))
    review = next(t for t in ok(services.planning_controller.list_tasks()) if t.name == "Review")
    assert review.dependency_ids == [first] and review.project_id == project.id
    assert all(choice.id != review.id for choice in ok(controller.editor_options(rows(controller)["Review (The)"])).dependencies)

    # A recurring template with more fields than the form shows keeps all of them through an edit.
    services.planning_controller._service.update_task(
        review.model_copy(update={"recurrence": RecurrenceSpec(frequency=RecurrenceFrequency.DAILY),
                                  "tags": ["x", "y", "z"]}), expected_version=review.version)
    ref = rows(controller)["Review (The)"]
    draft = ok(controller.draft_for(ref))
    ok(controller.save_draft(TaskDraft(**{**draft.__dict__, "duration": "1 h 13 min"}), editing=ref))
    saved = ok(services.planning_controller.get_task(review.id))
    assert saved.estimated_duration_minutes == 73 and saved.recurrence.frequency == RecurrenceFrequency.DAILY
    assert saved.tags == ["x", "y", "z"] and saved.dependency_ids == [first] and saved.project_id == project.id


def test_stale_versions_type_changes_and_deletes_with_dependents_are_refused(services) -> None:
    controller = page(services)
    ok(controller.save_draft(valid_draft(name="Base")))
    ref = rows(controller)["Base"]
    task = ok(services.planning_controller.get_task(ref.id))
    ok(services.planning_controller.add_or_update_task(task.model_copy(update={"priority": 2}),
                                                       expected_version=task.version))  # changed elsewhere
    stale = controller.save_draft(valid_draft(name="Base renamed"), editing=ref)
    assert not stale.ok and ok(services.planning_controller.get_task(ref.id)).name == "Base"

    fresh = rows(controller)["Base"]
    converted = controller.save_draft(block_draft("Base", "9:00 AM", "10:00 AM"), editing=fresh)
    assert not converted.ok and "cannot become a fixed block" in converted.error

    ok(controller.save_draft(valid_draft(name="After", dependency_ids=(fresh.id,))))
    assert "saved schedule entries are removed too" in ok(controller.delete_description(fresh))
    refused = controller.delete(rows(controller)["Base"])
    assert not refused.ok and "After" in refused.error and "depend" in refused.error
    assert "Base" in rows(controller)


def test_an_account_workspace_owns_new_records_and_never_offers_other_owners(services) -> None:
    alice = uuid.uuid4()
    services.planning_service.create_task(Task(name="Someone else's", category="study",
                                               estimated_duration_minutes=10, priority=1, user_id=uuid.uuid4()))
    services.switch_workspace(OwnerScope.account(alice))
    controller = page(services)
    ok(controller.save_draft(valid_draft(name="Mine")))
    ok(controller.save_draft(block_draft("My block", "8:00 AM", "8:13 AM")))
    assert {t.user_id for t in ok(services.planning_controller.list_tasks())} == {alice}
    assert [b.user_id for b in ok(services.planning_controller.get_fixed_blocks(DAY))] == [alice]
    assert [c.label for c in ok(controller.editor_options()).dependencies] == ["Mine (Wed Sep 23)"]


def test_a_block_in_another_timezone_is_edited_in_its_own_zone(services) -> None:
    tokyo_start = datetime(2026, 9, 23, 9, tzinfo=ZoneInfo("Asia/Tokyo"))
    block = services.planning_service.create_fixed_block(FixedBlock(
        label="Call", category="work", planned_date=DAY, timezone="Asia/Tokyo", planned_start=tokyo_start,
        planned_end=tokyo_start + timedelta(hours=1)))
    draft = draft_from_block(block)
    assert (draft.start, draft.end) == ("9:00 AM", "10:00 AM")
    moved = build_block(TaskDraft(**{**draft.__dict__, "start": "9:13 AM"}), timezone_name="UTC", existing=block)
    assert moved.timezone == "Asia/Tokyo" and moved.planned_start == tokyo_start + timedelta(minutes=13)
