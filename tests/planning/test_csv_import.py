"""Tests for app/planning/csv_import.py and PlanningService.apply_import: the
transactional CSV boundary -- complete validation before any write,
relationship resolution across the whole file (never guessing duplicate
names), append vs replace scope, duplicate-id behavior, history-linked
records, and all-or-nothing writes (including a replace's deletions).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.execution.service import ExecutionService
from app.planning.application import PlanningService
from app.planning.compat import import_legacy_csv_rows
from app.planning.csv_import import CsvImportError, parse_legacy_csv, parse_legacy_csv_file
from app.planning.errors import DuplicateEntityError, EntityInUseError, InvalidEntityError
from app.planning.models import ScheduledTask

ANCHOR = date(2026, 1, 5)
HEADER = "date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"


def parse(body: str, *, tz: str = "UTC", anchor: date = ANCHOR):
    return parse_legacy_csv(HEADER + body, anchor_date=anchor, timezone=tz)


def issues(body: str, **kwargs) -> list[str]:
    with pytest.raises(CsvImportError) as info:
        parse(body, **kwargs)
    return [str(issue) for issue in info.value.issues]


def apply(service: PlanningService, parsed, *, replace: bool = False):
    return service.apply_import(
        parsed.tasks, parsed.fixed_blocks, replace_range=(parsed.start_date, parsed.end_date) if replace else None
    )


# -----------------------------------------------------------------------------
# Parsing into canonical models (the compat conversion contract)
# -----------------------------------------------------------------------------


def test_rows_become_canonical_tasks_and_blocks_on_real_dates() -> None:
    parsed = parse_legacy_csv_file("samples/inputs/valid_multi_day_two_days.csv", anchor_date=ANCHOR, timezone="UTC")

    assert (parsed.start_date, parsed.end_date) == (date(2026, 1, 5), date(2026, 1, 6))
    assert len(parsed.fixed_blocks) == 8 and len(parsed.tasks) == 4
    research = next(task for task in parsed.tasks if task.name == "Research Topic")
    assert research.preferred_dates == [date(2026, 1, 5)]
    assert (research.preferred_time_window.start_minute, research.preferred_time_window.end_minute) == (540, 720)
    assert research.tags == ["writing"] and research.estimated_duration_minutes == 90 and research.priority == 8
    write = next(task for task in parsed.tasks if task.name == "Write Draft")
    assert write.dependency_ids == [research.id]
    sleep = parsed.fixed_blocks[0]
    assert sleep.planned_start == datetime(2026, 1, 5, tzinfo=timezone.utc)


def test_timezone_and_1440_boundary_follow_the_compat_contract() -> None:
    parsed = parse("1,Late,work,,true,1380,1440,60,0,\n", tz="America/New_York")
    [block] = parsed.fixed_blocks
    assert block.planned_start == datetime(2026, 1, 6, 4, tzinfo=timezone.utc)  # 23:00 EST
    assert block.planned_end == datetime(2026, 1, 6, 5, tzinfo=timezone.utc)  # next local midnight


def test_minute_precise_values_are_accepted() -> None:
    [task] = parse("1,Quick,work,,false,613,616,3,5,\n").tasks
    assert task.estimated_duration_minutes == 3


def test_utf8_bom_and_blank_lines_are_tolerated(tmp_path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(("\ufeff" + HEADER + "1,A,study,,false,540,600,30,5,\n\n").encode("utf-8"))
    assert [task.name for task in parse_legacy_csv_file(path, anchor_date=ANCHOR, timezone="UTC").tasks] == ["A"]


def test_every_import_mints_new_ids() -> None:
    body = "1,A,study,,false,540,600,30,5,\n1,Sleep,sleep,,true,0,480,0,0,\n"
    first, second = parse(body), parse(body)
    assert first.tasks[0].id != second.tasks[0].id
    assert first.fixed_blocks[0].id != second.fixed_blocks[0].id


# -----------------------------------------------------------------------------
# Validation of the complete file
# -----------------------------------------------------------------------------


def test_invalid_late_row_is_reported_with_its_line_and_rejects_the_whole_file() -> None:
    body = "1,A,study,,false,540,600,30,5,\n" * 5 + "3,Broken,study,,false,540,600,30,11,\n"
    assert issues(body) == ["line 7: priority must be between 1 and 10, got 11"]


@pytest.mark.parametrize(
    "row, message",
    [
        ("0,A,study,,false,540,600,30,5,", "day index"),
        ("x,A,study,,false,540,600,30,5,", "date must be a whole number"),
        ("1,,study,,false,540,600,30,5,", "name is required"),
        ("1,A,study,,maybe,540,600,30,5,", "fixed must be true or false"),
        ("1,A,study,,false,600,540,30,5,", "must be after start_time"),
        ("1,A,study,,false,540,1500,30,5,", "within 0-1440"),
        ("1,A,study,,false,540,600,0,5,", "duration must be greater than 0"),
        ("1,A,study,,false,540,600,,5,", "duration is required"),
        ("1,A,,,false,540,600,30,5,", "category is required"),
    ],
)
def test_malformed_values_are_errors(row: str, message: str) -> None:
    [issue] = issues(row + "\n")
    assert issue.startswith("line 2:") and message in issue


def test_missing_columns_and_empty_files_are_errors() -> None:
    with pytest.raises(CsvImportError, match="missing required column"):
        parse_legacy_csv("name,fixed\nA,false\n", anchor_date=ANCHOR, timezone="UTC")
    assert issues("") == ["the CSV contains no rows"]
    [timezone_issue] = issues("1,A,study,,false,540,600,30,5,\n", tz="Mars/Olympus")
    assert "Mars/Olympus" in timezone_issue


def test_overlapping_fixed_blocks_in_the_file_are_errors() -> None:
    [issue] = issues("1,Class,event,,true,540,660,0,0,\n1,Lab,event,,true,600,720,0,0,\n2,Lab,event,,true,600,720,0,0,\n")
    assert "overlaps" in issue and issue.startswith("line 3")


# -----------------------------------------------------------------------------
# Relationships: never weakened, never guessed
# -----------------------------------------------------------------------------


def test_missing_dependency_is_an_error_not_silently_dropped() -> None:
    [issue] = issues("1,Review,study,,false,540,600,30,5,Nonexistent Task\n")
    assert "'Nonexistent Task'" in issue and "does not match" in issue
    # The standalone compat adapter keeps its legacy drop-and-report behavior.
    rows = [dict(zip(HEADER.strip().split(","), "1,Review,study,,false,540,600,30,5,Nonexistent Task".split(",")))]
    result = import_legacy_csv_rows(rows, anchor_date=ANCHOR, tz_name="UTC")[1]
    assert [d.kind for d in result.diagnostics] == ["missing"]


def test_ambiguous_duplicate_names_are_errors() -> None:
    body = (
        "1,Study,study,,false,540,660,60,8,\n"
        "1,Study,study,,false,780,900,60,6,\n"
        "1,Follow Up,study,,false,960,1020,30,7,Study\n"
    )
    [issue] = issues(body)
    assert issue.startswith("line 4") and "more than one" in issue


def test_same_day_name_wins_and_cross_day_references_resolve_when_unique() -> None:
    body = (
        "1,Study,study,,false,540,660,60,8,\n"
        "2,Study,study,,false,540,660,60,8,\n"
        "2,Quiz,study,,false,780,840,30,5,Study\n"  # same-day Study (day 2), not ambiguous
        "1,Outline,study,,false,700,760,30,5,\n"
        "3,Essay,study,,false,540,660,60,5,Outline\n"  # cross-day, unique
    )
    parsed = parse(body)
    by_name_day = {(t.name, t.preferred_dates[0].day): t for t in parsed.tasks}
    assert by_name_day[("Quiz", 6)].dependency_ids == [by_name_day[("Study", 6)].id]
    assert by_name_day[("Essay", 7)].dependency_ids == [by_name_day[("Outline", 5)].id]


def test_cross_day_reference_to_a_name_used_on_several_other_days_is_ambiguous() -> None:
    body = "1,Study,study,,false,540,660,60,8,\n2,Study,study,,false,540,660,60,8,\n3,Exam,study,,false,540,600,30,5,Study\n"
    assert "more than one" in issues(body)[0]


def test_hyphenated_names_and_hyphen_lists_keep_the_compat_rules() -> None:
    parsed = parse_legacy_csv_file("samples/inputs/dependency_name_contains_hyphen.csv", anchor_date=ANCHOR, timezone="UTC")
    tasks = {task.name: task for task in parsed.tasks}
    assert tasks["Study Session"].dependency_ids == [tasks["Pre-Calc Review"].id]

    listed = parse("1,A,s,,false,0,60,30,5,\n1,B,s,,false,0,60,30,5,\n1,C,s,,false,60,120,30,5,A-B\n")
    by_name = {task.name: task for task in listed.tasks}
    assert by_name["C"].dependency_ids == [by_name["A"].id, by_name["B"].id]


def test_self_references_and_cycles_are_errors() -> None:
    assert "depends on itself" in issues("1,Loop,s,,false,0,60,30,5,Loop\n")[0]
    assert "cycle" in issues(
        "1,A,s,,false,0,60,30,5,B\n1,B,s,,false,0,60,30,5,A\n"
    )[0]
    sample = Path("samples/inputs/dependency_cycle_invalid.csv").read_text(encoding="utf-8").split("\n", 1)[1]
    assert issues(sample)


# -----------------------------------------------------------------------------
# Writing: append / replace, all-or-nothing
# -----------------------------------------------------------------------------


def test_append_persists_tasks_dependencies_and_blocks(planning_service: PlanningService) -> None:
    parsed = parse_legacy_csv_file("samples/inputs/valid_multi_day_two_days.csv", anchor_date=ANCHOR, timezone="UTC")

    result = apply(planning_service, parsed)

    assert {t.id for t in planning_service.list_tasks()} == {t.id for t in parsed.tasks}
    assert len(planning_service.list_fixed_blocks()) == 8
    stored = {t.name: t for t in planning_service.list_tasks()}
    assert stored["Submit Assignment"].dependency_ids == [stored["Edit Draft"].id]
    assert result.cleared is None and len(result.tasks) == 4


def test_appending_twice_creates_distinct_entities(planning_service: PlanningService) -> None:
    body = "1,A,study,,false,540,600,30,5,\n"
    apply(planning_service, parse(body))
    apply(planning_service, parse(body))
    assert [task.name for task in planning_service.list_tasks()] == ["A", "A"]


def test_reapplying_the_same_parsed_ids_is_a_duplicate_and_changes_nothing(planning_service: PlanningService) -> None:
    parsed = parse("1,A,study,,false,540,600,30,5,\n1,Sleep,sleep,,true,0,480,0,0,\n")
    apply(planning_service, parsed)
    with pytest.raises(DuplicateEntityError):
        apply(planning_service, parsed)
    assert len(planning_service.list_tasks()) == 1 and len(planning_service.list_fixed_blocks()) == 1


def test_append_rejects_blocks_overlapping_saved_blocks(planning_service: PlanningService) -> None:
    apply(planning_service, parse("1,Sleep,sleep,,true,0,480,0,0,\n"))
    with pytest.raises(InvalidEntityError, match="overlaps the saved fixed block"):
        apply(planning_service, parse("1,Nap,sleep,,true,420,500,0,0,\n1,A,study,,false,540,600,30,5,\n"))
    assert [task.name for task in planning_service.list_tasks()] == []


def test_replace_only_affects_the_files_date_span(planning_service: PlanningService) -> None:
    apply(planning_service, parse(
        "1,Mon task,study,,false,540,600,30,5,\n2,Tue task,study,,false,540,600,30,5,\n"
        "4,Thu task,study,,false,540,600,30,5,\n2,Tue block,event,,true,0,60,0,0,\n"
    ))
    undated = planning_service.save_task(
        planning_service.list_tasks()[0].model_copy(update={"id": uuid.uuid4(), "name": "Undated", "preferred_dates": []})
    )

    # The new file covers days 2..3 (Tue..Wed).
    result = apply(planning_service, parse("2,New Tue,study,,false,540,600,30,5,\n3,New Wed,study,,false,540,600,30,5,\n"),
                   replace=True)

    names = sorted(task.name for task in planning_service.list_tasks())
    assert names == ["Mon task", "New Tue", "New Wed", "Thu task", undated.name]
    assert planning_service.list_fixed_blocks() == []
    assert result.cleared.deleted_tasks == 1 and result.cleared.deleted_fixed_blocks == 1


def test_replace_keeps_execution_history_and_reports_linked_placements(
    planning_service: PlanningService, execution_service: ExecutionService
) -> None:
    [task] = apply(planning_service, parse("1,Worked,study,,false,540,600,30,5,\n")).tasks
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    [placement] = planning_service.replace_placements(ANCHOR, ANCHOR, [
        ScheduledTask(task_id=task.id, planned_date=ANCHOR, timezone="UTC", planned_start=start,
                      planned_end=start.replace(hour=10)),
    ]).placements
    execution = execution_service.get_or_create_canonical_execution(task, placement)
    execution_service.start(execution.id)

    result = apply(planning_service, parse("1,Fresh,study,,false,540,600,30,5,\n"), replace=True)

    assert result.cleared.placements_with_history == 1
    history = execution_service.get_execution(execution.id)
    assert history.task_name == "Worked" and history.status.value == "in_progress"
    assert history.task_id == task.id and history.scheduled_task_id == placement.id
    assert [t.name for t in planning_service.list_tasks()] == ["Fresh"]


def test_replace_is_refused_when_an_outside_task_depends_on_a_replaced_one(planning_service: PlanningService) -> None:
    [inside] = apply(planning_service, parse("1,Inside,study,,false,540,600,30,5,\n")).tasks
    outside = planning_service.save_task(inside.model_copy(update={
        "id": uuid.uuid4(), "name": "Outside", "preferred_dates": [date(2026, 2, 1)],
        "dependency_ids": [inside.id],
    }))

    with pytest.raises(EntityInUseError):
        apply(planning_service, parse("1,Replacement,study,,false,540,600,30,5,\n"), replace=True)

    assert {t.name for t in planning_service.list_tasks()} == {"Inside", outside.name}


def test_injected_write_failure_rolls_back_everything_including_replace_deletions(
    planning_service: PlanningService, planning_repository, monkeypatch
) -> None:
    apply(planning_service, parse("1,Keep me,study,,false,540,600,30,5,\n1,Keep block,event,,true,0,60,0,0,\n"))
    before = (planning_service.list_tasks(), planning_service.list_fixed_blocks())

    original = planning_repository.insert_task
    calls = {"count": 0}

    def fail_late(task):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("disk full (injected)")
        original(task)

    monkeypatch.setattr(planning_repository, "insert_task", fail_late)
    replacement = parse("1,New A,study,,false,540,600,30,5,\n1,New B,study,,false,600,660,30,5,\n1,Blk,event,,true,0,30,0,0,\n")
    with pytest.raises(RuntimeError, match="injected"):
        apply(planning_service, replacement, replace=True)

    assert (planning_service.list_tasks(), planning_service.list_fixed_blocks()) == before
