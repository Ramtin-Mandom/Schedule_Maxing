"""
app/planning/preferences.py

Canonical per-day scheduling preferences (Task 3 / Schedule Maxing v2) and
an adapter bridging them to the existing, frozen Greedy Optimizer v1 reward
engine (app/reward.py).

This module does not implement optimizer modes or day allocation -- that is
Task 4/5's job. It only resolves *what a given day's preferences are* and
gives a way to turn them into the legacy RewardSettings shape so
calculate_task_score/calculate_schedule_score (untouched) can already be
exercised against per-day canonical preferences today.

Layering model (built-in defaults -> YAML template -> optional user-level
overrides -> date-specific overrides; since Milestone 3 the user and date
layers are persisted as PreferenceRecords through PlanningService, while
this module itself stays storage-free):
    resolve_day_preferences applies four layers in increasing precedence.
    Each layer is a PreferenceOverrides instance (or None, meaning "this
    layer contributes nothing"). For the two per-category dict fields
    (category_multipliers, category_preferred_windows), a layer
    distinguishes three states for any given category key:
        - key absent from the layer's dict: this layer says nothing about
          that category; whatever the lower layers resolved to (or the
          neutral fallback, if nothing did) shows through unchanged.
        - key present with a real value: this layer sets/overrides that
          category's value from this point up (an explicit multiplier of
          0.0 is a real, deliberate value -- "give this category no
          priority boost at all" -- not treated as "unset").
        - key present with value None: this layer explicitly *clears* any
          value set by a lower layer for that category, reverting it to
          "unset" (so a still-higher layer, or the neutral fallback if none
          exists, applies). This is different from the key being absent:
          absent never touches what a lower layer decided; None
          deliberately erases it.
    Scalar fields (day_window, optimizer_mode, and each field of `reward`)
    only need two states (absent = inherit, present = override), since
    there is no "erase a scalar back to the previous layer's undetermined
    state" need for those -- a layer either has an opinion or it doesn't.

Immutability: every call to resolve_day_preferences (and every helper here)
builds and returns new dict/model instances. Nothing in this module ever
mutates a caller-supplied PreferenceOverrides/DayPreferences, a module-level
default, or a previously resolved DayPreferences -- editing the date-layer
override for Monday cannot affect Tuesday, the YAML-derived layer, or an
earlier optimization request that already captured its own DayPreferences.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import date as date_
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from app.planning.models import LocalTimeWindow
from app.planning.time import MINUTES_PER_DAY, LocalDayWindow, UnsupportedSchedulingWindowError, validate_timezone
from app.reward import RewardSettings

# -----------------------------------------------------------------------------
# Optimizer mode
# -----------------------------------------------------------------------------


class OptimizerMode(str, Enum):
    """
    Which day-scheduling candidate strategy a day's preferences request.

    This milestone only defines and validates the mode as configuration --
    it does not implement either mode's scheduling behavior (that is
    Task 4). Nothing here or in app/optimizer.py currently reads this value
    to change scheduling; do not describe a Task-3-era run as "precise" or
    "ADHD-friendly" until Task 4 actually wires it in.
    """

    PRECISE_GREEDY = "precise_greedy"
    ADHD_FRIENDLY = "adhd_friendly"


# -----------------------------------------------------------------------------
# Day window
# -----------------------------------------------------------------------------


class DayWindowSpec(BaseModel):
    """
    An explicit local scheduling window for one day, using Task 1's time
    contract (app.planning.time.LocalDayWindow) rather than a raw pair of
    ints -- so 24:00 and a same-day midnight endpoint stay unambiguous, and
    an unsupported overnight window is rejected the same way Task 1's
    engine already rejects one (see to_local_day_window below).
    """

    start_minute: int = Field(ge=0, lt=MINUTES_PER_DAY)
    end_minute: int = Field(ge=0, le=MINUTES_PER_DAY)
    end_day_offset: int = 0

    @model_validator(mode="after")
    def _validate_structure(self) -> "DayWindowSpec":
        """
        Mirror LocalDayWindow's own date/timezone-independent structural
        checks here, so a malformed window (bad ordering, an unsupported
        end_day_offset) fails immediately at the configuration boundary --
        when this spec is constructed -- rather than being deferred until
        some later, possibly-never-reached call to to_local_day_window().
        The date/timezone-*dependent* checks (DST ambiguity, an offset
        transition inside the window) genuinely cannot be validated here
        without a real date+timezone, and remain in to_local_day_window.
        """
        if self.end_day_offset not in (0, 1):
            raise UnsupportedSchedulingWindowError(
                "a window ending more than one day after its date is not supported yet "
                f"(end_day_offset must be 0 or 1, got {self.end_day_offset})"
            )
        if self.end_day_offset == 0 and self.end_minute == MINUTES_PER_DAY:
            # Normalize, matching LocalDayWindow: a same-day 24:00 end means
            # the following midnight, identical to end_day_offset=1/end_minute=0.
            self.end_minute = 0
            self.end_day_offset = 1
        if self.end_day_offset == 0 and self.end_minute <= self.start_minute:
            raise ValueError(
                "end_minute must be after start_minute when end_day_offset is 0; "
                "use end_day_offset=1 with end_minute=0 (or end_minute=1440) to "
                "express a window ending at the following midnight"
            )
        if self.end_day_offset == 1 and self.end_minute != 0:
            raise UnsupportedSchedulingWindowError(
                "a window ending after the following midnight is not supported yet "
                "(end_day_offset=1 requires end_minute=0)"
            )
        return self

    def to_local_day_window(self, day: date_, tz_name: str) -> LocalDayWindow:
        """Resolve this spec against a real date/timezone. Raises the same
        UnsupportedSchedulingWindowError/AmbiguousLocalTimeError/ValueError
        as constructing a LocalDayWindow directly would -- this is a thin,
        validating passthrough, not a separate set of rules."""
        return LocalDayWindow(
            day=day,
            tz_name=tz_name,
            start_minute=self.start_minute,
            end_minute=self.end_minute,
            end_day_offset=self.end_day_offset,
        )


_WHOLE_DAY_WINDOW = DayWindowSpec(start_minute=0, end_minute=0, end_day_offset=1)


# -----------------------------------------------------------------------------
# Reward preferences (validated; the adapter below bridges these to
# app.reward.RewardSettings for the existing scorer)
# -----------------------------------------------------------------------------


def _is_finite(value: float, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite number, got {value!r}")
    return value


class RewardPreferences(BaseModel):
    """
    Fully resolved reward/scoring preferences for one day. Mirrors
    app.reward.RewardSettings' fields exactly (see
    day_preferences_to_reward_settings) -- including short_gap_bonus_*,
    Task 4's bounded ADHD-only gap-filling bonus (see
    app/reward.py's _short_gap_bonus_score). That bonus is only actually
    applied by calculate_task_score when its own adhd_mode=True is passed
    explicitly (the canonical day engine does this only in adhd_friendly
    mode), so these fields have no effect in precise_greedy regardless of
    their value here.
    """

    weight_importance: float = 5.0
    weight_time_bonus: float = 3.0
    weight_tag_relation: float = 2.0
    weight_fragmentation_penalty: float = -4.0
    weight_category_bonus: float = 1.0

    max_time_distance_minutes: int = 240
    same_tag_window_minutes: int = 120
    min_gap_between_tasks_minutes: int = 30

    # 0.0 (the default) means "disabled", including in adhd_friendly mode.
    short_gap_bonus_weight: float = 0.0
    short_gap_bonus_max_minutes: int = 20
    short_gap_bonus_cap: float = 10.0

    # Mirrors app.reward.RewardSettings.tag_relations exactly (see
    # _same_or_related_tag) -- keys/values are matched case-insensitively
    # there, so no normalization is required here.
    tag_relations: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator(
        "weight_importance",
        "weight_time_bonus",
        "weight_tag_relation",
        "weight_fragmentation_penalty",
        "weight_category_bonus",
        "short_gap_bonus_weight",
        "short_gap_bonus_cap",
    )
    @classmethod
    def _validate_finite(cls, value: float, info) -> float:
        return _is_finite(value, info.field_name)

    @model_validator(mode="after")
    def _validate_bounds(self) -> "RewardPreferences":
        if self.max_time_distance_minutes <= 0:
            raise ValueError(
                f"max_time_distance_minutes must be positive, got {self.max_time_distance_minutes!r}"
            )
        if self.same_tag_window_minutes < 0:
            raise ValueError(f"same_tag_window_minutes must be >= 0, got {self.same_tag_window_minutes!r}")
        if self.min_gap_between_tasks_minutes < 0:
            raise ValueError(
                f"min_gap_between_tasks_minutes must be >= 0, got {self.min_gap_between_tasks_minutes!r}"
            )
        if self.short_gap_bonus_max_minutes <= 0:
            raise ValueError(
                f"short_gap_bonus_max_minutes must be positive, got {self.short_gap_bonus_max_minutes!r}"
            )
        if self.short_gap_bonus_weight < 0:
            raise ValueError(
                f"short_gap_bonus_weight must be >= 0 (0 disables the bonus), got {self.short_gap_bonus_weight!r}"
            )
        if self.short_gap_bonus_cap < 0:
            raise ValueError(f"short_gap_bonus_cap must be >= 0, got {self.short_gap_bonus_cap!r}")
        return self


class RewardPreferencesOverride(BaseModel):
    """One layer's partial overrides for RewardPreferences: every field is
    None by default, meaning "this layer does not override this field."""

    weight_importance: float | None = None
    weight_time_bonus: float | None = None
    weight_tag_relation: float | None = None
    weight_fragmentation_penalty: float | None = None
    weight_category_bonus: float | None = None
    max_time_distance_minutes: int | None = None
    same_tag_window_minutes: int | None = None
    min_gap_between_tasks_minutes: int | None = None
    short_gap_bonus_weight: float | None = None
    short_gap_bonus_max_minutes: int | None = None
    short_gap_bonus_cap: float | None = None
    #: Present (even {}) replaces the whole dict from this layer up; absent
    #: (None, the default) inherits the lower layer's tag_relations
    #: unchanged -- the same two-state semantics as the other reward scalars
    #: here, not the three-state absent/value/None semantics used by the
    #: per-category dict fields on PreferenceOverrides.
    tag_relations: dict[str, list[str]] | None = None


