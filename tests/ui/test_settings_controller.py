from datetime import timedelta

from app.planning.preferences import OptimizerMode, PreferenceOverrides
from app.planning.workflow import Freshness
from app.ui.account_controller import masked_email, profile_summary
from app.ui.settings_controller import SettingsController
from tests.ui.test_day_controller import services as services, db_path as db_path, ok, DAY, add_task  # noqa: F401


def test_defaults_ignore_date_layer_preserve_overrides_and_mark_schedule_stale(services):
    planning = services.planning_controller
    settings = SettingsController(planning, today=lambda: DAY)
    explicit = ok(planning.set_date_overrides(DAY, PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY)))
    view = ok(settings.load())
    assert view.engine == OptimizerMode.PRECISE_GREEDY
    other = DAY + timedelta(days=1)
    add_task(services, "Scheduled", day=other)
    ok(planning.generate(other, other))
    before = ok(planning.get_placements(other))
    view = ok(settings.set_engine(view, OptimizerMode.ADHD_FRIENDLY))
    assert ok(planning.date_preferences(DAY)) == explicit
    assert ok(planning.day_freshness([other]))[other].status == Freshness.STALE
    assert ok(planning.get_placements(other)) == before
    assert "reward.short_gap_bonus_weight" in [row.spec.key for row in view.rows]
    view = ok(settings.change(view, "reward.short_gap_bonus_weight", "save", "0"))
    assert view.layer.reward.short_gap_bonus_weight == 0
    view = ok(settings.reset(view))
    assert view.version is None and view.engine == OptimizerMode.PRECISE_GREEDY
    assert ok(planning.date_preferences(DAY)) == explicit


def test_absent_value_null_and_stale_writes(services):
    settings = SettingsController(services.planning_controller, today=lambda: DAY)
    initial = ok(settings.load())
    changed = ok(settings.change(initial, "category_multipliers:study", "save", "2"))
    assert changed.layer.category_multipliers["study"] == 2
    assert not settings.change(initial, "category_multipliers:study", "save", "4").ok
    cleared = ok(settings.change(changed, "category_multipliers:study", "clear"))
    assert cleared.layer.category_multipliers["study"] is None
    inherited = ok(settings.change(cleared, "category_multipliers:study", "inherit"))
    assert "study" not in inherited.layer.category_multipliers
    assert not settings.change(inherited, "reward.weight_importance", "save", "nan").ok
    assert ok(settings.load()).layer == inherited.layer


def test_related_tags_are_editable_and_capabilities_exclude_unused_weights(services):
    settings = SettingsController(services.planning_controller, today=lambda: DAY)
    view = ok(settings.load())
    keys = {row.spec.key for row in view.rows}
    assert "reward.tag_relations" in keys
    assert "reward.weight_category_bonus" not in keys
    assert "reward.short_gap_bonus_weight" not in keys
    view = ok(settings.change(view, "reward.tag_relations", "save", "study = reading, writing; exercise = walk"))
    assert view.layer.reward.tag_relations == {"study": ["reading", "writing"], "exercise": ["walk"]}
    assert not settings.change(view, "reward.tag_relations", "save", "invalid syntax").ok
    view = ok(settings.change(view, "reward.tag_relations", "save", ""))
    assert view.layer.reward.tag_relations == {}
    view = ok(settings.change(view, "reward.tag_relations", "inherit"))
    assert view.layer.reward.tag_relations is None


def test_profile_uses_real_identity_masked_email_and_plan_fallback():
    assert masked_email("ramtin@example.com") == "r***@e***"
    assert masked_email(None) == "Email unavailable"
    text = profile_summary({"display_name": "Ramtin", "email": "ramtin@example.com"})
    assert text == "Ramtin\nr***@e***\nPlan: Normal"
    assert "Plan: Team" in profile_summary({"plan": "Team"})
    assert "No display name set" in profile_summary({})
