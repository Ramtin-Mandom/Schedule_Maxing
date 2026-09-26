"""
app/ui/preferences_model.py

The Tk-free model behind the native preference editor (Milestone 4,
Prompt 4): which preference fields the desktop shows, how each is displayed
and parsed, and how one field of one stored layer is set, inherited again or
cleared. Day Preferences uses it for a date layer; the later default
Settings page uses the same fields for the user layer.

Only controls the active day engines actually read are listed (see
app/reward.py's calculate_task_score and app/optimizer.py): the scheduling
window, the reward weights and distances, the per-category importance and
preferred times, and -- only while ADHD friendly is the effective engine --
the short-gap bonus. weight_category_bonus (carried but never read) and the
legacy Greedy Optimizer v1 / annealing settings are deliberately absent.

Layer semantics (app/planning/preferences.py) are kept exactly:

    - a scalar field is either absent from a layer (inherit) or set;
    - a per-category field is absent (inherit), set, or explicitly cleared
      (stored as null: "no value from here up", so a lower layer's value no
      longer applies and the neutral fallback does).

Every change returns a new PreferenceOverrides; nothing is mutated and
nothing is saved here (the controller saves through PlanningController).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.planning.models import LocalTimeWindow
from app.planning.preferences import (
    DayPreferences,
    DayWindowSpec,
    OptimizerMode,
    PreferenceOverrides,
    RewardPreferencesOverride,
)
from app.ui.task_form_model import CATEGORIES
from app.ui.time_fields import FieldError, format_clock, format_duration, parse_clock

FieldKind = Literal["window", "number", "minutes", "category_number", "category_window", "relations"]
LayerState = Literal["inherited", "set", "cleared"]
#: A field's typed value: one text, or (start, end) texts for a window.
FieldInput = str | tuple[str, str]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    help: str
    kind: FieldKind
    group: str
    #: The engines that read this field (None: every engine).
    engines: frozenset[OptimizerMode] | None = None

    @property
    def category(self) -> str | None:
        return self.key.split(":", 1)[1] if ":" in self.key else None

    @property
    def clearable(self) -> bool:
        """Per-category fields have the third, explicit "no preference" state."""
        return self.kind in ("category_number", "category_window")


_ADHD = frozenset({OptimizerMode.ADHD_FRIENDLY})

#: The fields every scope shows (category fields are added per category by field_specs).
BASE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("day_window", "Scheduling window",
              "Flexible tasks are placed only inside this part of the day. Fixed blocks are shown either way.",
              "window", "Day"),
    FieldSpec("reward.weight_importance", "Priority weight",
              "How strongly higher-priority tasks win the better times.", "number", "Scoring"),
    FieldSpec("reward.weight_time_bonus", "Preferred-time weight",
              "How strongly a task is drawn toward its preferred time.", "number", "Scoring"),
    FieldSpec("reward.max_time_distance_minutes", "Preferred-time reach",
              "How far from its preferred time a task still gets part of that bonus.", "minutes", "Scoring"),
    FieldSpec("reward.weight_tag_relation", "Related-tag weight",
              "Bonus for placing tasks with the same or related tags near each other.", "number", "Scoring"),
    FieldSpec("reward.same_tag_window_minutes", "Related-tag distance",
              "How close related tasks must be to earn that bonus.", "minutes", "Scoring"),
    FieldSpec("reward.tag_relations", "Related tags",
              "Use study = reading, writing; exercise = walking. Empty means no relations. "
              "Scoring currently uses each task's first tag; all tags are preserved.", "relations", "Scoring"),
    FieldSpec("reward.weight_fragmentation_penalty", "Fragmentation penalty",
              "Usually negative: discourages leaving gaps shorter than the preferred spacing.", "number", "Scoring"),
    FieldSpec("reward.min_gap_between_tasks_minutes", "Preferred spacing between tasks",
              "A soft preference, not a guaranteed break: tasks can still be placed back to back when time is short.",
              "minutes", "Scoring"),
    FieldSpec("reward.short_gap_bonus_weight", "Short-gap bonus",
              "ADHD friendly only: rewards short tasks that fill small gaps. 0 turns it off.", "number", "ADHD friendly",
              _ADHD),
    FieldSpec("reward.short_gap_bonus_max_minutes", "Short-gap size",
              "ADHD friendly only: the largest gap the short-gap bonus looks at.", "minutes", "ADHD friendly", _ADHD),
    FieldSpec("reward.short_gap_bonus_cap", "Short-gap bonus cap",
              "ADHD friendly only: the most one placement can earn from the short-gap bonus.", "number",
              "ADHD friendly", _ADHD),
)


def field_categories(*sources: Iterable[str]) -> list[str]:
    """The desktop's categories, then any other category a layer mentions (kept, never renamed)."""
    extra = sorted({category for source in sources for category in source} - set(CATEGORIES))
    return [*CATEGORIES, *extra]


