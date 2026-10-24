"""Tests for app/reward.py: candidate-placement scoring and YAML configuration.

Reward-component tests score a ScoringTask between SimpleNamespace neighbours
(anything with start/end/category/tag satisfies the Neighbour protocol).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.reward import (
    RewardSettings,
    ScoringTask,
    calculate_task_score,
    load_reward_settings,
)

ZERO_SETTINGS = dict(
    weight_importance=0.0,
    weight_time_bonus=0.0,
    weight_tag_relation=0.0,
    weight_fragmentation_penalty=0.0,
)


# -----------------------------
# load_reward_settings
# -----------------------------


def test_load_reward_settings_defaults_when_no_file_found(tmp_path) -> None:
    # Discovery is anchored to project_root's own config/ directory, not
    # cwd, so isolation from this repo's real config/task_preference.yaml
    # requires an explicit (guaranteed-empty) project_root -- chdir alone
    # would not do it.
    assert load_reward_settings(project_root=tmp_path) == RewardSettings()


def test_load_reward_settings_missing_explicit_path_raises(tmp_path) -> None:
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError):
        load_reward_settings(missing)


def test_load_reward_settings_parses_full_yaml(tmp_path) -> None:
    config = tmp_path / "task_preference.yaml"
    config.write_text(
        """
weights:
  importance: 10
  time_bonus: 6
  tag_relation: 4
  fragmentation_penalty: -8
  category_bonus: 2

preference:
  max_time_distance_minutes: 100
  same_tag_window_minutes: 50
  min_gap_between_tasks_minutes: 15

category_weights:
  study: 1.5

task_weights:
  Study Math: 2.0

task_time_windows:
  Study Math:
    start_time: 540
    end_time: 600

category_time_windows:
  study:
    start_time: 480
    end_time: 720

tag_relations:
  math:
    - exam
""",
        encoding="utf-8",
    )

    settings = load_reward_settings(config)

    assert settings.weight_importance == 10.0
    assert settings.weight_time_bonus == 6.0
    assert settings.weight_tag_relation == 4.0
    assert settings.weight_fragmentation_penalty == -8.0
    assert settings.max_time_distance_minutes == 100
    assert settings.same_tag_window_minutes == 50
    assert settings.min_gap_between_tasks_minutes == 15
    assert settings.category_weights == {"study": 1.5}
    assert not hasattr(settings, "task_weights")  # the removed per-task-name keys are ignored
    assert settings.category_time_windows == {"study": {"start_time": 480, "end_time": 720}}
    assert settings.tag_relations == {"math": ["exam"]}


def test_load_reward_settings_uses_defaults_for_missing_keys(tmp_path) -> None:
    config = tmp_path / "task_preference.yaml"
    config.write_text("weights:\n  importance: 9\n", encoding="utf-8")

    settings = load_reward_settings(config)

    assert settings.weight_importance == 9.0
    assert settings.weight_time_bonus == RewardSettings().weight_time_bonus


def test_load_reward_settings_rejects_non_mapping_yaml(tmp_path) -> None:
    config = tmp_path / "task_preference.yaml"
    config.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_reward_settings(config)


def test_default_discovery_finds_the_canonical_singular_filename(tmp_path) -> None:
    """config/task_preference.yaml (singular) is the canonical, auto-discovered
    template name (Task 3 fix -- see app/reward.py's module docstring)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "task_preference.yaml").write_text(
        "weights:\n  importance: 999\n", encoding="utf-8"
    )

    settings = load_reward_settings(project_root=tmp_path)

    assert settings.weight_importance == 999.0


@pytest.mark.parametrize("legacy_name", ["task_preferences.yaml", "task_prefrence.yaml"])
def test_default_discovery_ignores_the_removed_legacy_filenames(tmp_path, legacy_name) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / legacy_name).write_text("weights:\n  importance: 888\n", encoding="utf-8")

    assert load_reward_settings(project_root=tmp_path) == RewardSettings()


def test_default_discovery_does_not_walk_ancestor_directories(tmp_path) -> None:
    """Discovery no longer walks ancestor/home directories (Task 3 fix): a
    matching filename above project_root, or at project_root itself instead
    of project_root/config, must NOT be picked up."""
    nested_root = tmp_path / "a" / "b" / "c"
    nested_root.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "task_preference.yaml").write_text(
        "weights:\n  importance: 777\n", encoding="utf-8"
    )

    settings = load_reward_settings(project_root=nested_root)

    assert settings == RewardSettings()


def test_default_discovery_does_not_search_project_root_itself_only_its_config_subdirectory(tmp_path) -> None:
    (tmp_path / "task_preference.yaml").write_text(
        "weights:\n  importance: 555\n", encoding="utf-8"
    )

    settings = load_reward_settings(project_root=tmp_path)

    assert settings == RewardSettings()


