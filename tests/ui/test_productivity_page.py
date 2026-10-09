"""The Productivity page as real widgets against a temporary database: the section bar on top chooses one of
exactly three sections; General is filter-free boxes of awards and facts; Specific holds the Task-based and
Time-based parts, each with its own filters, which never change another part; Project lists the projects and
shows a selected one's collected points; there is no shared filter bar, history or data panel;
and the task form offers the task-type control. Skipped without a display (tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from pathlib import Path

from app.ui import tracker_view
from app.ui.projects_controller import ProjectsController
from app.ui.task_form_model import CATEGORIES, TaskDraft
from tests.ui.test_day_status_board import scheduled_day
from tests.ui.test_desktop_app import close_app, press, pump
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display


def test_three_sections_with_boxes_and_their_own_filters(tmp_path: Path, dialogs) -> None:
    app, day = scheduled_day(tmp_path / "page.db", tmp_path, ("Read", "Write"))
    try:
        press(app, day, "Read", "right")  # completed
        app.show_page("productivity")
        page = app.pages["productivity"].content
        pump(app, until=lambda: page.reports["General"] is not None)

        # Exactly three sections; the bar is the top of the page; one is shown and the others keep their state.
        assert tuple(page.section_frames) == tuple(page.section_buttons) == tracker_view.SECTIONS == (
            "General", "Specific", "Project")
        assert page.section_buttons["General"].master.grid_info()["row"] == 0
        assert page.section_frames["General"].winfo_manager() == "grid"
        assert page.section_frames["Specific"].winfo_manager() == ""
        assert tuple(page.part_frames) == ("Task-based", "Time-based")  # both former sections live in Specific
        assert page.section_buttons["General"].cget("text").endswith("General")
        assert page.section_buttons["General"].cget("text") != "General"  # marked, not coloured only
        # The shared filter bar and the history panel are gone.
        assert not any(hasattr(page, name) for name in ("apply_button", "history_detail", "range_label"))

        # General: no filters, only labelled boxes -- the five awards and the facts.
        assert len(page.award_tiles.stats) == len(tracker_view.AWARD_KINDS)
        facts = page.fact_tiles.values()
        assert facts["Tasks completed"] == "1"
        shown = [tile for tile in page.fact_tiles.tiles if tile.winfo_manager() == "grid"]
        assert len(shown) == len(facts) and shown[0].title_label.cget("text") == "Tasks completed"
        assert shown[0].value_label.cget("text") == "1"

        # Specific is read when first shown: its Task-based part has its own period, category, tag and type
        # filters, its Time-based part its own range, weekday and time-of-day filters.
        assert page.reports["Task-based"] is None and page.reports["Time-based"] is None
        page.section_buttons["Specific"].invoke()
        pump(app, until=lambda: page.reports["Task-based"] is not None and page.reports["Time-based"] is not None)
        assert page.section_frames["Specific"].winfo_manager() == "grid"
        assert all(frame.winfo_manager() == "grid" for frame in page.part_frames.values())
        assert page.section_frames["General"].winfo_manager() == ""
        report = page.reports["Task-based"]
        # The category filter offers the task form's categories; the tag filter every tag used so far.
        categories = list(page.category_menu.cget("values"))
        assert categories[:len(CATEGORIES) + 1] == ["(any)", *CATEGORIES] and set(report.categories) <= set(categories)
        used = app.productivity_controller.used_tags()
        assert list(page.tag_menu.cget("values")) == ["(any)", *sorted({*report.tags, *used}, key=str.casefold)]
        assert not hasattr(page, "reset_button")  # the Data panel (exports and delete) is not on this page
        assert {"Read", "Write"} <= set(page.type_menu.cget("values"))
        assert {"Read", "Write"} <= {stat.label for stat in page.type_cards.stats}
        page.type_var.set("Read")
        page._render_types()
        assert page.type_tiles.values()["Completed"] == "1"
        assert page.type_tiles.title_label.cget("text").startswith("Read")
        page.type_var.set("Write")
        page.period_var.set("Today")
        page._render_types()
        assert page.type_tiles.values()["Completed"] == "0"

        # The Time-based part's filters change no other part or section.
        assert page.time_tiles.values()["Completed"] == "1" and len(page.weekday_tiles.stats) == 7
        assert page.week_tiles.stats and page.month_tiles.stats and "Planned" in page.day_tiles.values()
        page.days_var.set("Last 7 days")
        page._load("Time-based")
        pump(app, until=lambda: page.reports["Time-based"].range_days == 7)
        assert page.reports["Task-based"].range_days is None and page.reports["General"].range_days is None
        page.time_reset_button.invoke()
        pump(app, until=lambda: page.reports["Time-based"].range_days is None)
        assert page.days_var.get() == "All time" and page.period_var.get() == "Today"  # the task filters keep theirs
        assert page.section == "Specific" and dialogs.errors == []

        # An answer to an older request never replaces a newer one.
        stale = page._requests["Time-based"]
        page._load("Time-based")
        page._load("Time-based")
        assert page._requests["Time-based"] == stale + 2
        pump(app)

        # Project: the projects are listed; selecting one shows its points, and the Projects page's link opens
        # this section with the project already selected (the same view and figures).
        planning = app.services.planning_controller
        projects = ProjectsController(planning, timezone=app.services.timezone,
                                      executions=app.services.execution_controller)
        project = projects.create("Thesis").value
        idle = projects.create("Idle").value
        assert projects.add_task(project.id, TaskDraft(kind="task", name="Outline", category="study", duration="30m",
                                                       points="7"), day.snapshot.day.isoformat()).ok
        outline = projects.load(project.id).value.tasks[0]
        assert projects.set_task_completed(outline.task_id, True).ok  # completed without ever being scheduled
        page.section_buttons["Project"].invoke()
        pump(app, until=lambda: len(page.project_buttons) == 2)
        assert page.section_frames["Project"].winfo_manager() == "grid" and page.project_report is None
        assert page.project_tiles.stats == [] and set(page.project_buttons) == {project.id, idle.id}
        page.project_buttons[project.id].invoke()
        pump(app, until=lambda: page.project_report is not None)
        values = page.project_tiles.values()
        assert values["Total points collected"] == "7" and values["Tasks completed"] == "1"
        assert values["Average points per day"] == "7"  # one calendar day in the period
        assert "7 points ÷ 1 calendar day" in page.project_tiles.stats[1].caption
        assert [stat.value for stat in page.project_day_tiles.stats] == ["7"]
        assert page.project_buttons[project.id].cget("text").startswith("●")
        page.project_buttons[idle.id].invoke()  # a project with nothing completed: no numbers are invented
        pump(app, until=lambda: page.project_report is not None and page.project_id == idle.id
             and not page.project_report.has_data)
        assert page.project_tiles.values()["Total points collected"] == "0"
        assert page.project_tiles.values()["Average points per day"] == tracker_view.NO_VALUE
        assert page.project_day_tiles.stats == []
        app.show_page("day")
        app.open_project_performance(project.id)
        pump(app, until=lambda: page.project_id == project.id and page.project_report is not None
             and page.project_report.has_data)
        assert app.shell.current == "productivity" and page.section == "Project"
        assert page.project_tiles.values()["Total points collected"] == "7"

        # The task form has no advanced settings: no task-type, deadline, dependency or repeat controls.
        form = app.pages["day"].form
        assert not any(hasattr(form, name) for name in (
            "more_frame", "type_select", "deadline_date", "dependency_picker", "repeat_select"))
    finally:
        close_app(app)
