"""
reward.py

Reward/scoring logic for the schedule optimizer.

Discovery precedence (Task 3 / Schedule Maxing v2 fix -- see below for the
defect this replaces):
    1. An explicit `config_path` passed to load_reward_settings()/
       optimize_day_schedule(): that exact file is loaded, or
       FileNotFoundError is raised if it does not exist.
    2. CANONICAL_CONFIG_FILENAME ("task_preference.yaml", singular) inside
       this project's own config/ directory -- the checked-in template.
    3. The first of LEGACY_CONFIG_FILENAMES (in the fixed order below,
       first match wins) found in the same config/ directory, for
       migration compatibility with a config file created under an older
       supported name:
           - task_preferences.yaml (the plural name this repo's template
             used before this fix)
           - task_prefrence.yaml / task_prefrence.yml (the old misspelling)
           - task_preference.yml (the old singular .yml variant)
    4. RewardSettings' built-in Python defaults, if none of the above exist.

"This project's own config/ directory" is resolved from app/reward.py's own
file location (its grandparent directory's config/ subdirectory), never
from the current working directory and never by walking ancestor or home
directories -- so default discovery works identically regardless of where
`python -m app.main` / `python -m app.app` / pytest is invoked from, and
never picks up an unrelated file from somewhere above an unrelated cwd.
Tests that need an isolated project root (e.g. asserting "no config found")
pass an explicit `project_root=` override instead of relying on chdir.

Historical defect this replaces: prior to this fix, default (no
config_path) discovery searched the current directory, a `config/`
subdirectory, and every ancestor directory (plus each ancestor's own
config/) for task_prefrence.yaml/task_prefrence.yml/task_preference.yaml/
task_preference.yml -- but this repository's actual checked-in file was
`config/task_preferences.yaml` (plural), which was never in that candidate
list, so default discovery silently never found it and neither app/main.py
nor app/app.py passed an explicit config_path, meaning the checked-in YAML
had no effect on scheduling in practice. This module now migrates the
checked-in template to the canonical singular filename (recognizing the
old plural filename as a legacy variant for anyone who already created
one), and default discovery finds it directly -- editing
config/task_preference.yaml now does affect `python -m app.main` and
`python -m app.app` without any code change on their part.

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

    # Bounded ADHD-only short-gap-filling bonus (Task 4 / Schedule Maxing
    # v2). Read by calculate_task_score only when its own adhd_mode=True is
    # passed explicitly -- see _short_gap_bonus_score. weight=0.0 (the
    # default) disables the bonus entirely, including in adhd_mode.
    short_gap_bonus_weight: float = 0.0
    short_gap_bonus_max_minutes: int = 20
    short_gap_bonus_cap: float = 10.0

    def __post_init__(self) -> None:
        # _time_preference_score divides by max_time_distance_minutes whenever
        # a placement is not fully inside its preferred window, so a
        # nonpositive value would raise ZeroDivisionError deep inside scoring
        # instead of at configuration load time. Reject it clearly here.
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


# -----------------------------
# YAML loading
# -----------------------------


#: The canonical, auto-discovered template filename (see module docstring).
CANONICAL_CONFIG_FILENAME = "task_preference.yaml"

#: Recognized legacy filenames, checked in this fixed order (first match
#: wins) only after the canonical filename is confirmed absent. Do not add
#: further speculative spellings here -- only names this module has
#: actually, historically supported.
LEGACY_CONFIG_FILENAMES: tuple[str, ...] = (
    "task_preferences.yaml",  # this repo's pre-fix plural filename
    "task_prefrence.yaml",
    "task_prefrence.yml",
    "task_preference.yml",
)


def load_reward_settings(
    config_path: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> RewardSettings:
    """
    Load reward settings from YAML.

    If `config_path` is provided, that exact path is used (raising
    FileNotFoundError if it does not exist). Otherwise, the canonical/
    legacy template search in `project_root`'s config/ directory is used
    -- see module docstring and _resolve_config_path. `project_root`
    defaults to this project's own root (this module's grandparent
    directory); pass it explicitly in tests that need an isolated,
    guaranteed-empty search location instead of relying on chdir.

    Missing YAML values are okay. Defaults are used for anything not specified.
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

    with path.open("r", encoding="utf-8") as file:
        raw_data = yaml.safe_load(file) or {}

    if not isinstance(raw_data, dict):
        raise ValueError(f"{path.name} must contain a YAML dictionary at the top level.")

    return _settings_from_dict(raw_data)


def _default_project_root() -> Path:
    """This project's own root directory, derived from this module's file
    location (app/reward.py -> app/ -> project root), independent of cwd."""
    return Path(__file__).resolve().parent.parent


