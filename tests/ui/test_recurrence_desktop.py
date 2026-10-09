"""Recurring series in the desktop's reusable form and page controller (docs/recurrence.md), headless: the
form's Repeats fields round-trip and validate (a series starts on the page's date in its time zone; it takes no
deadline; an occurrence does not repeat itself); series rows appear (with "needs setup" for templates saved
before series repeated, which the form then configures); Make Schedule materializes the occurrences first;
edits and removals take the chosen scope; and a Make Schedule result started before an account switch is
dropped instead of reaching the new workspace."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.planning.models import OccurrenceState, RecurrenceSpec, Task
from app.planning.recurrence import occurrence_task_id
from app.planning.scope import OwnerScope
from app.ui import background
from app.ui.app_services import open_app_services
from app.ui.background import ControllerResult, WorkerRegistry, run_in_background
from app.ui.planning_controller import PlanningController
from app.ui.schedule_page_controller import RowRef, SchedulePageController
from app.ui.task_form_model import FormErrors, TaskDraft, build_task, draft_from_task
from tests.ui.test_app_services import FakeWidget, _wait_idle

MON = date(2026, 3, 2)


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


def weekly_draft(**fields) -> TaskDraft:
    return TaskDraft(**{"name": "Gym", "category": "exercise", "date": MON.isoformat(), "duration": "45",
                        "repeat": "weekly", "repeat_weekdays": (0, 3), **fields})


# -----------------------------------------------------------------------------
# The form model
# -----------------------------------------------------------------------------


def test_repeats_fields_build_a_configured_series_starting_on_the_forms_date() -> None:
    task = build_task(weekly_draft(repeat_interval="2", repeat_end="after", repeat_count="10"),
                      timezone_name="Europe/Berlin")
    assert task.recurrence == RecurrenceSpec(frequency="weekly", interval=2, weekdays=[0, 3], count=10,
                                             start_date=MON, timezone="Europe/Berlin")
    assert (task.required_date, task.preferred_dates, task.deadline) == (None, [], None)  # dated by its rule
    back = draft_from_task(task, "Europe/Berlin")
    assert (back.repeat, back.repeat_interval, back.repeat_weekdays, back.repeat_end, back.repeat_count,
            back.recurrence_role, back.date) == ("weekly", "2", (0, 3), "after", "10", "series", MON.isoformat())
    assert "every 2 weeks on Mon, Thu" in back.recurrence_note

    monthly = build_task(weekly_draft(repeat="monthly", repeat_day_of_month="31", repeat_end="on",
                                      repeat_until="2026-12-31"), timezone_name="UTC")
    assert (monthly.recurrence.day_of_month, monthly.recurrence.end_date) == (31, date(2026, 12, 31))


def test_the_repeats_fields_report_every_problem_next_to_its_field() -> None:
    with pytest.raises(FormErrors) as errors:
        build_task(weekly_draft(repeat_interval="0", repeat_end="after", repeat_count="x", deadline_date="2026-03-05",
                                deadline_time="5:00 PM"), timezone_name="UTC")
    assert {"repeat_interval", "repeat_count", "deadline_date"} <= set(errors.value.errors)
    with pytest.raises(FormErrors) as errors:
        build_task(weekly_draft(repeat_end="on"), timezone_name="UTC")
    assert "repeat_until" in errors.value.errors


def test_an_occurrence_is_edited_like_a_task_and_never_repeats_itself() -> None:
    series = Task(name="Gym", category="exercise", estimated_duration_minutes=45, priority=6,
                  recurrence=RecurrenceSpec(frequency="weekly", start_date=MON, timezone="UTC"))
    occurrence = Task(id=occurrence_task_id(series.id, MON), name="Gym", category="exercise",
                      estimated_duration_minutes=45, priority=6, required_date=MON, series_id=series.id,
                      occurrence_slot=MON, series_version=1)
    draft = draft_from_task(occurrence, "UTC", series=series)
    assert draft.recurrence_role == "occurrence" and draft.repeat == ""
    assert MON.isoformat() in draft.recurrence_note and "Gym" in draft.recurrence_note
    edited = build_task(replace(draft, name="Gym (legs)"), timezone_name="UTC", existing=occurrence)
    assert (edited.series_id, edited.occurrence_slot, edited.required_date, edited.name) == (
        series.id, MON, MON, "Gym (legs)")
    with pytest.raises(FormErrors):
        build_task(replace(draft, repeat="daily"), timezone_name="UTC", existing=occurrence)


# -----------------------------------------------------------------------------
# The page controller
# -----------------------------------------------------------------------------


@pytest.fixture
def week(tmp_path: Path):
    from app.execution.db import get_connection
    from app.planning.application import PlanningService
    from app.planning.repository import PlanningRepository

    connection = get_connection(tmp_path / "app.db")
    planning = PlanningController(service=PlanningService(PlanningRepository(connection)), timezone="UTC",
                                  project_root=str(tmp_path))
    yield SchedulePageController(planning, number_of_days=7, anchor_date=MON, timezone="UTC")
    connection.close()


def rows(page: SchedulePageController) -> dict[str, list]:
    found: dict[str, list] = {}
    for row in page.load().value.rows:
        found.setdefault(row.type_label, []).append(row)
    return found


def test_a_series_row_then_its_occurrences_after_make_schedule(week: SchedulePageController) -> None:
    assert week.save_draft(weekly_draft()).ok
    [series_row] = rows(week)["repeats"]
    assert "Mon, Thu" in series_row.time_text and "repeat" not in rows(week)

    run = week.make_schedule()
    assert run.ok, run.error
    repeats = rows(week)["repeat"]
    assert sorted(row.date for row in repeats) == [MON, MON + timedelta(days=3)]
    assert {e.placement.planned_date for e in run.value.snapshot.executables} == {MON, MON + timedelta(days=3)}
    assert week.scope_choices(series_row.ref).value == [("series", "The entire series")]
    assert [value for value, _ in week.scope_choices(repeats[0].ref).value] == ["occurrence", "future", "series"]


def test_a_template_saved_before_series_repeated_shows_needs_setup_and_the_form_configures_it(
    week: SchedulePageController,
) -> None:
    legacy = week.planning.add_or_update_task(Task(
        name="Standup", category="work", estimated_duration_minutes=15, priority=5,
        recurrence=RecurrenceSpec(frequency="daily"))).value
    [row] = rows(week)["needs setup"]
    draft = week.draft_for(row.ref).value
    assert draft.needs_configuration and draft.date == "" and draft.repeat == ""  # nothing is guessed for it
    assert week.save_draft(replace(draft, name="Daily standup"), editing=row.ref).ok  # an ordinary edit...
    renamed = week.planning.get_task(legacy.id).value
    assert renamed.name == "Daily standup" and renamed.needs_configuration  # ...does not configure it
    [row] = rows(week)["needs setup"]
    draft = week.draft_for(row.ref).value
    assert week.save_draft(replace(draft, repeat="daily"), editing=row.ref).ok  # choosing how it repeats does
    configured = week.planning.get_task(legacy.id).value.recurrence
    assert (configured.start_date, configured.timezone, configured.frequency.value) == (MON, "UTC", "daily")
    assert "needs setup" not in rows(week)


def test_edit_scopes_from_one_occurrence(week: SchedulePageController) -> None:
    week.save_draft(weekly_draft(repeat="daily", repeat_weekdays=()))
    week.make_schedule()
    by_date = {row.date: row for row in rows(week)["repeat"]}

    only_tuesday = by_date[MON + timedelta(days=1)]
    draft = week.draft_for(only_tuesday.ref).value
    assert week.save_draft(replace(draft, name="Gym (light)"), editing=only_tuesday.ref, scope="occurrence").ok
    tuesday = week.planning.get_task(only_tuesday.ref.id).value
    assert tuesday.name == "Gym (light)" and tuesday.occurrence_state == OccurrenceState.MODIFIED

    from_thursday = {row.date: row for row in rows(week)["repeat"]}[MON + timedelta(days=3)]
    draft = week.draft_for(from_thursday.ref).value
    assert week.save_draft(replace(draft, points="9"), editing=from_thursday.ref, scope="future").ok
    labels = rows(week)
    assert len(labels["repeats"]) == 2  # the series now ends on Wednesday; a new segment starts Thursday
    week.make_schedule()
    later = [row for row in rows(week)["repeat"] if row.date >= MON + timedelta(days=3)]
    assert later and all(week.planning.get_task(row.ref.id).value.points == 9 for row in later)


def test_removal_scopes_from_one_occurrence(week: SchedulePageController) -> None:
    week.save_draft(weekly_draft(repeat="daily", repeat_weekdays=()))
    week.make_schedule()
    by_date = {row.date: row for row in rows(week)["repeat"]}
    monday = by_date[MON]
    assert [value for value, _ in week.removal_choices(monday.ref).value] == ["skip", "occurrence", "future", "series"]
    assert "repeating task" in week.delete_description(monday.ref).value

    assert week.delete(monday.ref, scope="skip").ok
    week.make_schedule()  # never regenerated
    assert MON not in {row.date for row in rows(week)["repeat"]}
    skipped = week.planning.get_task_including_deleted(monday.ref.id).value
    assert skipped.occurrence_state == OccurrenceState.SKIPPED

    friday = {row.date: row for row in rows(week)["repeat"]}[MON + timedelta(days=4)]
    assert week.delete(friday.ref, scope="future").ok
    assert max(row.date for row in rows(week)["repeat"]) == MON + timedelta(days=3)

    wednesday = {row.date: row for row in rows(week)["repeat"]}[MON + timedelta(days=2)]
    assert week.delete(wednesday.ref, scope="series").ok
    labels = rows(week)
    assert "repeat" not in labels and "repeats" not in labels


def test_a_make_schedule_result_started_before_an_account_switch_is_dropped(tmp_path: Path) -> None:
    services = open_app_services(tmp_path / "app.db", timezone="UTC", project_root=str(tmp_path))
    try:
        alice = uuid.uuid4()
        page = SchedulePageController(services.planning_controller, number_of_days=7, anchor_date=MON, timezone="UTC")
        assert page.save_draft(weekly_draft(repeat="daily", repeat_weekdays=())).ok
        guard = services.workspace_guard()
        registry, widget, delivered = WorkerRegistry(), FakeWidget(), []
        release = threading.Event()

        def slow_schedule() -> ControllerResult:
            release.wait(timeout=5)
            return page.make_schedule()  # expansion and generation run in the worker, in the old workspace

        assert run_in_background(widget, slow_schedule, delivered.append, registry=registry, still_current=guard)
        services.switch_workspace(OwnerScope.account(alice))
        release.set()
        _wait_idle(registry)
        widget.run_pending()
        assert delivered == []  # the ownerless workspace's result never reaches Alice's view
        assert services.planning_controller.list_tasks().value == []  # nor did its occurrences become hers
        occurrences = [task for task in services.planning_service.list_tasks() if task.series_id is not None]
        assert len(occurrences) == 7 and {task.user_id for task in occurrences} == {None}
    finally:
        services.close()


def test_rows_identify_occurrences_by_id_not_name(week: SchedulePageController) -> None:
    week.save_draft(weekly_draft(repeat="daily", repeat_weekdays=()))
    week.make_schedule()
    refs = [row.ref for row in rows(week)["repeat"]]
    assert len({ref.id for ref in refs}) == 7 and all(isinstance(ref, RowRef) for ref in refs)
