"""Tests for app/planning/preferences.py: DayPreferences layering/resolution,
the legacy RewardSettings adapter, and preferred-window precedence.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.planning.models import LocalTimeWindow, Task
from app.planning.preferences import (
    DayWindowSpec,
    OptimizerMode,
    PreferenceOverrides,
    RewardPreferences,
    RewardPreferencesOverride,
    day_preferences_overrides_from_reward_settings,
    day_preferences_to_reward_settings,
    effective_task_preferred_window,
    resolve_day_preferences,
    to_legacy_scoring_task,
)
from app.planning.time import UnsupportedSchedulingWindowError
from app.reward import RewardSettings, calculate_task_score

DAY = date(2024, 6, 3)


def _task(**overrides) -> Task:
    defaults = dict(name="Study", category="study", estimated_duration_minutes=60, priority=4)
    defaults.update(overrides)
    return Task(**defaults)


# -----------------------------------------------------------------------------
# Neutral defaults and exact multiplier effects
# -----------------------------------------------------------------------------


def test_neutral_multiplier_leaves_score_unchanged():
    prefs = resolve_day_preferences(date=DAY, timezone="UTC")
    settings = day_preferences_to_reward_settings(prefs)
    task = {"fixed": False, "duration": 60, "priority": 4, "category": "health", "name": "X"}

    score = calculate_task_score(task, start_time=0, settings=settings)

    assert score == 4 * settings.weight_importance  # category_multiplier=1.0, task_multiplier=1.0


def test_health_1_3_override_has_exact_effect():
    baseline_prefs = resolve_day_preferences(date=DAY, timezone="UTC")
    boosted_prefs = resolve_day_preferences(
        date=DAY, timezone="UTC", date_overrides=PreferenceOverrides(category_multipliers={"health": 1.3})
    )
    task = {"fixed": False, "duration": 60, "priority": 4, "category": "health", "name": "X"}

    baseline_score = calculate_task_score(task, start_time=0, settings=day_preferences_to_reward_settings(baseline_prefs))
    boosted_score = calculate_task_score(task, start_time=0, settings=day_preferences_to_reward_settings(boosted_prefs))

    assert boosted_score == pytest.approx(baseline_score * 1.3)


def test_unknown_category_remains_usable_with_neutral_fallback():
    prefs = resolve_day_preferences(date=DAY, timezone="UTC")
    assert prefs.effective_category_multiplier("some_imported_category") == 1.0
    assert prefs.category_preferred_windows.get("some_imported_category") is None


# -----------------------------------------------------------------------------
# Layering: built-in -> yaml -> user -> date, absent/value/None semantics
# -----------------------------------------------------------------------------


def test_layers_apply_in_increasing_precedence():
    yaml_layer = PreferenceOverrides(category_multipliers={"study": 1.1, "work": 1.2})
    user_layer = PreferenceOverrides(category_multipliers={"work": 1.5})
    date_layer = PreferenceOverrides(category_multipliers={"study": 2.0})

    prefs = resolve_day_preferences(
        date=DAY, timezone="UTC", yaml_overrides=yaml_layer, user_overrides=user_layer, date_overrides=date_layer
    )

    assert prefs.category_multipliers["study"] == 2.0  # date overrides yaml
    assert prefs.category_multipliers["work"] == 1.5  # user overrides yaml


def test_absent_key_inherits_from_lower_layer():
    yaml_layer = PreferenceOverrides(category_multipliers={"study": 1.1})
    date_layer = PreferenceOverrides(category_multipliers={"work": 1.5})  # says nothing about "study"

    prefs = resolve_day_preferences(date=DAY, timezone="UTC", yaml_overrides=yaml_layer, date_overrides=date_layer)

    assert prefs.category_multipliers["study"] == 1.1
    assert prefs.category_multipliers["work"] == 1.5


def test_explicit_zero_multiplier_is_preserved_not_treated_as_missing():
    date_layer = PreferenceOverrides(category_multipliers={"chores": 0.0})
    prefs = resolve_day_preferences(date=DAY, timezone="UTC", date_overrides=date_layer)

    assert prefs.category_multipliers["chores"] == 0.0
    assert prefs.effective_category_multiplier("chores") == 0.0


def test_explicit_none_clears_a_lower_layers_override():
    yaml_layer = PreferenceOverrides(category_multipliers={"health": 1.5})
    date_layer = PreferenceOverrides(category_multipliers={"health": None})

    prefs = resolve_day_preferences(date=DAY, timezone="UTC", yaml_overrides=yaml_layer, date_overrides=date_layer)

    assert "health" not in prefs.category_multipliers
    assert prefs.effective_category_multiplier("health") == 1.0  # falls back to neutral, not to the cleared yaml value


def test_explicit_none_on_preferred_window_clears_it():
    yaml_layer = PreferenceOverrides(
        category_preferred_windows={"study": LocalTimeWindow(start_minute=480, end_minute=600)}
    )
    date_layer = PreferenceOverrides(category_preferred_windows={"study": None})

    prefs = resolve_day_preferences(date=DAY, timezone="UTC", yaml_overrides=yaml_layer, date_overrides=date_layer)

    assert prefs.category_preferred_windows.get("study") is None


def test_reward_scalar_fields_layer_independently():
    yaml_layer = PreferenceOverrides(
        reward=RewardPreferencesOverride(weight_importance=10.0, weight_time_bonus=6.0)
    )
    date_layer = PreferenceOverrides(reward=RewardPreferencesOverride(weight_importance=20.0))

    prefs = resolve_day_preferences(date=DAY, timezone="UTC", yaml_overrides=yaml_layer, date_overrides=date_layer)

    assert prefs.reward.weight_importance == 20.0  # date overrides yaml
    assert prefs.reward.weight_time_bonus == 6.0  # inherited from yaml, untouched by date layer


def test_optimizer_mode_defaults_to_precise_greedy_and_is_overridable():
    default_prefs = resolve_day_preferences(date=DAY, timezone="UTC")
    assert default_prefs.optimizer_mode == OptimizerMode.PRECISE_GREEDY

    adhd_prefs = resolve_day_preferences(
        date=DAY, timezone="UTC", date_overrides=PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY)
    )
    assert adhd_prefs.optimizer_mode == OptimizerMode.ADHD_FRIENDLY


def test_day_window_defaults_to_whole_day_and_is_overridable():
    default_prefs = resolve_day_preferences(date=DAY, timezone="UTC")
    assert default_prefs.day_window.start_minute == 0
    assert default_prefs.day_window.end_day_offset == 1

    custom = DayWindowSpec(start_minute=420, end_minute=1320)
    prefs = resolve_day_preferences(date=DAY, timezone="UTC", date_overrides=PreferenceOverrides(day_window=custom))
    assert prefs.day_window == custom


# -----------------------------------------------------------------------------
# Independence / immutability
# -----------------------------------------------------------------------------


def test_editing_one_resolved_day_does_not_mutate_another():
    monday = resolve_day_preferences(date=date(2024, 6, 3), timezone="UTC")
    tuesday = resolve_day_preferences(
        date=date(2024, 6, 4), timezone="UTC", date_overrides=PreferenceOverrides(category_multipliers={"work": 5.0})
    )

    monday.category_multipliers["work"] = 999.0  # mutate the returned dict directly

    assert tuesday.category_multipliers["work"] == 5.0
    assert monday.category_multipliers["work"] == 999.0  # only affects the object actually mutated


def test_resolving_twice_with_different_overrides_does_not_leak_state():
    first = resolve_day_preferences(
        date=DAY, timezone="UTC", date_overrides=PreferenceOverrides(category_multipliers={"study": 3.0})
    )
    second = resolve_day_preferences(date=DAY, timezone="UTC")  # no overrides this time

    assert first.category_multipliers.get("study") == 3.0
    assert "study" not in second.category_multipliers


def test_reused_override_layer_object_is_not_mutated_by_resolution():
    shared_layer = PreferenceOverrides(category_multipliers={"study": 1.5})

    resolved_a = resolve_day_preferences(date=DAY, timezone="UTC", date_overrides=shared_layer)
    resolved_a.category_multipliers["study"] = 42.0

    resolved_b = resolve_day_preferences(date=DAY, timezone="UTC", date_overrides=shared_layer)

    assert resolved_b.category_multipliers["study"] == 1.5
    assert shared_layer.category_multipliers["study"] == 1.5


def test_module_level_defaults_are_never_mutated():
    baseline = RewardPreferences()
    resolve_day_preferences(
        date=DAY, timezone="UTC", date_overrides=PreferenceOverrides(reward=RewardPreferencesOverride(weight_importance=999.0))
    )
    assert RewardPreferences().weight_importance == baseline.weight_importance == 5.0


# -----------------------------------------------------------------------------
# YAML-derived layer adapter
# -----------------------------------------------------------------------------


def test_day_preferences_overrides_from_reward_settings_round_trips_categories():
    settings = RewardSettings(category_weights={"study": 1.25, "work": 1.1})
    layer = day_preferences_overrides_from_reward_settings(settings)

    prefs = resolve_day_preferences(date=DAY, timezone="UTC", yaml_overrides=layer)

    assert prefs.category_multipliers == {"study": 1.25, "work": 1.1}
    assert prefs.reward.weight_importance == settings.weight_importance


def test_day_preferences_overrides_from_reward_settings_round_trips_short_gap_bonus():
    """Regression: short_gap_bonus_weight/max_minutes/cap were silently
    dropped by day_preferences_overrides_from_reward_settings, so a
    YAML-configured short_gap_bonus block never reached the canonical
    engine's RewardPreferences -- DayPreferences.reward always saw the
    class defaults (weight=0.0, i.e. disabled) regardless of YAML."""
    settings = RewardSettings(short_gap_bonus_weight=4.0, short_gap_bonus_max_minutes=15, short_gap_bonus_cap=8.0)
    layer = day_preferences_overrides_from_reward_settings(settings)

    prefs = resolve_day_preferences(date=DAY, timezone="UTC", yaml_overrides=layer)

    assert prefs.reward.short_gap_bonus_weight == 4.0
    assert prefs.reward.short_gap_bonus_max_minutes == 15
    assert prefs.reward.short_gap_bonus_cap == 8.0

    # And it must survive the return trip back into a legacy RewardSettings,
    # since the canonical day engine calls day_preferences_to_reward_settings
    # on the resolved DayPreferences before scoring.
    round_tripped = day_preferences_to_reward_settings(prefs)
    assert round_tripped.short_gap_bonus_weight == 4.0
    assert round_tripped.short_gap_bonus_max_minutes == 15
    assert round_tripped.short_gap_bonus_cap == 8.0


def test_day_preferences_overrides_from_reward_settings_round_trips_tag_relations():
    """Regression: tag_relations had no field anywhere in
    app.planning.preferences, so a YAML-configured tag_relations block was
    structurally unreachable from the canonical engine -- the reconstructed
    RewardSettings always had tag_relations={} regardless of YAML."""
    settings = RewardSettings(tag_relations={"math": ["exam", "homework"]})
    layer = day_preferences_overrides_from_reward_settings(settings)

    prefs = resolve_day_preferences(date=DAY, timezone="UTC", yaml_overrides=layer)

    assert prefs.reward.tag_relations == {"math": ["exam", "homework"]}

    round_tripped = day_preferences_to_reward_settings(prefs)
    assert round_tripped.tag_relations == {"math": ["exam", "homework"]}


def test_short_gap_bonus_and_tag_relations_are_overridable_per_day():
    yaml_layer = PreferenceOverrides(
        reward=RewardPreferencesOverride(short_gap_bonus_weight=1.0, tag_relations={"a": ["b"]})
    )
    date_layer = PreferenceOverrides(reward=RewardPreferencesOverride(short_gap_bonus_weight=2.0))

    prefs = resolve_day_preferences(date=DAY, timezone="UTC", yaml_overrides=yaml_layer, date_overrides=date_layer)

    assert prefs.reward.short_gap_bonus_weight == 2.0  # date overrides yaml
    assert prefs.reward.tag_relations == {"a": ["b"]}  # inherited from yaml, untouched by date layer


# -----------------------------------------------------------------------------
# Preferred-window precedence
# -----------------------------------------------------------------------------


def test_precedence_task_override_wins_over_everything():
    task = _task(category="study", preferred_time_window=None)
    prefs = resolve_day_preferences(
        date=DAY, timezone="UTC",
        date_overrides=PreferenceOverrides(category_preferred_windows={"study": LocalTimeWindow(start_minute=0, end_minute=60)}),
    )
    override = LocalTimeWindow(start_minute=700, end_minute=760)

    window = effective_task_preferred_window(task, prefs, task_override=override)

    assert window == {"start_time": 700, "end_time": 760}


def test_precedence_task_own_window_wins_over_category():
    from app.planning.models import LocalTimeWindow as LTW

    task = _task(category="study", preferred_time_window=LTW(start_minute=600, end_minute=660))
    prefs = resolve_day_preferences(
        date=DAY, timezone="UTC",
        date_overrides=PreferenceOverrides(category_preferred_windows={"study": LocalTimeWindow(start_minute=0, end_minute=60)}),
    )

    window = effective_task_preferred_window(task, prefs)

    assert window == {"start_time": 600, "end_time": 660}


def test_precedence_falls_back_to_category_when_task_has_no_window():
    task = _task(category="study", preferred_time_window=None)
    category_window = LocalTimeWindow(start_minute=480, end_minute=600)
    prefs = resolve_day_preferences(
        date=DAY, timezone="UTC",
        date_overrides=PreferenceOverrides(category_preferred_windows={"study": category_window}),
    )

    window = effective_task_preferred_window(task, prefs)

    assert window == {"start_time": 480, "end_time": 600}


def test_precedence_none_when_nothing_specifies_a_window():
    task = _task(category="study", preferred_time_window=None)
    prefs = resolve_day_preferences(date=DAY, timezone="UTC")

    assert effective_task_preferred_window(task, prefs) is None


def test_to_legacy_scoring_task_reaches_calculate_task_score():
    task = _task(name="Study Math", category="study", tags=["math"], priority=9, preferred_time_window=None)
    prefs = resolve_day_preferences(
        date=DAY, timezone="UTC",
        date_overrides=PreferenceOverrides(
            category_multipliers={"study": 2.0},
            category_preferred_windows={"study": LocalTimeWindow(start_minute=480, end_minute=600)},
            reward=RewardPreferencesOverride(weight_time_bonus=10.0, weight_tag_relation=0.0, weight_fragmentation_penalty=0.0),
        ),
    )

    scoring_task = to_legacy_scoring_task(task, prefs)
    settings = day_preferences_to_reward_settings(prefs)

    assert scoring_task["preference_time"] == {"start_time": 480, "end_time": 600}
    score = calculate_task_score(scoring_task, start_time=480, settings=settings)
    # priority(9) * weight_importance(5) * category_multiplier(2.0) + time_bonus(10, fully inside window)
    assert score == pytest.approx(9 * 5 * 2.0 + 10.0)


# -----------------------------------------------------------------------------
# Validation at the configuration boundary
# -----------------------------------------------------------------------------


def test_rejects_nonfinite_reward_weight():
    with pytest.raises(ValidationError):
        resolve_day_preferences(
            date=DAY, timezone="UTC",
            date_overrides=PreferenceOverrides(reward=RewardPreferencesOverride(weight_importance=float("inf"))),
        )


def test_rejects_nonpositive_max_time_distance():
    with pytest.raises(ValidationError):
        resolve_day_preferences(
            date=DAY, timezone="UTC",
            date_overrides=PreferenceOverrides(reward=RewardPreferencesOverride(max_time_distance_minutes=0)),
        )


def test_rejects_negative_min_gap():
    with pytest.raises(ValidationError):
        resolve_day_preferences(
            date=DAY, timezone="UTC",
            date_overrides=PreferenceOverrides(reward=RewardPreferencesOverride(min_gap_between_tasks_minutes=-5)),
        )


def test_rejects_negative_short_gap_bonus_weight():
    with pytest.raises(ValidationError):
        RewardPreferences(short_gap_bonus_weight=-1.0)


def test_rejects_nonpositive_short_gap_bonus_max_minutes():
    with pytest.raises(ValidationError):
        RewardPreferences(short_gap_bonus_max_minutes=0)


def test_short_gap_bonus_disabled_with_zero_is_valid():
    prefs = RewardPreferences(short_gap_bonus_weight=0.0)
    assert prefs.short_gap_bonus_weight == 0.0


def test_rejects_invalid_optimizer_mode_string():
    with pytest.raises(ValueError):
        OptimizerMode("simulated_annealing")


def test_rejects_inconsistent_timezone():
    with pytest.raises(ValueError):
        resolve_day_preferences(date=DAY, timezone="Definitely/NotAZone")


def test_rejects_malformed_day_window_end_before_start():
    with pytest.raises(ValidationError):
        DayWindowSpec(start_minute=600, end_minute=300)


def test_rejects_unsupported_multi_day_offset():
    with pytest.raises(ValidationError):
        DayWindowSpec(start_minute=0, end_minute=0, end_day_offset=2)


def test_day_window_1440_same_day_normalizes_like_task_1s_local_day_window():
    spec = DayWindowSpec(start_minute=1380, end_minute=1440)
    assert spec.end_minute == 0
    assert spec.end_day_offset == 1


def test_to_local_day_window_applies_task_1_overnight_limitation():
    """A day_window that would need to extend further into a following day
    is rejected explicitly when actually resolved against a date/timezone,
    exactly like Task 1's LocalDayWindow -- no silent wraparound."""
    prefs = resolve_day_preferences(date=DAY, timezone="America/New_York")
    # A day_window ending after the following midnight cannot even be
    # constructed (see test_rejects_unsupported_multi_day_offset); confirm
    # the resolved DayPreferences' *supported* whole-day window still
    # resolves correctly end-to-end through Task 1's time contract.
    window = prefs.to_local_day_window()
    start, end = window.to_utc_instants()
    assert (end - start).total_seconds() / 60 == pytest.approx(24 * 60)


def test_to_local_day_window_rejects_dst_crossing_window():
    dst_transition_day = date(2024, 3, 10)  # America/New_York spring-forward
    prefs = resolve_day_preferences(
        date=dst_transition_day, timezone="America/New_York",
        date_overrides=PreferenceOverrides(day_window=DayWindowSpec(start_minute=60, end_minute=240)),
    )
    with pytest.raises(UnsupportedSchedulingWindowError):
        prefs.to_local_day_window().to_utc_instants()


# -----------------------------------------------------------------------------
# No per-day YAML files or hidden global mutation
# -----------------------------------------------------------------------------


def test_resolve_day_preferences_does_not_touch_the_filesystem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config/ directory exists here
    # If this function tried to read a per-day YAML file, a missing
    # directory/file would raise; it must not, since it takes overrides as
    # plain in-memory arguments only.
    prefs = resolve_day_preferences(date=DAY, timezone="UTC")
    assert prefs.date == DAY
