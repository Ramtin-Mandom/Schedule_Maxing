"""
reward.py

Reward/scoring logic for the schedule optimizer.

This file now reads task preference values from `task_prefrence.yaml`
or `task_preference.yaml`.

Supported file locations:
    - task_prefrence.yaml
    - task_prefrence.yml
    - task_preference.yaml
    - task_preference.yml
    - config/task_prefrence.yaml
    - config/task_preference.yaml

Why this design:
    - Hard constraints still belong in optimizer/constraints.
    - This file only decides how good a valid placement is.
    - If the YAML file is missing, the optimizer still works using safe defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


# -----------------------------
# Reward configuration model
# -----------------------------


@dataclass(frozen=True)
class RewardSettings:
    """
    Runtime reward settings loaded from YAML.

    The default values are intentionally conservative so the app still works
    even if the YAML file is missing or incomplete.
    """

    weight_importance: float = 5.0
    weight_time_bonus: float = 3.0
    weight_tag_relation: float = 2.0
    weight_fragmentation_penalty: float = -4.0
    weight_category_bonus: float = 1.0

    max_time_distance_minutes: int = 240
    same_tag_window_minutes: int = 120
    min_gap_between_tasks_minutes: int = 30

    category_weights: dict[str, float] = field(default_factory=dict)
    task_weights: dict[str, float] = field(default_factory=dict)

    # Example:
    # task_time_windows:
    #   Study Math:
    #     start_time: 540
    #     end_time: 660
    task_time_windows: dict[str, dict[str, int]] = field(default_factory=dict)

    # Example:
    # category_time_windows:
    #   study:
    #     start_time: 540
    #     end_time: 720
    category_time_windows: dict[str, dict[str, int]] = field(default_factory=dict)

    # Example:
    # tag_relations:
    #   math:
    #     - exam
    #     - homework
    tag_relations: dict[str, list[str]] = field(default_factory=dict)


# -----------------------------
# YAML loading
# -----------------------------


def load_reward_settings(config_path: str | Path | None = None) -> RewardSettings:
    """
    Load reward settings from YAML.

    If `config_path` is provided, that path is used directly.
    Otherwise, common project locations are searched.

    Missing YAML values are okay. Defaults are used for anything not specified.
    """

    path = _resolve_config_path(config_path)

    if path is None:
        return RewardSettings()

    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required to read task_prefrence.yaml. "
            "Install it with: pip install PyYAML"
        ) from exc

    with path.open("r", encoding="utf-8") as file:
        raw_data = yaml.safe_load(file) or {}

    if not isinstance(raw_data, dict):
        raise ValueError(f"{path.name} must contain a YAML dictionary at the top level.")

    return _settings_from_dict(raw_data)


def _resolve_config_path(config_path: str | Path | None) -> Path | None:
    if config_path is not None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Reward config file not found: {path}")
        return path

    candidate_names = [
        "task_prefrence.yaml",      # keeping your current spelling
        "task_prefrence.yml",
        "task_preference.yaml",     # also support the correct spelling
        "task_preference.yml",
    ]

    search_roots = [Path.cwd(), Path.cwd() / "config"]

    current = Path.cwd()
    for parent in [current, *current.parents]:
        search_roots.append(parent)
        search_roots.append(parent / "config")

    for root in search_roots:
        for name in candidate_names:
            candidate = root / name
            if candidate.exists():
                return candidate

    return None


def _settings_from_dict(data: Mapping[str, Any]) -> RewardSettings:
    weights = _as_dict(data.get("weights"))
    preference = _as_dict(data.get("preference"))

    return RewardSettings(
        weight_importance=float(weights.get("importance", 5.0)),
        weight_time_bonus=float(weights.get("time_bonus", 3.0)),
        weight_tag_relation=float(weights.get("tag_relation", 2.0)),
        weight_fragmentation_penalty=float(weights.get("fragmentation_penalty", -4.0)),
        weight_category_bonus=float(weights.get("category_bonus", 1.0)),

        max_time_distance_minutes=int(preference.get("max_time_distance_minutes", 240)),
        same_tag_window_minutes=int(preference.get("same_tag_window_minutes", 120)),
        min_gap_between_tasks_minutes=int(preference.get("min_gap_between_tasks_minutes", 30)),

        category_weights=_float_dict(data.get("category_weights")),
        task_weights=_float_dict(data.get("task_weights")),
        task_time_windows=_window_dict(data.get("task_time_windows")),
        category_time_windows=_window_dict(data.get("category_time_windows")),
        tag_relations=_list_dict(data.get("tag_relations")),
    )


# -----------------------------
# Main reward functions
# -----------------------------


def calculate_task_score(
    task: Any,
    start_time: int,
    previous_task: Any | None = None,
    next_task: Any | None = None,
    settings: RewardSettings | None = None,
    config_path: str | Path | None = None,
) -> float:
    """
    Score one valid placement of a task.

    This function assumes the optimizer has already checked hard constraints:
    overlap, duration, day bounds, fixed blocks, and dependencies.

    The reward combines:
        1. priority / importance
        2. closeness to preferred time
        3. YAML category/task multipliers
        4. bonus for related tasks near each other
        5. small penalty for awkward tiny gaps
    """

    settings = settings or load_reward_settings(config_path)

    duration = int(_get(task, "duration", 0))
    end_time = start_time + duration

    if _get(task, "fixed", False):
        return 0.0

    priority_score = _priority_score(task, settings)
    time_score = _time_preference_score(task, start_time, end_time, settings)
    relation_score = _relation_score(task, previous_task, next_task, start_time, end_time, settings)
    fragmentation_score = _fragmentation_score(previous_task, next_task, start_time, end_time, settings)

    total = priority_score + time_score + relation_score + fragmentation_score
    return round(total, 2)


def calculate_schedule_score(
    scheduled_tasks: list[Any],
    settings: RewardSettings | None = None,
    config_path: str | Path | None = None,
) -> float:
    """
    Recompute the total score of a full schedule.

    Useful after the optimizer creates the final schedule.
    """

    settings = settings or load_reward_settings(config_path)

    total = 0.0
    tasks = sorted(
        scheduled_tasks,
        key=lambda scheduled: _get(_get(scheduled, "time_window"), "start_time", 0),
    )

    for index, scheduled in enumerate(tasks):
        task_start = _get(_get(scheduled, "time_window"), "start_time", 0)
        previous_task = tasks[index - 1] if index > 0 else None
        next_task = tasks[index + 1] if index + 1 < len(tasks) else None

        # If this is already a ScheduledTask, it may not have priority/duration.
        # In that case keep its existing score.
        if not hasattr(scheduled, "priority") and not hasattr(scheduled, "duration"):
            total += float(_get(scheduled, "score", 0.0))
            continue

        total += calculate_task_score(
            scheduled,
            task_start,
            previous_task=previous_task,
            next_task=next_task,
            settings=settings,
        )

    return round(total, 2)


# Backward-compatible aliases in case your older code used these names.
score_task = calculate_task_score
score_schedule = calculate_schedule_score


# -----------------------------
# Reward components
# -----------------------------


def _priority_score(task: Any, settings: RewardSettings) -> float:
    priority = float(_get(task, "priority", 1))
    category = str(_get(task, "category", "")).lower()
    task_name = str(_get(task, "name", ""))

    category_multiplier = settings.category_weights.get(category, 1.0)
    task_multiplier = settings.task_weights.get(task_name, 1.0)

    return priority * settings.weight_importance * category_multiplier * task_multiplier


def _time_preference_score(
    task: Any,
    start_time: int,
    end_time: int,
    settings: RewardSettings,
) -> float:
    preferred_window = _preferred_window_for_task(task, settings)

    if preferred_window is None:
        return 0.0

    preferred_start = int(preferred_window["start_time"])
    preferred_end = int(preferred_window["end_time"])

    scheduled_center = (start_time + end_time) / 2
    preferred_center = (preferred_start + preferred_end) / 2

    # Full bonus if the scheduled task is fully inside its preferred window.
    if preferred_start <= start_time and end_time <= preferred_end:
        return settings.weight_time_bonus

    distance = abs(scheduled_center - preferred_center)
    decay = max(0.0, 1.0 - (distance / settings.max_time_distance_minutes))

    return settings.weight_time_bonus * decay


def _preferred_window_for_task(task: Any, settings: RewardSettings) -> dict[str, int] | None:
    task_name = str(_get(task, "name", ""))
    category = str(_get(task, "category", "")).lower()

    if task_name in settings.task_time_windows:
        return settings.task_time_windows[task_name]

    if category in settings.category_time_windows:
        return settings.category_time_windows[category]

    preference_time = _get(task, "preference_time", None)
    if preference_time is not None:
        start_time = _get(preference_time, "start_time", None)
        end_time = _get(preference_time, "end_time", None)
        if start_time is not None and end_time is not None:
            return {"start_time": int(start_time), "end_time": int(end_time)}

    return None


def _relation_score(
    task: Any,
    previous_task: Any | None,
    next_task: Any | None,
    start_time: int,
    end_time: int,
    settings: RewardSettings,
) -> float:
    score = 0.0

    for neighbor in [previous_task, next_task]:
        if neighbor is None:
            continue

        neighbor_window = _get(neighbor, "time_window", None)
        if neighbor_window is None:
            continue

        neighbor_start = int(_get(neighbor_window, "start_time", 0))
        neighbor_end = int(_get(neighbor_window, "end_time", 0))

        gap = max(neighbor_start - end_time, start_time - neighbor_end, 0)

        if gap > settings.same_tag_window_minutes:
            continue

        if _same_or_related_tag(task, neighbor, settings):
            score += settings.weight_tag_relation
        elif str(_get(task, "category", "")).lower() == str(_get(neighbor, "category", "")).lower():
            score += settings.weight_tag_relation * 0.5

    return score


def _fragmentation_score(
    previous_task: Any | None,
    next_task: Any | None,
    start_time: int,
    end_time: int,
    settings: RewardSettings,
) -> float:
    """
    Penalize tiny unusable gaps.

    Example:
        A 10-minute empty space between tasks is usually not useful if the app
        schedules in 30-minute blocks, so the score should discourage it.
    """

    penalty = 0.0

    for neighbor in [previous_task, next_task]:
        if neighbor is None:
            continue

        neighbor_window = _get(neighbor, "time_window", None)
        if neighbor_window is None:
            continue

        neighbor_start = int(_get(neighbor_window, "start_time", 0))
        neighbor_end = int(_get(neighbor_window, "end_time", 0))

        gap = max(neighbor_start - end_time, start_time - neighbor_end, 0)

        if 0 < gap < settings.min_gap_between_tasks_minutes:
            penalty += settings.weight_fragmentation_penalty

    return penalty


def _same_or_related_tag(task: Any, neighbor: Any, settings: RewardSettings) -> bool:
    task_tag = str(_get(task, "tag", "")).lower()
    neighbor_tag = str(_get(neighbor, "tag", "")).lower()

    if not task_tag or not neighbor_tag:
        return False

    if task_tag == neighbor_tag:
        return True

    related_to_task = [tag.lower() for tag in settings.tag_relations.get(task_tag, [])]
    related_to_neighbor = [tag.lower() for tag in settings.tag_relations.get(neighbor_tag, [])]

    return neighbor_tag in related_to_task or task_tag in related_to_neighbor


# -----------------------------
# Small parsing helpers
# -----------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default

    if isinstance(obj, dict):
        return obj.get(key, default)

    return getattr(obj, key, default)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}

    return {str(key): float(val) for key, val in value.items()}


def _window_dict(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        return {}

    result: dict[str, dict[str, int]] = {}

    for name, window in value.items():
        if not isinstance(window, dict):
            continue

        if "start_time" not in window or "end_time" not in window:
            continue

        result[str(name)] = {
            "start_time": int(window["start_time"]),
            "end_time": int(window["end_time"]),
        }

    return result


def _list_dict(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}

    result: dict[str, list[str]] = {}

    for key, values in value.items():
        if isinstance(values, list):
            result[str(key).lower()] = [str(item).lower() for item in values]
        else:
            result[str(key).lower()] = [str(values).lower()]

    return result
