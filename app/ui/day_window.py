"""
app/ui/day_window.py

The Day Window of one real calendar date, Tk-free (tests/ui/test_day_window.py):
the start and end of the usable day, shown and changed above the Day, Week
and Month schedules (app/ui/day_window_bar.py).

Where it lives -- no second storage:

- The DEFAULT window is the user preference layer's `day_window` (Settings,
  "Default scheduling preferences"), falling back to the app template and
  then the whole day.
- A date's OVERRIDE is that date's own preference layer's `day_window`
  (PreferenceScope.DATE, app/planning/preferences.py), saved through
  PlanningController.update_date_overrides -- the same per-user record that
  is stored locally, synchronized to the server's PostgreSQL and written
  directly with direct PostgreSQL storage. Only that one field is changed;
  the date's engine and other overrides are kept, and a layer left empty is
  deleted so the date simply inherits again.
- The day engine resolves every date's preferences with those layers, so a
  run for the date schedules flexible work only inside its effective window.

Changing the default in Settings moves every date without an override; a
date with an override keeps it. Saving a window is refused -- with nothing
written -- when the end is not after the start, when a time does not exist
(daylight saving), or when one of the date's fixed blocks would lie outside
it (the day engine cannot schedule around a block outside the day). Saved
work outside a narrower window makes the date's schedule out of date; Make
Schedule then reports it instead of silently moving anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_

from app.planning.application import RangeScope
from app.planning.preferences import DayWindowSpec, PreferenceOverrides, resolve_day_preferences
from app.planning.time import local_minutes
from app.ui.background import ControllerResult
from app.ui.planning_controller import PlanningController
from app.ui.time_fields import MINUTES_PER_DAY, FieldError, format_clock, parse_clock


def window_minutes(spec: DayWindowSpec) -> tuple[int, int]:
    """A window as (start, end) minutes from local midnight; an end at the following midnight is 1440."""
    return spec.start_minute, MINUTES_PER_DAY if spec.end_day_offset == 1 else spec.end_minute


def window_text(start: int, end: int) -> str:
    return f"{format_clock(start)} – {format_clock(end)}"


def _block_text(block, day: date_) -> str:
    start = local_minutes(block.planned_start, day, block.timezone)
    end = local_minutes(block.planned_end, day, block.timezone) or MINUTES_PER_DAY
    return f"“{block.label}” ({window_text(start, end)})"


class DayWindowError(ValueError):
    """A window that cannot be saved for the date; the message says why and what to do."""


@dataclass(frozen=True)
class DayWindowState:
    day: date_
    timezone: str
    #: The date's effective window (what scheduling uses), in minutes from local midnight (end may be 1440).
    start_minute: int
    end_minute: int
    #: The window the date inherits without an override of its own (the Settings default).
    default_start: int
    default_end: int
    #: True when the date has its own window.
    overridden: bool
    #: The date layer's version (the precondition for changing it); None: the date has no layer.
    version: int | None

    @property
    def text(self) -> str:
        return window_text(self.start_minute, self.end_minute)

    @property
    def default_text(self) -> str:
        return window_text(self.default_start, self.default_end)


def parse_window(start_text: str, end_text: str) -> DayWindowSpec:
    """Typed start/end times as a window (end 12:00 AM = the next midnight); DayWindowError if invalid."""
    try:
        start = parse_clock(start_text)
    except FieldError as error:
        raise DayWindowError(f"Start: {error}") from None
    try:
        end = parse_clock(end_text, end_of_interval=True)
    except FieldError as error:
        raise DayWindowError(f"End: {error}") from None
    if end <= start:
        raise DayWindowError("The day must end after it starts (a day window cannot run past midnight; "
                             "use 12:00 AM as the end for midnight).")
    return DayWindowSpec(start_minute=start, end_minute=end)


class DayWindowController:
    """Reads and changes one date's Day Window through PlanningController (see the module docstring)."""

    def __init__(self, planning: PlanningController) -> None:
        self._planning = planning

    def state(self, day: date_) -> ControllerResult[DayWindowState]:
        views = self._planning.preference_views(day, day)
        if not views.ok:
            return ControllerResult.failure(views.error, views.cause)
        view = views.value.days[day]
        start, end = window_minutes(view.effective.day_window)
        default_start, default_end = window_minutes(view.inherited.day_window)
        layer = view.date_layer
        return ControllerResult.success(DayWindowState(
            day=day, timezone=views.value.timezone_name, start_minute=start, end_minute=end,
            default_start=default_start, default_end=default_end,
            overridden=layer is not None and layer.overrides.day_window is not None,
            version=layer.version if layer is not None else None,
        ))

    def save(self, day: date_, start_text: str, end_text: str, *,
             expected_version: int | None) -> ControllerResult[DayWindowState]:
        """Give `day` its own window (only the day_window field of its layer changes)."""
        try:
            spec = parse_window(start_text, end_text)
        except DayWindowError as error:
            return ControllerResult.failure(str(error), error)
        return self._change(day, spec, expected_version)

    def use_default(self, day: date_, *, expected_version: int | None) -> ControllerResult[DayWindowState]:
        """Remove `day`'s own window, so it follows the Settings default again."""
        return self._change(day, None, expected_version)

    def _change(self, day: date_, spec: DayWindowSpec | None,
                expected_version: int | None) -> ControllerResult[DayWindowState]:
        views = self._planning.preference_views(day, day)
        if not views.ok:
            return ControllerResult.failure(views.error, views.cause)
        view = views.value.days[day]
        stored = view.date_layer.overrides if view.date_layer is not None else PreferenceOverrides()
        updated = stored.model_copy(update={"day_window": spec})
        try:
            # Resolve exactly as scheduling will: a window whose ends do not exist on this date is refused here.
            preferences = resolve_day_preferences(
                date=day, timezone=views.value.timezone_name, yaml_overrides=views.value.template,
                user_overrides=views.value.user_layer.overrides if views.value.user_layer is not None else None,
                date_overrides=updated,
            )
            window_start, window_end = preferences.to_local_day_window().to_utc_instants()
        except ValueError as error:
            message = f"That window cannot be used on {day:%b} {day.day}: {error}"
            return ControllerResult.failure(message, DayWindowError(message))
        blocks = self._planning.load_range(day, day, scope=RangeScope.PLANNED)
        if not blocks.ok:
            return ControllerResult.failure(blocks.error, blocks.cause)
        outside = [block for block in blocks.value.fixed_blocks_by_date.get(day, [])
                   if block.planned_start < window_start or block.planned_end > window_end]
        if outside:
            names = ", ".join(_block_text(block, day) for block in outside)
            start, end = window_minutes(preferences.day_window)
            message = (f"{window_text(start, end)} would leave fixed block(s) outside the day: {names}. "
                       "Choose a window that contains them, or move or remove those blocks first. Nothing was saved.")
            return ControllerResult.failure(message, DayWindowError(message))
        result = self._planning.update_date_overrides(
            day, lambda overrides: overrides.model_copy(update={"day_window": spec}), expected_version=expected_version)
        if not result.ok:
            return ControllerResult.failure(result.error, result.cause)
        return self.state(day)