# -----------------------------------------------------------------------------
# One override layer
# -----------------------------------------------------------------------------


class PreferenceOverrides(BaseModel):
    """
    One layer of partial preference overrides (YAML template, user-level,
    or date-specific). See the module docstring for the absent/value/None
    three-state semantics of the two dict fields.
    """

    day_window: DayWindowSpec | None = None
    category_multipliers: dict[str, float | None] = Field(default_factory=dict)
    category_preferred_windows: dict[str, LocalTimeWindow | None] = Field(default_factory=dict)
    optimizer_mode: OptimizerMode | None = None
    reward: RewardPreferencesOverride = Field(default_factory=RewardPreferencesOverride)

    @field_validator("category_multipliers")
    @classmethod
    def _validate_multipliers(cls, value: dict[str, float | None]) -> dict[str, float | None]:
        for category, multiplier in value.items():
            if multiplier is not None and not math.isfinite(multiplier):
                raise ValueError(f"category_multipliers[{category!r}] must be finite, got {multiplier!r}")
        return value


# -----------------------------------------------------------------------------
# Persisted override layers (Milestone 3)
# -----------------------------------------------------------------------------


class PreferenceScope(str, Enum):
    #: The "user" layer: applies to every date unless a date layer says otherwise.
    USER = "user"
    #: A date layer: applies to exactly one calendar date.
    DATE = "date"