def test_explicit_config_path_wins_over_canonical_project_template(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "task_preference.yaml").write_text(
        "weights:\n  importance: 111\n", encoding="utf-8"
    )
    explicit = tmp_path / "explicit_override.yaml"
    explicit.write_text("weights:\n  importance: 42\n", encoding="utf-8")

    settings = load_reward_settings(explicit, project_root=tmp_path)

    assert settings.weight_importance == 42.0


def test_default_discovery_is_independent_of_current_working_directory(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "task_preference.yaml").write_text(
        "weights:\n  importance: 444\n", encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    settings = load_reward_settings(project_root=tmp_path)

    assert settings.weight_importance == 444.0


# -----------------------------
# RewardSettings validation
# -----------------------------


def test_reward_settings_rejects_zero_max_time_distance() -> None:
    """Regression: max_time_distance_minutes is used as a divisor in
    _time_preference_score whenever a placement is not fully inside its
    preferred window. A value of 0 previously reached that division and
    raised ZeroDivisionError deep inside scoring; it must now be rejected
    clearly at configuration time instead.
    """
    with pytest.raises(ValueError):
        RewardSettings(max_time_distance_minutes=0)


def test_reward_settings_rejects_negative_max_time_distance() -> None:
    with pytest.raises(ValueError):
        RewardSettings(max_time_distance_minutes=-10)


def test_reward_settings_accepts_positive_max_time_distance() -> None:
    settings = RewardSettings(max_time_distance_minutes=1)
    assert settings.max_time_distance_minutes == 1


def test_load_reward_settings_rejects_zero_max_time_distance_from_yaml(tmp_path) -> None:
    config = tmp_path / "task_preference.yaml"
    config.write_text(
        "preference:\n  max_time_distance_minutes: 0\n", encoding="utf-8"
    )

    with pytest.raises(ValueError):
        load_reward_settings(config)


# -----------------------------
# calculate_task_score: weight component
# -----------------------------


def neighbour(start: int, end: int, *, category: str = "other", tag: str = "") -> SimpleNamespace:
    return SimpleNamespace(start=start, end=end, category=category, tag=tag)


def test_weight_score_is_weight_importance_without_a_category_multiplier() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_importance": 5.0})

    score = calculate_task_score(ScoringTask(category="other", duration=60), start_time=0, settings=settings)

    assert score == 5.0


def test_weight_category_bonus_is_not_wired_into_scoring() -> None:
    """weight_category_bonus is carried with the preferences for compatibility,
    but only the per-category category_weights multiplier affects the score."""
    low = RewardSettings(**{**ZERO_SETTINGS, "weight_importance": 5.0, "weight_category_bonus": 1.0})
    high = RewardSettings(**{**ZERO_SETTINGS, "weight_importance": 5.0, "weight_category_bonus": 500.0})
    task = ScoringTask(category="study", duration=60)

    score_low = calculate_task_score(task, start_time=0, settings=low)
    score_high = calculate_task_score(task, start_time=0, settings=high)

    assert score_low == score_high == 5.0


def test_weight_score_applies_the_category_multiplier_case_insensitively() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_importance": 5.0, "category_weights": {"study": 2.0}})

    score = calculate_task_score(ScoringTask(category="Study", duration=60), start_time=0, settings=settings)

    assert score == 10.0  # 5 * 2.0


# -----------------------------
# calculate_task_score: preferred-time component
# -----------------------------


def test_time_preference_full_bonus_inside_preferred_window() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_time_bonus": 10.0})
    task = ScoringTask(category="other", duration=60, preferred_window=(480, 600))

    score = calculate_task_score(task, start_time=500, settings=settings)

    assert score == 10.0


def test_time_preference_zero_when_distance_exceeds_max() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_time_bonus": 10.0, "max_time_distance_minutes": 100}
    )
    task = ScoringTask(category="other", duration=60, preferred_window=(0, 60))

    score = calculate_task_score(task, start_time=200, settings=settings)

    assert score == 0.0


def test_time_preference_partial_decay_with_distance() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_time_bonus": 10.0, "max_time_distance_minutes": 200}
    )
    task = ScoringTask(category="other", duration=60, preferred_window=(0, 60))

    # preferred center = 30, scheduled center = 160, distance = 130
    score = calculate_task_score(task, start_time=130, settings=settings)

    expected = round(10.0 * (1.0 - 130 / 200), 2)
    assert score == expected


def test_time_preference_zero_when_no_preferred_window_available() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_time_bonus": 10.0})

    score = calculate_task_score(ScoringTask(category="other", duration=60), start_time=100, settings=settings)

    assert score == 0.0


