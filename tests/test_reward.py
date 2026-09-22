"""Tests for app/reward.py: candidate-placement scoring and YAML configuration.

Reward-component tests use plain dicts for tasks/neighbors (reward.py reads
fields through a dict-or-attribute `_get` helper), which makes it easy to
exercise edge cases like "no preferred window" that the real pydantic Task
model can't represent (it always has a preference_time).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.reward import (
    RewardSettings,
    calculate_schedule_score,
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


def test_load_reward_settings_defaults_when_no_file_found(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_reward_settings() == RewardSettings()


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
    assert settings.task_weights == {"Study Math": 2.0}
    assert settings.task_time_windows == {"Study Math": {"start_time": 540, "end_time": 600}}
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


def test_default_discovery_does_not_match_checked_in_plural_filename(tmp_path, monkeypatch) -> None:
    """Documents the discovery mismatch: the repository's real reward config is
    config/task_preferences.yaml (plural), but _resolve_config_path's default
    (no config_path) search only looks for singular/misspelled variants
    (task_prefrence.yaml, task_prefrence.yml, task_preference.yaml,
    task_preference.yml). Placing a file under the plural name -- exactly as
    it exists in this repo -- must NOT be picked up by default discovery.
    """
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "task_preferences.yaml").write_text(
        "weights:\n  importance: 999\n", encoding="utf-8"
    )

    settings = load_reward_settings()

    assert settings == RewardSettings()


def test_default_discovery_walks_ancestor_directories(tmp_path, monkeypatch) -> None:
    """Documents the ancestor-walking behavior of the default search: a
    matching filename several directories above the current working directory
    is still found. This is why tests that must guarantee isolation from a
    real config file (e.g. test_optimizer.py) cannot rely on chdir alone.
    """
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / "task_preference.yaml").write_text(
        "weights:\n  importance: 777\n", encoding="utf-8"
    )
    monkeypatch.chdir(nested)

    settings = load_reward_settings()

    assert settings.weight_importance == 777.0


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
# calculate_task_score: priority component
# -----------------------------


def test_calculate_task_score_zero_for_fixed_task() -> None:
    task = {"fixed": True, "duration": 60, "priority": 10, "category": "study", "name": "Sleep"}
    score = calculate_task_score(task, start_time=0, settings=RewardSettings(**ZERO_SETTINGS))
    assert score == 0.0


def test_priority_score_scales_with_priority_and_weight() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_importance": 5.0})
    task = {"fixed": False, "duration": 60, "priority": 4, "category": "other", "name": "X"}

    score = calculate_task_score(task, start_time=0, settings=settings)

    assert score == 20.0  # priority(4) * weight_importance(5) * 1.0 * 1.0


def test_weight_category_bonus_is_not_yet_wired_into_scoring() -> None:
    """Characterizes a known gap (see Milestone 0 notes / README): RewardSettings
    loads weight_category_bonus from YAML's weights.category_bonus, but
    _priority_score never reads it -- only the separate category_weights
    per-category multiplier dict actually affects the score. Changing
    weight_category_bonus alone must not change calculate_task_score's
    output. This is not the desired end state; it documents current behavior
    so a future fix has a test to update deliberately rather than a silent
    change in scoring.
    """
    low = RewardSettings(**{**ZERO_SETTINGS, "weight_importance": 5.0, "weight_category_bonus": 1.0})
    high = RewardSettings(**{**ZERO_SETTINGS, "weight_importance": 5.0, "weight_category_bonus": 500.0})
    task = {"fixed": False, "duration": 60, "priority": 4, "category": "study", "name": "X"}

    score_low = calculate_task_score(task, start_time=0, settings=low)
    score_high = calculate_task_score(task, start_time=0, settings=high)

    assert score_low == score_high == 20.0  # priority(4) * weight_importance(5) * 1.0 * 1.0


def test_priority_score_applies_category_and_task_weights() -> None:
    settings = RewardSettings(
        **{
            **ZERO_SETTINGS,
            "weight_importance": 5.0,
            "category_weights": {"study": 2.0},
            "task_weights": {"Study Math": 1.5},
        }
    )
    task = {"fixed": False, "duration": 60, "priority": 4, "category": "study", "name": "Study Math"}

    score = calculate_task_score(task, start_time=0, settings=settings)

    assert score == 60.0  # 4 * 5 * 2.0 * 1.5


# -----------------------------
# calculate_task_score: preferred-time component
# -----------------------------


def test_time_preference_full_bonus_inside_preferred_window() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_time_bonus": 10.0})
    task = {
        "fixed": False,
        "duration": 60,
        "priority": 1,
        "category": "other",
        "name": "X",
        "preference_time": {"start_time": 480, "end_time": 600},
    }

    score = calculate_task_score(task, start_time=500, settings=settings)

    assert score == 10.0


def test_time_preference_zero_when_distance_exceeds_max() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_time_bonus": 10.0, "max_time_distance_minutes": 100}
    )
    task = {
        "fixed": False,
        "duration": 60,
        "priority": 1,
        "category": "other",
        "name": "X",
        "preference_time": {"start_time": 0, "end_time": 60},
    }

    score = calculate_task_score(task, start_time=200, settings=settings)

    assert score == 0.0


def test_time_preference_partial_decay_with_distance() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_time_bonus": 10.0, "max_time_distance_minutes": 200}
    )
    task = {
        "fixed": False,
        "duration": 60,
        "priority": 1,
        "category": "other",
        "name": "X",
        "preference_time": {"start_time": 0, "end_time": 60},
    }

    # preferred center = 30, scheduled center = 160, distance = 130
    score = calculate_task_score(task, start_time=130, settings=settings)

    expected = round(10.0 * (1.0 - 130 / 200), 2)
    assert score == expected


def test_time_preference_zero_when_no_preferred_window_available() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_time_bonus": 10.0})
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "other", "name": "X"}

    score = calculate_task_score(task, start_time=100, settings=settings)

    assert score == 0.0


def test_task_time_window_overrides_category_time_window() -> None:
    settings = RewardSettings(
        **{
            **ZERO_SETTINGS,
            "weight_time_bonus": 10.0,
            "task_time_windows": {"Study Math": {"start_time": 0, "end_time": 60}},
            "category_time_windows": {"study": {"start_time": 1000, "end_time": 1060}},
        }
    )
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "study", "name": "Study Math"}

    score = calculate_task_score(task, start_time=0, settings=settings)

    assert score == 10.0


# -----------------------------
# calculate_task_score: neighboring tag/category bonus
# -----------------------------


def test_relation_score_same_tag_bonus() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_tag_relation": 5.0})
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "study", "tag": "math", "name": "B"}
    neighbor = {"category": "study", "tag": "math", "time_window": {"start_time": 0, "end_time": 60}}

    score = calculate_task_score(task, start_time=60, previous_task=neighbor, settings=settings)

    assert score == 5.0


def test_relation_score_related_tag_via_tag_relations() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_tag_relation": 5.0, "tag_relations": {"math": ["exam"]}}
    )
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "study", "tag": "exam", "name": "Exam"}
    neighbor = {"category": "study", "tag": "math", "time_window": {"start_time": 0, "end_time": 60}}

    score = calculate_task_score(task, start_time=60, previous_task=neighbor, settings=settings)

    assert score == 5.0


def test_relation_score_half_bonus_for_same_category_unrelated_tags() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_tag_relation": 5.0})
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "study", "tag": "reading", "name": "B"}
    neighbor = {"category": "study", "tag": "writing", "time_window": {"start_time": 0, "end_time": 60}}

    score = calculate_task_score(task, start_time=60, previous_task=neighbor, settings=settings)

    assert score == 2.5


def test_relation_score_zero_when_gap_exceeds_window() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_tag_relation": 5.0, "same_tag_window_minutes": 30}
    )
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "study", "tag": "math", "name": "B"}
    neighbor = {"category": "study", "tag": "math", "time_window": {"start_time": 0, "end_time": 60}}

    score = calculate_task_score(task, start_time=200, previous_task=neighbor, settings=settings)

    assert score == 0.0


def test_relation_score_ignores_missing_neighbors() -> None:
    settings = RewardSettings(**{**ZERO_SETTINGS, "weight_tag_relation": 5.0})
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "study", "tag": "math", "name": "B"}

    score = calculate_task_score(task, start_time=60, settings=settings)

    assert score == 0.0


# -----------------------------
# calculate_task_score: fragmentation penalty
# -----------------------------


def test_fragmentation_penalty_for_small_gap() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_fragmentation_penalty": -4.0, "min_gap_between_tasks_minutes": 30}
    )
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "other", "tag": "", "name": "B"}
    neighbor = {"category": "other", "tag": "", "time_window": {"start_time": 0, "end_time": 60}}

    score = calculate_task_score(task, start_time=70, previous_task=neighbor, settings=settings)

    assert score == -4.0


def test_fragmentation_no_penalty_when_gap_meets_minimum() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_fragmentation_penalty": -4.0, "min_gap_between_tasks_minutes": 30}
    )
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "other", "tag": "", "name": "B"}
    neighbor = {"category": "other", "tag": "", "time_window": {"start_time": 0, "end_time": 60}}

    score = calculate_task_score(task, start_time=90, previous_task=neighbor, settings=settings)

    assert score == 0.0


def test_fragmentation_no_penalty_when_back_to_back() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_fragmentation_penalty": -4.0, "min_gap_between_tasks_minutes": 30}
    )
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "other", "tag": "", "name": "B"}
    neighbor = {"category": "other", "tag": "", "time_window": {"start_time": 0, "end_time": 60}}

    score = calculate_task_score(task, start_time=60, previous_task=neighbor, settings=settings)

    assert score == 0.0


def test_fragmentation_penalty_applies_per_neighbor() -> None:
    settings = RewardSettings(
        **{**ZERO_SETTINGS, "weight_fragmentation_penalty": -4.0, "min_gap_between_tasks_minutes": 30}
    )
    task = {"fixed": False, "duration": 60, "priority": 1, "category": "other", "tag": "", "name": "B"}
    previous = {"category": "other", "tag": "", "time_window": {"start_time": 0, "end_time": 60}}
    following = {"category": "other", "tag": "", "time_window": {"start_time": 140, "end_time": 200}}

    # task occupies 70-130: gap of 10 before, gap of 10 after -> two penalties
    score = calculate_task_score(
        task, start_time=70, previous_task=previous, next_task=following, settings=settings
    )

    assert score == -8.0


# -----------------------------
# calculate_schedule_score
# -----------------------------


def test_calculate_schedule_score_matches_manual_sum() -> None:
    """calculate_schedule_score's "already scored" branch checks hasattr(), which
    only reflects true attribute access -- so recomputable items must be
    attribute-bearing objects here, not plain dicts.
    """
    settings = RewardSettings()
    item_a = SimpleNamespace(
        name="A",
        category="study",
        tag="",
        priority=5,
        duration=60,
        fixed=False,
        time_window=SimpleNamespace(start_time=0, end_time=60),
    )
    item_b = SimpleNamespace(
        name="B",
        category="study",
        tag="",
        priority=5,
        duration=60,
        fixed=False,
        time_window=SimpleNamespace(start_time=60, end_time=120),
    )
    scheduled = [item_a, item_b]

    total = calculate_schedule_score(scheduled, settings=settings)

    expected = round(
        calculate_task_score(item_a, 0, previous_task=None, next_task=item_b, settings=settings)
        + calculate_task_score(item_b, 60, previous_task=item_a, next_task=None, settings=settings),
        2,
    )
    assert total == expected


def test_calculate_schedule_score_keeps_existing_score_for_scheduled_task_objects(
    make_scheduled_task,
) -> None:
    item = make_scheduled_task("Sleep", start=0, end=480, score=0.0)

    total = calculate_schedule_score([item], settings=RewardSettings())

    assert total == 0.0


def test_calculate_schedule_score_empty_schedule_is_zero() -> None:
    assert calculate_schedule_score([], settings=RewardSettings()) == 0.0


def test_calculate_schedule_score_sums_nonzero_stored_scores(make_scheduled_task) -> None:
    """Distinct from test_calculate_schedule_score_matches_manual_sum: that test
    exercises the recompute-from-rich-task-objects branch, while
    test_calculate_schedule_score_keeps_existing_score_for_scheduled_task_objects
    only ever used a stored score of 0.0, which would not catch a bug where
    the "sum stored scores" branch silently returned 0 regardless of input.
    """
    item_a = make_scheduled_task("A", start=0, end=60, score=12.5)
    item_b = make_scheduled_task("B", start=60, end=120, score=7.25)

    total = calculate_schedule_score([item_a, item_b], settings=RewardSettings())

    assert total == 19.75
