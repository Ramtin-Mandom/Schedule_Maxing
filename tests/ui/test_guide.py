"""The How to Use page's content (app/ui/guide_content.py), headless: every topic the app offers
is documented once, in reading order, the day-status-colour section exists for the later colour
feature, and the page describes the date and time entry the forms really use."""

from __future__ import annotations

from app.ui.guide_content import GUIDE_SECTIONS, section
from app.ui.shell_state import NAV_ITEMS


def _text(key: str) -> str:
    return " ".join(block if isinstance(block, str) else " ".join(block) for block in section(key).blocks)


def test_every_topic_is_documented_once_in_reading_order() -> None:
    keys = [item.key for item in GUIDE_SECTIONS]
    assert keys == [
        "overview", "day_view", "week_view", "month_view", "projects", "project_tasks", "adding_tasks",
        "fixed_blocks", "flexible_tasks", "preferred_times", "categories", "priority_points", "dependencies",
        "make_schedule", "scheduled_unscheduled", "task_workflow", "productivity", "day_window", "settings",
        "synchronization", "reset", "day_status_colours",
    ]
    assert len(set(keys)) == len(keys) and all(item.title and item.blocks for item in GUIDE_SECTIONS)


def test_the_guide_matches_how_dates_and_times_are_entered() -> None:
    adding = _text("adding_tasks")
    assert "no date field" in adding and "[hour] : [minute] [AM/PM]" in adding
    assert "computer's clock" in _text("overview")
    window = _text("day_window")
    assert "Apply to this date" in window and "Use default" in window and "Settings" in window


def test_the_day_status_colours_are_explained_exactly() -> None:
    assert GUIDE_SECTIONS[-1].key == "day_status_colours"
    text = _text("day_status_colours")
    for phrase in ("Neutral, dark tint: no scheduled tasks", "Light white: 50% or more", "Dark red: 80% or more "
                   "uncompleted", "Light red: 60% or more uncompleted", "Light green: 60% or more completed",
                   "Dark green: 80% or more completed", "Yellow: a mixed result", "PAST date",
                   "actually scheduled"):
        assert phrase in text, phrase


def test_projects_and_their_statistics_are_explained() -> None:
    projects = _text("projects")
    for phrase in ("YYYY-MM-DD", "Ongoing projects and Completed projects", "Double-click a project",
                   "Mark complete", "View performance", "Homework (mat)"):
        assert phrase in projects, phrase
    tasks = _text("project_tasks")
    for phrase in ("Add Task, Project Tasks and Milestones", "Done: tick it", "even one that has not been scheduled",
                   "project tasks only", "Untick Done", "× on the left of a task", "then the category's",
                   "1-3 neutral, 4-7 light green, 8-9 green, 10 dark green", "× on the left of a milestone"):
        assert phrase in tasks, phrase
    productivity = _text("productivity")
    for phrase in ("General", "Specific", "Task-based", "Time-based", "Project: your projects",
                   "every calendar day of the period", "0 points and no average"):
        assert phrase in productivity, phrase
    assert "completed with Done in its project" in _text("task_workflow")


def test_points_are_explained_apart_from_the_schedulers_score() -> None:
    assert "Points (0 to 1000, default 1)" in _text("adding_tasks")
    assert "its score is never your points" in _text("priority_points")


def test_how_to_use_is_in_the_navigation_right_before_about() -> None:
    labels = [item.label for item in NAV_ITEMS]
    assert labels[-2:] == ["How to Use", "About"]
    guide = next(item for item in NAV_ITEMS if item.key == "guide")
    assert not guide.placeholder