def field_specs(engine: OptimizerMode, categories: Iterable[str]) -> list[FieldSpec]:
    """The fields that mean something for `engine`, in display order."""
    specs = [spec for spec in BASE_FIELDS if spec.engines is None or engine in spec.engines]
    for category in categories:
        name = category.replace("_", " ").capitalize()
        specs.append(FieldSpec(f"category_multipliers:{category}", f"{name}: importance",
                               "Multiplies the priority of this category's tasks (1 is neutral).", "category_number",
                               "Categories"))
        specs.append(FieldSpec(f"category_preferred_windows:{category}", f"{name}: preferred time",
                               "Used for tasks of this category that have no preferred time of their own.",
                               "category_window", "Categories"))
    return specs


# -----------------------------------------------------------------------------
# Reading values
# -----------------------------------------------------------------------------


def _window_text(start: int, end: int) -> str:
    return f"{format_clock(start)} – {format_clock(end)}"


def _number_text(value: float) -> str:
    return f"{value:g}"


def effective_value(preferences: DayPreferences, spec: FieldSpec) -> object:
    """The resolved value of a field (None: no value, e.g. a category without a preferred time)."""
    if spec.key == "day_window":
        return preferences.day_window
    if spec.key.startswith("reward."):
        return getattr(preferences.reward, spec.key.split(".", 1)[1])
    if spec.kind == "category_number":
        return preferences.category_multipliers.get(spec.category)
    return preferences.category_preferred_windows.get(spec.category)


def display(spec: FieldSpec, value: object) -> str:
    """A resolved value in words."""
    if spec.kind == "relations":
        return "; ".join(f"{tag} = {', '.join(related)}" for tag, related in value.items()) or "No relations"
    if spec.kind == "window" and isinstance(value, DayWindowSpec):
        end = 1440 if value.end_day_offset == 1 else value.end_minute
        return "Whole day" if (value.start_minute, end) == (0, 1440) else _window_text(value.start_minute, end)
    if spec.kind == "category_window":
        return _window_text(value.start_minute, value.end_minute) if value is not None else "No preferred time"
    if spec.kind == "category_number":
        return _number_text(value) if value is not None else "1 (neutral)"
    if spec.kind == "minutes":
        return format_duration(int(value)) if value else "0 min"
    return _number_text(float(value))


def edit_value(spec: FieldSpec, value: object) -> FieldInput:
    """A resolved value as the editor pre-fills it."""
    if spec.kind == "relations":
        return display(spec, value) if value else ""
    if spec.kind in ("window", "category_window"):
        if value is None:
            return ("", "")
        if isinstance(value, DayWindowSpec):
            end = 1440 if value.end_day_offset == 1 else value.end_minute
            return (format_clock(value.start_minute), format_clock(end))
        return (format_clock(value.start_minute), format_clock(value.end_minute))
    if spec.kind == "category_number":
        return _number_text(value) if value is not None else "1"
    if spec.kind == "minutes":
        return str(int(value))
    return _number_text(float(value))


def layer_state(overrides: PreferenceOverrides | None, spec: FieldSpec) -> LayerState:
    """Whether one stored layer inherits, sets or (per-category fields only) clears a field."""
    if overrides is None:
        return "inherited"
    if spec.key == "day_window":
        return "set" if overrides.day_window is not None else "inherited"
    if spec.key.startswith("reward."):
        return "set" if getattr(overrides.reward, spec.key.split(".", 1)[1]) is not None else "inherited"
    mapping = overrides.category_multipliers if spec.kind == "category_number" else overrides.category_preferred_windows
    if spec.category not in mapping:
        return "inherited"
    return "cleared" if mapping[spec.category] is None else "set"


# -----------------------------------------------------------------------------
# Rows for the editor
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PreferenceRow:
    spec: FieldSpec
    #: What scheduling uses (with this layer).
    effective_text: str
    #: What it would use without this layer's value.
    inherited_text: str
    state: LayerState
    #: Where the effective value comes from, in words.
    source: str
    edit: FieldInput


