"""The Productivity page as real widgets against a temporary database: the section bar on top chooses one of
exactly three sections; General is filter-free boxes of awards and facts; Task-based and Time-based each have
their own filters, which never change another section; there is no shared filter bar, history or data panel;
and the task form offers the task-type control. Skipped without a display (tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from pathlib import Path

from app.ui import tracker_view
from app.ui.task_editor import NEW_TYPE, OWN_TYPE
from app.ui.task_form_model import CATEGORIES
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
            "General", "Task-based", "Time-based")
        assert page.section_buttons["General"].master.grid_info()["row"] == 0
        assert page.section_frames["General"].winfo_manager() == "grid"
        assert page.section_frames["Task-based"].winfo_manager() == ""
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

        # Task-based is read when first shown, with its own period, category, tag and type filters.
        assert page.reports["Task-based"] is None
        page.section_buttons["Task-based"].invoke()
        pump(app, until=lambda: page.reports["Task-based"] is not None)
        assert page.section_frames["Task-based"].winfo_manager() == "grid"
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

        # Time-based has its own range, weekday and time-of-day filters; they change no other section.
        page.section_buttons["Time-based"].invoke()
        pump(app, until=lambda: page.reports["Time-based"] is not None)
        assert page.section_frames["Time-based"].winfo_manager() == "grid"
        assert page.time_tiles.values()["Completed"] == "1" and len(page.weekday_tiles.stats) == 7
        assert page.week_tiles.stats and page.month_tiles.stats and "Planned" in page.day_tiles.values()
        page.days_var.set("Last 7 days")
        page._load("Time-based")
        pump(app, until=lambda: page.reports["Time-based"].range_days == 7)
        assert page.reports["Task-based"].range_days is None and page.reports["General"].range_days is None
        page.time_reset_button.invoke()
        pump(app, until=lambda: page.reports["Time-based"].range_days is None)
        assert page.days_var.get() == "All time" and page.period_var.get() == "Today"  # the task filters keep theirs
        assert page.section == "Time-based" and dialogs.errors == []

        # An answer to an older request never replaces a newer one.
        stale = page._requests["Time-based"]
        page._load("Time-based")
        page._load("Time-based")
        assert page._requests["Time-based"] == stale + 2
        pump(app)

        # The task form offers the type control without a redesign: keep its own type, pick one, or name a new one.
        form = app.pages["day"].form
        values = form.type_select.values
        assert values[0] == OWN_TYPE and values[-1] == NEW_TYPE and {"Read", "Write"} <= set(values)
        form.type_select.choose(NEW_TYPE)
        assert form.new_type_field.winfo_manager() == "grid"
        form.type_select.choose(OWN_TYPE)
        assert form.new_type_field.winfo_manager() == ""
    finally:
        close_app(app)
