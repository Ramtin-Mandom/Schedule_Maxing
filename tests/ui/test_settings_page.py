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
