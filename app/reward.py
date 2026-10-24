"""
reward.py

How good a valid placement of a task is: the scheduler's score. The engine
(app/optimizer.py) checks every hard constraint -- day window, fixed blocks,
overlaps, durations, deadlines, required work -- before anything is scored;
this module only ranks placements that are already allowed.

A placement of `task` at [start, end), between its chronological neighbours,
scores the sum of (rounded to 2 decimals):

    1. weight         weight_importance x the category's multiplier
                      (category_weights; 1 when absent). Constant in start.
    2. time           weight_time_bonus while the placement lies inside the
                      task's preferred window; otherwise the bonus decays
                      linearly with the distance between the placement's and
                      the window's centres, reaching 0 at
                      max_time_distance_minutes. 0 without a window.
    3. relation       per neighbour at most same_tag_window_minutes away:
                      weight_tag_relation for the same or a related tag
                      (tag_relations), else half of it for the same category.
    4. fragmentation  weight_fragmentation_penalty per neighbour that leaves a
                      gap of 0 < gap < min_gap_between_tasks_minutes.
    5. short gap      (adhd_mode only; short_gap_bonus_weight 0 disables it)
                      a bounded bonus for a short task that fills what would
                      otherwise be a stranded gap -- see _short_gap_bonus_score.

The engine's event search (app/optimizer._event_breakpoints) relies on each
component being constant or affine in the start between its breakpoints:
change a formula here and that derivation must change with it.

Settings: RewardSettings, from config/task_preference.yaml
(load_reward_settings; the template layer of the user's preferences) or from
a day's resolved preferences (app.planning.preferences.
day_preferences_to_reward_settings, which is what the engine scores with).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from app import runtime


# -----------------------------
# Reward configuration model
# -----------------------------


@dataclass(frozen=True)
class RewardSettings:
    """
    Reward weights and parameters. The defaults are conservative so scoring
    works even when the YAML file is missing or incomplete.
    """

    weight_importance: float = 5.0
    weight_time_bonus: float = 3.0
    weight_tag_relation: float = 2.0
    weight_fragmentation_penalty: float = -4.0
    #: Carried with the preferences for compatibility; no component reads it.
    weight_category_bonus: float = 1.0

    max_time_distance_minutes: int = 240
    same_tag_window_minutes: int = 120
    min_gap_between_tasks_minutes: int = 30

    #: Category -> multiplier of the weight component (case-insensitive lookup by lowercased category).
    category_weights: dict[str, float] = field(default_factory=dict)

    #: Category -> preferred window in local minutes ({"start_time": 540, "end_time": 720}). Read from the YAML
    #: template into the preference layers (category_preferred_windows); scoring takes the window a task
    #: actually has from its ScoringTask, never from here.
    category_time_windows: dict[str, dict[str, int]] = field(default_factory=dict)

    #: Tag -> related tags (lowercased): a related neighbour counts like the same tag.
    tag_relations: dict[str, list[str]] = field(default_factory=dict)

    #: The bounded ADHD-only short-gap bonus (see _short_gap_bonus_score); weight 0 disables it.
    short_gap_bonus_weight: float = 0.0
    short_gap_bonus_max_minutes: int = 20
    short_gap_bonus_cap: float = 10.0

    def __post_init__(self) -> None:
        import dataclasses
        import math

        # NaN and infinity pass every comparison below and then poison every score they touch.
        for item in dataclasses.fields(self):
            value = getattr(self, item.name)
            numbers = value.values() if isinstance(value, dict) else (value,)
            for number in numbers:
                if isinstance(number, float) and not math.isfinite(number):
                    raise ValueError(f"{item.name} must be a finite number, got {number!r}.")
        # _time_score divides by max_time_distance_minutes whenever a placement is not fully inside its
        # preferred window, so a nonpositive value would fail deep inside scoring. Reject it here.
        if self.max_time_distance_minutes <= 0:
            raise ValueError(
                "max_time_distance_minutes must be a positive number of minutes, "
                f"got {self.max_time_distance_minutes!r}."
            )
        if self.short_gap_bonus_max_minutes <= 0:
            raise ValueError(
                "short_gap_bonus_max_minutes must be a positive number of minutes, "
                f"got {self.short_gap_bonus_max_minutes!r}."
            )
        if self.short_gap_bonus_weight < 0:
            raise ValueError(
                "short_gap_bonus_weight must be >= 0 (0 disables the bonus), "
                f"got {self.short_gap_bonus_weight!r}."
            )
        if self.short_gap_bonus_cap < 0:
            raise ValueError(f"short_gap_bonus_cap must be >= 0, got {self.short_gap_bonus_cap!r}.")


@dataclass(frozen=True)
class ScoringTask:
    """
    The reward's view of a task: its category and first tag (for the weight
    and relation components), its duration, and its preferred window in the
    same day-relative minutes as the start being scored (None: no time
    preference).
    """

    category: str
    duration: int
    tag: str = ""
    preferred_window: tuple[int, int] | None = None


class Neighbour(Protocol):
    """An already-placed interval next to the placement being scored (a task or a fixed block)."""

    start: int
    end: int
    category: str
    tag: str


# -----------------------------
# YAML loading
# -----------------------------


#: The auto-discovered template filename, in the project's (or the bundle's) config/ directory.
CANONICAL_CONFIG_FILENAME = "task_preference.yaml"
#: A preference file is small; anything larger is not one (and deeply nested YAML is slow to load).
MAX_CONFIG_BYTES = 256 * 1024


def load_reward_settings(
    config_path: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> RewardSettings:
    """
    Load reward settings from YAML: `config_path` exactly (FileNotFoundError
    if it does not exist), else CANONICAL_CONFIG_FILENAME in `project_root`'s
    config/ directory -- this project's own root (from source) or the bundled
    resources (packaged; app/runtime.py), never the current directory. No
    file: RewardSettings' defaults. Missing values use the defaults too.
    """
    path = _resolve_config_path(config_path, project_root=project_root)
    if path is None:
        return RewardSettings()

    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            f"PyYAML is required to read {CANONICAL_CONFIG_FILENAME}. "
            "Install it with: pip install PyYAML"
        ) from exc

    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_CONFIG_BYTES // 1024} KiB; a preference file is a few "
                         "hundred lines at most.")
    with path.open("r", encoding="utf-8") as file:
        raw_data = yaml.safe_load(file) or {}  # safe_load: plain data only, never arbitrary objects

    if not isinstance(raw_data, dict):
        raise ValueError(f"{path.name} must contain a YAML dictionary at the top level.")

    return _settings_from_dict(raw_data)


def _resolve_config_path(config_path: str | Path | None, *, project_root: str | Path | None = None) -> Path | None:
    if config_path is not None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Reward config file not found: {path}")
        return path
    root = Path(project_root) if project_root is not None else runtime.resource_root()
    canonical = root / "config" / CANONICAL_CONFIG_FILENAME
    return canonical if canonical.exists() else None


def _settings_from_dict(data: Mapping[str, Any]) -> RewardSettings:
    weights = _as_dict(data.get("weights"))
    preference = _as_dict(data.get("preference"))
    short_gap_bonus = _as_dict(data.get("short_gap_bonus"))

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
        category_time_windows=_window_dict(data.get("category_time_windows")),
        tag_relations=_list_dict(data.get("tag_relations")),

        short_gap_bonus_weight=float(short_gap_bonus.get("weight", 0.0)),
        short_gap_bonus_max_minutes=int(short_gap_bonus.get("max_minutes", 20)),
        short_gap_bonus_cap=float(short_gap_bonus.get("cap", 10.0)),
    )


# -----------------------------
# Scoring
# -----------------------------


def calculate_task_score(
    task: ScoringTask,
    start_time: int,
    previous_task: Neighbour | None = None,
    next_task: Neighbour | None = None,
    settings: RewardSettings | None = None,
    *,
    adhd_mode: bool = False,
    day_start: int | None = None,
    day_end: int | None = None,
) -> float:
    """
    Score one valid placement of `task` starting at `start_time` (see the
    module docstring for the components). day_start/day_end bound the day
    for the short-gap bonus only, which treats a day edge like a neighbour;
    leave them None to skip that side.
    """
    settings = settings or load_reward_settings()
    end_time = start_time + task.duration

    total = (
        _weight_score(task, settings)
        + _time_score(task, start_time, end_time, settings)
        + _relation_score(task, previous_task, next_task, start_time, end_time, settings)
        + _fragmentation_score(previous_task, next_task, start_time, end_time, settings)
        + _short_gap_bonus_score(start_time, end_time, previous_task, next_task, day_start, day_end, settings,
                                 adhd_mode)
    )
    return round(total, 2)


def _weight_score(task: ScoringTask, settings: RewardSettings) -> float:
    return settings.weight_importance * settings.category_weights.get(task.category.lower(), 1.0)


def _time_score(task: ScoringTask, start_time: int, end_time: int, settings: RewardSettings) -> float:
    if task.preferred_window is None:
        return 0.0
    preferred_start, preferred_end = task.preferred_window
    # Full bonus while the placement is fully inside its preferred window.
    if preferred_start <= start_time and end_time <= preferred_end:
        return settings.weight_time_bonus
    distance = abs((start_time + end_time) / 2 - (preferred_start + preferred_end) / 2)
    return settings.weight_time_bonus * max(0.0, 1.0 - distance / settings.max_time_distance_minutes)


def _gap(neighbour: Neighbour, start_time: int, end_time: int) -> int:
    return max(neighbour.start - end_time, start_time - neighbour.end, 0)


def _relation_score(
    task: ScoringTask,
    previous_task: Neighbour | None,
    next_task: Neighbour | None,
    start_time: int,
    end_time: int,
    settings: RewardSettings,
) -> float:
    score = 0.0
    for neighbour in (previous_task, next_task):
        if neighbour is None or _gap(neighbour, start_time, end_time) > settings.same_tag_window_minutes:
            continue
        if _same_or_related_tag(task.tag, neighbour.tag, settings):
            score += settings.weight_tag_relation
        elif task.category.lower() == neighbour.category.lower():
            score += settings.weight_tag_relation * 0.5
    return score


def _fragmentation_score(
    previous_task: Neighbour | None,
    next_task: Neighbour | None,
    start_time: int,
    end_time: int,
    settings: RewardSettings,
) -> float:
    """Penalize tiny gaps (0 < gap < min_gap_between_tasks_minutes) next to a real neighbour: rarely usable."""
    penalty = 0.0
    for neighbour in (previous_task, next_task):
        if neighbour is not None and 0 < _gap(neighbour, start_time, end_time) < settings.min_gap_between_tasks_minutes:
            penalty += settings.weight_fragmentation_penalty
    return penalty


def _short_gap_bonus_score(
    start_time: int,
    end_time: int,
    previous_task: Neighbour | None,
    next_task: Neighbour | None,
    day_start: int | None,
    day_end: int | None,
    settings: RewardSettings,
    adhd_mode: bool,
) -> float:
    """
    Bounded, deterministic, ADHD-only bonus for a short task that starts (or
    ends) flush against a neighbour or day edge where the gap was already
    small enough to be "stranded" (below min_gap_between_tasks_minutes).

    Per side: pre_gap = the distance from the neighbour's edge (or the day
    edge when there is no neighbour on that side) to this placement's edge;
    if 0 <= pre_gap < min_gap, side_bonus = short_gap_bonus_weight x
    (1 - pre_gap / min_gap), else 0. total = min(sum of both sides,
    short_gap_bonus_cap), rounded to 2 decimals.

    0 unless adhd_mode, short_gap_bonus_weight > 0 and the placement lasts at
    most short_gap_bonus_max_minutes.
    """
    if not adhd_mode or settings.short_gap_bonus_weight <= 0:
        return 0.0
    if end_time - start_time > settings.short_gap_bonus_max_minutes:
        return 0.0
    min_gap = settings.min_gap_between_tasks_minutes
    if min_gap <= 0:
        return 0.0

    bonus = 0.0
    if previous_task is not None:
        bonus += _stranded_gap_bonus(start_time - previous_task.end, min_gap, settings.short_gap_bonus_weight)
    elif day_start is not None:
        bonus += _stranded_gap_bonus(start_time - day_start, min_gap, settings.short_gap_bonus_weight)
    if next_task is not None:
        bonus += _stranded_gap_bonus(next_task.start - end_time, min_gap, settings.short_gap_bonus_weight)
    elif day_end is not None:
        bonus += _stranded_gap_bonus(day_end - end_time, min_gap, settings.short_gap_bonus_weight)
    return round(min(bonus, settings.short_gap_bonus_cap), 2)


def _stranded_gap_bonus(pre_gap: int, min_gap: int, weight: float) -> float:
    if pre_gap < 0 or pre_gap >= min_gap:
        return 0.0
    return weight * (1 - pre_gap / min_gap)


def _same_or_related_tag(task_tag: str, neighbour_tag: str, settings: RewardSettings) -> bool:
    task_tag, neighbour_tag = task_tag.lower(), neighbour_tag.lower()
    if not task_tag or not neighbour_tag:
        return False
    if task_tag == neighbour_tag:
        return True
    related_to_task = [tag.lower() for tag in settings.tag_relations.get(task_tag, [])]
    related_to_neighbour = [tag.lower() for tag in settings.tag_relations.get(neighbour_tag, [])]
    return neighbour_tag in related_to_task or task_tag in related_to_neighbour


# -----------------------------
# Small parsing helpers
# -----------------------------


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
        if not isinstance(window, dict) or "start_time" not in window or "end_time" not in window:
            continue
        result[str(name)] = {"start_time": int(window["start_time"]), "end_time": int(window["end_time"])}
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