class PreferenceRecord(BaseModel):
    """
    One persisted override layer (see PlanningService's preference methods).
    The YAML layer is never stored -- it is read from config/ -- so only the
    user layer and date layers are records. `overrides` keeps the exact
    absent/value/None semantics of PreferenceOverrides (the stored document
    round-trips losslessly; see overrides_to_document/overrides_from_document).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: uuid.UUID | None = None
    scope: PreferenceScope
    date: date_ | None = None
    overrides: PreferenceOverrides

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be aware datetimes (include a UTC offset)")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_scope(self) -> "PreferenceRecord":
        if (self.scope == PreferenceScope.DATE) != (self.date is not None):
            raise ValueError("a date-scoped preference record needs a date; a user-scoped one must not have one")
        return self


def overrides_to_document(overrides: PreferenceOverrides) -> str:
    """
    The stored JSON form of one layer, without optimizer_mode (a column of
    its own). A category key with an explicit None is kept as JSON null, and
    a reward/scalar field that is absent (None) stays null, so loading it
    back reproduces exactly the same layer.
    """
    return json.dumps(overrides.model_dump(mode="json", exclude={"optimizer_mode"}), sort_keys=True, allow_nan=False)


def overrides_from_document(document: str, optimizer_mode: str | None) -> PreferenceOverrides:
    data = json.loads(document)
    data["optimizer_mode"] = optimizer_mode
    return PreferenceOverrides.model_validate(data)


# -----------------------------------------------------------------------------
# Fully resolved DayPreferences
# -----------------------------------------------------------------------------


class DayPreferences(BaseModel):
    """
    Fully resolved scheduling preferences for one real calendar date.

    category_multipliers/category_preferred_windows here are the *fully
    merged* result of every layer -- a category simply absent from either
    dict means "no explicit preference at any layer"; callers read a
    multiplier through effective_category_multiplier (neutral 1.0
    fallback) and a preferred window via .get(category) (None = no
    preference, matching legacy calculate_task_score's own "no preferred
    window" case).
    """

    date: date_
    timezone: str
    day_window: DayWindowSpec = Field(default_factory=lambda: _WHOLE_DAY_WINDOW)
    category_multipliers: dict[str, float] = Field(default_factory=dict)
    category_preferred_windows: dict[str, LocalTimeWindow] = Field(default_factory=dict)
    optimizer_mode: OptimizerMode = OptimizerMode.PRECISE_GREEDY
    reward: RewardPreferences = Field(default_factory=RewardPreferences)

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        validate_timezone(value)
        return value

    def effective_category_multiplier(self, category: str) -> float:
        """A category with no explicit multiplier at any layer is neutral
        (1.0) -- this is the "keep unknown/imported categories valid with a
        neutral fallback" behavior, for the five primary categories and any
        other (e.g. legacy-imported) category name alike."""
        return self.category_multipliers.get(category, 1.0)

    def to_local_day_window(self) -> LocalDayWindow:
        return self.day_window.to_local_day_window(self.date, self.timezone)


# -----------------------------------------------------------------------------
# Layered resolution
# -----------------------------------------------------------------------------


def _merge_scalar_dict(lower: dict, override: dict) -> dict:
    """Merge one layer's dict override onto `lower`, per the module
    docstring's absent/value/None semantics. Always returns a new dict --
    `lower` is never mutated."""
    merged = dict(lower)
    for key, value in override.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _merge_reward(lower: RewardPreferences, override: RewardPreferencesOverride) -> RewardPreferences:
    data = lower.model_dump()
    for field_name in RewardPreferencesOverride.model_fields:
        value = getattr(override, field_name)
        if value is not None:
            data[field_name] = value
    return RewardPreferences(**data)


def resolve_day_preferences(
    *,
    date: date_,
    timezone: str,
    yaml_overrides: PreferenceOverrides | None = None,
    user_overrides: PreferenceOverrides | None = None,
    date_overrides: PreferenceOverrides | None = None,
) -> DayPreferences:
    """
    Resolve one day's effective preferences by layering, in increasing
    precedence: built-in defaults -> yaml_overrides (the project's YAML
    reward-config template; see day_preferences_overrides_from_reward_settings
    to build this from an already-loaded RewardSettings) -> user_overrides
    (optional user-level overrides, persisted by PlanningService) -> date_overrides
    (overrides specific to exactly this `date`).

    Every call returns an entirely new, independent DayPreferences -- no
    input layer, module-level default, or previously resolved
    DayPreferences is mutated by this call or by editing its result
    afterward.
    """
    validate_timezone(timezone)

    day_window = _WHOLE_DAY_WINDOW
    category_multipliers: dict[str, float] = {}
    category_preferred_windows: dict[str, LocalTimeWindow] = {}
    optimizer_mode = OptimizerMode.PRECISE_GREEDY
    reward = RewardPreferences()

    for layer in (yaml_overrides, user_overrides, date_overrides):
        if layer is None:
            continue
        if layer.day_window is not None:
            day_window = layer.day_window
        category_multipliers = _merge_scalar_dict(category_multipliers, layer.category_multipliers)
        category_preferred_windows = _merge_scalar_dict(
            category_preferred_windows, layer.category_preferred_windows
        )
        if layer.optimizer_mode is not None:
            optimizer_mode = layer.optimizer_mode
        reward = _merge_reward(reward, layer.reward)

    return DayPreferences(
        date=date,
        timezone=timezone,
        day_window=day_window,
        category_multipliers=category_multipliers,
        category_preferred_windows=category_preferred_windows,
        optimizer_mode=optimizer_mode,
        reward=reward,
    )


def day_preferences_overrides_from_reward_settings(settings: RewardSettings) -> PreferenceOverrides:
    """
    Build a PreferenceOverrides layer from an already-loaded
    app.reward.RewardSettings (typically via app.reward.load_reward_settings()),
    for use as resolve_day_preferences's yaml_overrides layer.

    category_weights/category_time_windows become this layer's
    category_multipliers/category_preferred_windows -- exactly the
    categories/windows explicitly present in the source YAML (RewardSettings
    never injects the five primary categories itself; the checked-in
    template does, by listing them explicitly with neutral 1.0 defaults --
    see config/task_preference.yaml).
    """
    return PreferenceOverrides(
        category_multipliers=dict(settings.category_weights),
        category_preferred_windows={
            category: LocalTimeWindow(start_minute=window["start_time"], end_minute=window["end_time"])
            for category, window in settings.category_time_windows.items()
        },
        reward=RewardPreferencesOverride(
            weight_importance=settings.weight_importance,
            weight_time_bonus=settings.weight_time_bonus,
            weight_tag_relation=settings.weight_tag_relation,
            weight_fragmentation_penalty=settings.weight_fragmentation_penalty,
            weight_category_bonus=settings.weight_category_bonus,
            max_time_distance_minutes=settings.max_time_distance_minutes,
            same_tag_window_minutes=settings.same_tag_window_minutes,
            min_gap_between_tasks_minutes=settings.min_gap_between_tasks_minutes,
            short_gap_bonus_weight=settings.short_gap_bonus_weight,
            short_gap_bonus_max_minutes=settings.short_gap_bonus_max_minutes,
            short_gap_bonus_cap=settings.short_gap_bonus_cap,
            tag_relations={tag: list(related) for tag, related in settings.tag_relations.items()},
        ),
    )


# -----------------------------------------------------------------------------
# Adapter: DayPreferences -> legacy app.reward.RewardSettings, and a
# canonical-Task -> legacy-scoring-shape helper, so the existing
# (Milestone-0-frozen) calculate_task_score/calculate_schedule_score can be
# exercised against canonical per-day preferences today, without changing
# app/optimizer.py (that wiring is Task 4's job).
# -----------------------------------------------------------------------------


def day_preferences_to_reward_settings(day_preferences: DayPreferences) -> RewardSettings:
    """
    Adapt resolved DayPreferences into a legacy RewardSettings instance.

    This is intentionally a one-way, read-only bridge: it builds and
    returns a brand-new RewardSettings on every call and never mutates
    app.reward or config.settings globals.

    category_weights is populated directly from
    day_preferences.category_multipliers (already fully merged/resolved);
    a category absent from it simply relies on RewardSettings.category_weights'
    own .get(category, 1.0) neutral fallback inside _priority_score, exactly
    matching legacy behavior. task_weights/task_time_windows are
    intentionally left empty here -- those are legacy per-task-name YAML
    overrides with no canonical equivalent in this milestone; a canonical
    task's own preferred window is resolved through
    effective_task_preferred_window below instead, not through this
    adapter's category_time_windows.

    The formula calculate_task_score already implements --
    priority * weight_importance * category_multiplier * task_multiplier --
    is unchanged: a category multiplier of 1.0 here leaves the score
    identical to task_multiplier=1.0/category_multiplier=1.0, i.e.
    unaffected, exactly like today. weight_category_bonus continues to be
    carried through (kept for compatibility) without being read by
    _priority_score -- this adapter does not change that known gap.
    """
    reward = day_preferences.reward
    return RewardSettings(
        weight_importance=reward.weight_importance,
        weight_time_bonus=reward.weight_time_bonus,
        weight_tag_relation=reward.weight_tag_relation,
        weight_fragmentation_penalty=reward.weight_fragmentation_penalty,
        weight_category_bonus=reward.weight_category_bonus,
        max_time_distance_minutes=reward.max_time_distance_minutes,
        same_tag_window_minutes=reward.same_tag_window_minutes,
        min_gap_between_tasks_minutes=reward.min_gap_between_tasks_minutes,
        category_weights=dict(day_preferences.category_multipliers),
        short_gap_bonus_weight=reward.short_gap_bonus_weight,
        short_gap_bonus_max_minutes=reward.short_gap_bonus_max_minutes,
        short_gap_bonus_cap=reward.short_gap_bonus_cap,
        tag_relations={tag: list(related) for tag, related in reward.tag_relations.items()},
    )


def effective_task_preferred_window(
    task,
    day_preferences: DayPreferences,
    *,
    task_override: LocalTimeWindow | None = None,
) -> dict[str, int] | None:
    """
    Resolve which preferred time window applies to a canonical Task
    (app.planning.models.Task) on a day with `day_preferences`, per
    precedence: explicit task_override (an explicit, request-scoped
    override) > the task's own preferred_time_window > the day's effective
    category preference for task.category > none.

    This intentionally *reverses* legacy calculate_task_score's own
    precedence (app/reward.py's _preferred_window_for_task, which favors a
    YAML category/task-name override over the task's own preference_time)
    -- that legacy function and its precedence are left completely
    unchanged for existing legacy Task consumers; this is a new, separate
    code path used only when scoring a canonical Task through
    to_legacy_scoring_task below.
    """
    if task_override is not None:
        return {"start_time": task_override.start_minute, "end_time": task_override.end_minute}
    if task.preferred_time_window is not None:
        return {
            "start_time": task.preferred_time_window.start_minute,
            "end_time": task.preferred_time_window.end_minute,
        }
    category_window = day_preferences.category_preferred_windows.get(task.category)
    if category_window is not None:
        return {"start_time": category_window.start_minute, "end_time": category_window.end_minute}
    return None


def to_legacy_scoring_task(
    task,
    day_preferences: DayPreferences,
    *,
    task_override: LocalTimeWindow | None = None,
) -> dict:
    """
    Build the dict shape app.reward.calculate_task_score expects (via its
    duck-typed _get helper) from a canonical Task, with its preferred
    window resolved through effective_task_preferred_window.

    This does not resolve a placement/start time -- callers pass
    start_time to calculate_task_score separately, exactly as today; this
    only bridges *task* fields (name/category/tag/duration/priority/
    preference window), not scheduling.
    """
    return {
        "name": task.name,
        "category": task.category,
        "tag": task.tags[0] if task.tags else "",
        "duration": task.estimated_duration_minutes,
        "priority": task.priority,
        "fixed": False,
        "preference_time": effective_task_preferred_window(task, day_preferences, task_override=task_override),
    }
