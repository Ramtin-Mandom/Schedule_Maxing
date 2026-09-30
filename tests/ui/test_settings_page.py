from app.planning.preferences import OptimizerMode, PreferenceOverrides
from tests.ui.test_desktop_app import open_app, close_app, WEDNESDAY
from tests.ui.test_desktop_app import dialogs as dialogs, pytestmark as pytestmark  # noqa: F401


def test_native_default_settings_persist_and_preserve_typed_errors(tmp_path, dialogs, monkeypatch):
    from app.ui import pages
    monkeypatch.setattr(pages, "ask_confirm", lambda *args, **kwargs: True)
    path = tmp_path / "settings.db"
    app = open_app(path, tmp_path)
    try:
        planning = app.services.planning_controller
        explicit = planning.set_date_overrides(WEDNESDAY, PreferenceOverrides(optimizer_mode=OptimizerMode.PRECISE_GREEDY))
        assert explicit.ok
        app.show_page("settings")
        page = app.pages["settings"]
        assert page.language_select.values == ["English"]
        page.engine_select.choose("ADHD friendly")
        assert page.view.engine == OptimizerMode.ADHD_FRIENDLY
        assert planning.date_preferences(WEDNESDAY).value == explicit.value
        key = "reward.weight_importance"
        page.editor.set_input(key, "oops")
        page.editor.buttons[key]["save"].invoke()
        assert page.editor.value_of(key) == "oops"
        assert "number" in page.preference_notice.text
        # A concurrent preferences write is refused; the typed edit is retained for review.
        page.editor.set_input(key, "7")
        stored = planning.user_preferences().value
        assert planning.set_user_overrides(stored.overrides.model_copy(update={"category_multipliers": {"study": 3}}),
                                           expected_version=stored.version).ok
        page.editor.buttons[key]["save"].invoke()
        assert page.editor.value_of(key) == "7" and page.preference_notice.tone == "error"
        page.editor.buttons[key]["save"].invoke()
        assert planning.user_preferences().value.overrides.reward.weight_importance == 7
        assert planning.user_preferences().value.overrides.category_multipliers["study"] == 3
        app.show_page("about")
        assert "Ramtin Rezaei" in app.pages["about"].message_label.cget("text")
        retired_variable = page.engine_select.variable
    finally:
        close_app(app)
    assert retired_variable._tk is None  # a later worker GC cannot call the closed interpreter
    app = open_app(path, tmp_path)
    try:
        app.show_page("settings")
        page = app.pages["settings"]
        assert page.engine_select.get() == "ADHD friendly"
        assert page.editor.value_of("reward.weight_importance") == "7"
        page.reset_button.invoke()
        assert app.services.planning_controller.user_preferences().value is None
        assert app.services.planning_controller.date_preferences(WEDNESDAY).value is not None
    finally:
        close_app(app)


def test_reset_all_task_data_asks_first_then_clears_every_page(tmp_path, dialogs, monkeypatch):
    from app.ui import pages
    from tests.ui.test_desktop_app import fill_form, pump

    app = open_app(tmp_path / "reset.db", tmp_path)
    try:
        day = app.pages["day"]
        fill_form(day, name="Study", duration="30")
        day.form.submit_button.invoke()
        day.make_schedule_button.invoke()
        pump(app, until=lambda: not day._busy)
        assert app.services.planning_controller.list_tasks().value
        settings = app.pages["settings"]
        app.show_page("settings")
        asked = []

        monkeypatch.setattr(pages, "ask_confirm", lambda parent, **kw: (asked.append(kw), False)[1])
        settings.reset_data_button.invoke()
        assert asked[0]["title"] == "Reset all task data?" and asked[0]["danger"] is True
        assert "cannot be undone" in asked[0]["message"] and "Kept: your account" in asked[0]["message"]
        assert "nothing was deleted" in settings.reset_data_notice.text  # cancelled
        assert app.services.planning_controller.list_tasks().value

        monkeypatch.setattr(pages, "ask_confirm", lambda parent, **kw: True)
        settings.reset_data_button.invoke()
        pump(app, until=lambda: settings.reset_data_button.cget("text") == "Reset All Task Data")
        assert "All task data was removed: 1 task(s)" in settings.reset_data_notice.text
        assert app.services.planning_controller.list_tasks().value == []
        assert app.services.execution_controller.list_executions().value == []
        pump(app)
        app.show_page("day")
        pump(app)
        assert day.snapshot.rows == [] and day.status_board.board.cards == []  # no page shows stale data
    finally:
        close_app(app)