def _resolve_config_path(
    config_path: str | Path | None,
    *,
    project_root: str | Path | None = None,
) -> Path | None:
    if config_path is not None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Reward config file not found: {path}")
        return path

    root = Path(project_root) if project_root is not None else _default_project_root()
    config_dir = root / "config"

    canonical = config_dir / CANONICAL_CONFIG_FILENAME
    if canonical.exists():
        return canonical

    for name in LEGACY_CONFIG_FILENAMES:
        candidate = config_dir / name
        if candidate.exists():
            return candidate

    return None


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
        task_weights=_float_dict(data.get("task_weights")),
        task_time_windows=_window_dict(data.get("task_time_windows")),
        category_time_windows=_window_dict(data.get("category_time_windows")),
        tag_relations=_list_dict(data.get("tag_relations")),

        short_gap_bonus_weight=float(short_gap_bonus.get("weight", 0.0)),
        short_gap_bonus_max_minutes=int(short_gap_bonus.get("max_minutes", 20)),
        short_gap_bonus_cap=float(short_gap_bonus.get("cap", 10.0)),
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
    *,
    adhd_mode: bool = False,
    day_start: int | None = None,
    day_end: int | None = None,
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
        6. (adhd_mode only) a bounded bonus for a short task that fills
           what would otherwise be a stranded gap -- see
           _short_gap_bonus_score. Always 0 unless adhd_mode=True is passed
           explicitly and settings.short_gap_bonus_weight > 0; a
           precise_greedy caller (the default) never sees this component,
           so this is backward compatible with every existing caller.

    day_start/day_end (optional) are this day's window bounds, used only by
    the short-gap bonus to treat "flush against the start/end of the day"
    the same as "flush against a neighboring task." Omit them (or leave
    previous_task/next_task as None with no day bound) to simply skip that
    side's bonus contribution.
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
    short_gap_bonus = _short_gap_bonus_score(
        start_time, end_time, previous_task, next_task, day_start, day_end, settings, adhd_mode
    )

    total = priority_score + time_score + relation_score + fragmentation_score + short_gap_bonus
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


def _short_gap_bonus_score(
    start_time: int,
    end_time: int,
    previous_task: Any | None,
    next_task: Any | None,
    day_start: int | None,
    day_end: int | None,
    settings: RewardSettings,
    adhd_mode: bool,
) -> float:
    """
    Bounded, deterministic, ADHD-only bonus for a short task that starts (or
    ends) flush against a neighbor/day-boundary on a side where the
    pre-placement gap was already small enough to be flagged by
    _fragmentation_score's own min_gap_between_tasks_minutes threshold --
    i.e. a gap that would otherwise likely go unused ("stranded").

    Formula (per side, before/after):
        pre_gap = distance from the neighbor's edge (or the day boundary,
                  when there is no neighbor on that side) to this
                  candidate's edge on that side -- exactly what
                  _fragmentation_score already measures.
        if 0 <= pre_gap < min_gap_between_tasks_minutes:
            side_bonus = short_gap_bonus_weight * (1 - pre_gap / min_gap_between_tasks_minutes)
        else:
            side_bonus = 0  (the gap on that side was already usable-sized,
                              or this candidate does not touch that side at
                              all -- nothing "stranded" to reclaim)
    total = min(side_bonus_before + side_bonus_after, short_gap_bonus_cap)

    The bonus is 0 unless adhd_mode=True and duration <= short_gap_bonus_max_minutes
    (only "short" placements qualify) and settings.short_gap_bonus_weight > 0
    (0 disables it entirely, including in adhd_mode). It never rewards every
    short task identically -- only placements whose actual geometry lands
    them flush or near-flush against a neighbor/boundary that had a small
    stranded gap -- and the per-placement cap prevents it from growing
    unboundedly by summing over both sides.
    """
    if not adhd_mode or settings.short_gap_bonus_weight <= 0:
        return 0.0

    duration = end_time - start_time
    if duration > settings.short_gap_bonus_max_minutes:
        return 0.0

    min_gap = settings.min_gap_between_tasks_minutes
    if min_gap <= 0:
        return 0.0

    bonus = 0.0

    previous_window = _get(previous_task, "time_window", None) if previous_task is not None else None
    if previous_window is not None:
        neighbor_end = int(_get(previous_window, "end_time", 0))
        pre_gap_before = start_time - neighbor_end
        bonus += _stranded_gap_bonus(pre_gap_before, min_gap, settings.short_gap_bonus_weight)
    elif day_start is not None:
        pre_gap_before = start_time - day_start
        bonus += _stranded_gap_bonus(pre_gap_before, min_gap, settings.short_gap_bonus_weight)

    next_window = _get(next_task, "time_window", None) if next_task is not None else None
    if next_window is not None:
        neighbor_start = int(_get(next_window, "start_time", 0))
        pre_gap_after = neighbor_start - end_time
        bonus += _stranded_gap_bonus(pre_gap_after, min_gap, settings.short_gap_bonus_weight)
    elif day_end is not None:
        pre_gap_after = day_end - end_time
        bonus += _stranded_gap_bonus(pre_gap_after, min_gap, settings.short_gap_bonus_weight)

    return round(min(bonus, settings.short_gap_bonus_cap), 2)


def _stranded_gap_bonus(pre_gap: int, min_gap: int, weight: float) -> float:
    if pre_gap < 0 or pre_gap >= min_gap:
        return 0.0
    return weight * (1 - pre_gap / min_gap)


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