def preference_rows(
    specs: Iterable[FieldSpec],
    *,
    effective: DayPreferences,
    inherited: DayPreferences,
    layer: PreferenceOverrides | None,
    lower_layer: PreferenceOverrides | None = None,
    layer_name: str = "this date",
    lower_name: str = "your defaults",
) -> list[PreferenceRow]:
    """
    One row per field: the effective and inherited values and where the
    value comes from. `layer` is the edited layer (a date layer, or for
    Settings the user layer); `lower_layer` the stored layer below it that
    the source names (the user layer for a date; None for Settings).
    """
    rows = []
    for spec in specs:
        state = layer_state(layer, spec)
        if state == "set":
            source = f"Set for {layer_name}"
        elif state == "cleared":
            source = f"No preference for {layer_name} (inherited value cleared)"
        elif lower_layer is not None and layer_state(lower_layer, spec) != "inherited":
            source = f"Inherited from {lower_name}"
        else:
            source = "Inherited app default"
        value = effective_value(effective, spec)
        rows.append(PreferenceRow(
            spec=spec, effective_text=display(spec, value), inherited_text=display(spec, effective_value(inherited, spec)),
            state=state, source=source, edit=edit_value(spec, value),
        ))
    return rows


# -----------------------------------------------------------------------------
# Changing one field of one layer
# -----------------------------------------------------------------------------


def _parse_number(text: str, spec: FieldSpec) -> float:
    raw = (text or "").strip()
    try:
        value = float(raw)
    except ValueError:
        raise FieldError(f"{spec.label} must be a number, like 2 or 0.5.") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise FieldError(f"{spec.label} must be a finite number.")
    return value


def _parse_minutes(text: str, spec: FieldSpec) -> int:
    raw = (text or "").strip().lower().removesuffix("min").removesuffix("minutes").strip()
    if not raw.isdigit():
        raise FieldError(f"{spec.label} is a whole number of minutes, like 30.")
    return int(raw)


def _parse_window(value: FieldInput, spec: FieldSpec) -> tuple[int, int]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise FieldError(f"{spec.label} needs a start and an end time.")
    start = parse_clock(value[0])
    end = parse_clock(value[1], end_of_interval=True)
    if end <= start:
        raise FieldError(f"{spec.label} must end after it starts (overnight windows are not supported).")
    return start, end


def with_value(overrides: PreferenceOverrides, spec: FieldSpec, value: FieldInput) -> PreferenceOverrides:
    """`overrides` with this field set to the typed value (FieldError if it cannot be read)."""
    data = overrides.model_copy(deep=True)
    if spec.key == "day_window":
        start, end = _parse_window(value, spec)
        return data.model_copy(update={"day_window": DayWindowSpec(start_minute=start, end_minute=end)})
    if spec.key.startswith("reward."):
        name = spec.key.split(".", 1)[1]
        if spec.kind == "relations":
            parsed = {}
            for pair in str(value).split(";"):
                if not pair.strip():
                    continue
                tag, separator, related = pair.partition("=")
                if not separator or not tag.strip() or not related.strip():
                    raise FieldError("Use tag = related, other; another = related. Empty removes all relations.")
                tag = tag.strip()
                if tag in parsed:
                    raise FieldError(f"{tag} appears twice; combine its related tags in one entry.")
                parsed[tag] = list(dict.fromkeys(item.strip() for item in related.split(",") if item.strip()))
        else:
            parsed = _parse_minutes(value, spec) if spec.kind == "minutes" else _parse_number(value, spec)
        reward = data.reward.model_copy(update={name: parsed})
        return data.model_copy(update={"reward": RewardPreferencesOverride.model_validate(reward.model_dump())})
    if spec.kind == "category_number":
        mapping = dict(data.category_multipliers)
        mapping[spec.category] = _parse_number(value, spec)
        return data.model_copy(update={"category_multipliers": mapping})
    start, end = _parse_window(value, spec)
    windows = dict(data.category_preferred_windows)
    windows[spec.category] = LocalTimeWindow(start_minute=start, end_minute=end)
    return data.model_copy(update={"category_preferred_windows": windows})


def inherit(overrides: PreferenceOverrides, spec: FieldSpec) -> PreferenceOverrides:
    """`overrides` without any value of its own for this field (absent: the lower layers show through)."""
    data = overrides.model_copy(deep=True)
    if spec.key == "day_window":
        return data.model_copy(update={"day_window": None})
    if spec.key.startswith("reward."):
        return data.model_copy(update={"reward": data.reward.model_copy(update={spec.key.split(".", 1)[1]: None})})
    name = "category_multipliers" if spec.kind == "category_number" else "category_preferred_windows"
    mapping = dict(getattr(data, name))
    mapping.pop(spec.category, None)
    return data.model_copy(update={name: mapping})


def clear(overrides: PreferenceOverrides, spec: FieldSpec) -> PreferenceOverrides:
    """`overrides` with a per-category field explicitly cleared (stored null: no value from this layer up)."""
    if not spec.clearable:
        raise ValueError(f"{spec.key} has no cleared state; inherit it instead.")
    name = "category_multipliers" if spec.kind == "category_number" else "category_preferred_windows"
    data = overrides.model_copy(deep=True)
    mapping = dict(getattr(data, name))
    mapping[spec.category] = None
    return data.model_copy(update={name: mapping})