def test_settings_category_time_windows_never_reach_scoring() -> None:
    """The window a task scores against is the one on its ScoringTask; the
    template's category_time_windows only seed the preference layers."""
    settings = RewardSettings(
        **{
            **ZERO_SETTINGS,
            "weight_time_bonus": 10.0,
            "category_time_windows": {"study": {"start_time": 0, "end_time": 60}},
        }
    )

    score = calculate_task_score(ScoringTask(category="study", duration=60), start_time=0, settings=settings)

    assert score == 0.0


# -----------------------------
# calculate_task_score: neighboring tag/category bonus
# -----------------------------


def test_relation_score_same_tag_bonus() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_tag_relation": 5.0})
    task = ScoringTask(category="study", duration=60, tag="math")

    score = calculate_task_score(task, start_time=60, previous_task=neighbour(0, 60, category="study", tag="math"),
                                 settings=settings)

    assert score == 5.0


def test_relation_score_related_tag_via_tag_relations() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_tag_relation": 5.0, "tag_relations": {"math": ["exam"]}}
    )
    task = ScoringTask(category="study", duration=60, tag="exam")

    score = calculate_task_score(task, start_time=60, previous_task=neighbour(0, 60, category="study", tag="math"),
                                 settings=settings)

    assert score == 5.0


def test_relation_score_half_bonus_for_same_category_unrelated_tags() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_tag_relation": 5.0})
    task = ScoringTask(category="study", duration=60, tag="reading")

    score = calculate_task_score(task, start_time=60,
                                 previous_task=neighbour(0, 60, category="study", tag="writing"), settings=settings)

    assert score == 2.5


def test_relation_score_zero_when_gap_exceeds_window() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_tag_relation": 5.0, "same_tag_window_minutes": 30}
    )
    task = ScoringTask(category="study", duration=60, tag="math")

    score = calculate_task_score(task, start_time=200, previous_task=neighbour(0, 60, category="study", tag="math"),
                                 settings=settings)

    assert score == 0.0


def test_relation_score_ignores_missing_neighbors() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_tag_relation": 5.0})

    score = calculate_task_score(ScoringTask(category="study", duration=60, tag="math"), start_time=60,
                                 settings=settings)

    assert score == 0.0


# -----------------------------
# calculate_task_score: fragmentation penalty
# -----------------------------


FRAGMENTATION_SETTINGS = {**ZERO_SETTINGS, "weight_fragmentation_penalty": -4.0, "min_gap_between_tasks_minutes": 30}
PLAIN_TASK = ScoringTask(category="other", duration=60)


def test_fragmentation_penalty_for_small_gap() -> None:
    score = calculate_task_score(PLAIN_TASK, start_time=70, previous_task=neighbour(0, 60),
                                 settings=RewardSettings(**FRAGMENTATION_SETTINGS))

    assert score == -4.0


def test_fragmentation_no_penalty_when_gap_meets_minimum() -> None:
    score = calculate_task_score(PLAIN_TASK, start_time=90, previous_task=neighbour(0, 60),
                                 settings=RewardSettings(**FRAGMENTATION_SETTINGS))

    assert score == 0.0


def test_fragmentation_no_penalty_when_back_to_back() -> None:
    score = calculate_task_score(PLAIN_TASK, start_time=60, previous_task=neighbour(0, 60),
                                 settings=RewardSettings(**FRAGMENTATION_SETTINGS))

    assert score == 0.0


def test_fragmentation_penalty_applies_per_neighbor() -> None:
    # task occupies 70-130: gap of 10 before, gap of 10 after -> two penalties
    score = calculate_task_score(PLAIN_TASK, start_time=70, previous_task=neighbour(0, 60),
                                 next_task=neighbour(140, 200), settings=RewardSettings(**FRAGMENTATION_SETTINGS))

    assert score == -8.0


# -----------------------------
# calculate_task_score: all components together
# -----------------------------


def test_components_sum_and_round_to_two_decimals() -> None:
    settings = RewardSettings(
        weight_importance=5.0, weight_time_bonus=3.0, weight_tag_relation=2.0, weight_fragmentation_penalty=-4.0,
        max_time_distance_minutes=240, same_tag_window_minutes=120, min_gap_between_tasks_minutes=30,
        category_weights={"study": 1.5},
    )
    task = ScoringTask(category="study", duration=60, tag="math", preferred_window=(0, 60))

    # weight 7.5; time 3 * (1 - 110/240); relation 2 (same tag, gap 10); fragmentation -4 (gap 10 < 30)
    score = calculate_task_score(task, start_time=110, previous_task=neighbour(0, 100, category="study", tag="math"),
                                 settings=settings)

    assert score == round(7.5 + 3.0 * (1 - 110 / 240) + 2.0 - 4.0, 2)
