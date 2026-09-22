"""
app/ui/planning_controller.py

The shared, testable state/controller boundary for canonical week/month/day
planning (Task 6 / Schedule Maxing v2). Deliberately Tk-free -- every method
here is plain Python, exercised by tests/ui/test_planning_controller.py
without a display -- so app/app.py's eventual widget wiring has a single,
already-tested source of truth for task identities, preferences,
allocation, and generated results, instead of reimplementing any of
app/planning/'s logic in a callback. Domain decisions (scheduling,
allocation, scoring) stay in app/planning/ and app/optimizer.py; this class
only holds state and delegates.

PlanningController holds:
    - the canonical task registry, shared across Day/Week/Month views, so
      editing a task anywhere is immediately visible everywhere else;
    - fixed blocks per date;
    - a layered preference stack per app.planning.preferences (YAML
      template -> one in-memory "user" override layer -> per-date override
      layers), resolved on demand -- never a per-day YAML file (see
      resolve_day_preferences's own "independent data" guarantee: editing
      one date's override here can never mutate another date's, or the
      YAML layer, or a DayPreferences already handed to a caller);
    - the current AllocationResult (or None before any Week/Month
      allocation has run) and a SelectedDayState per date, invalidated via
      app.planning.service.mark_stale_if_outdated whenever a new
      allocation run supersedes the one an existing generated day came
      from, or whenever a task/fixed-block/preference edit could affect an
      already-generated day (see _invalidate_generated_results).

Every mutating/possibly-failing method returns a ControllerResult, matching
ExecutionController/ProductivityController's convention, so a Tk callback
can always branch on `.ok`/`.error` instead of risking an uncaught
exception reaching Tk's event loop.
"""

from __future__ import annotations

import uuid
from datetime import date as date_
from datetime import timedelta

from app.optimizer import MandatoryTaskSchedulingError
from app.planning.allocation import AllocationResult, allocate_tasks, month_dates, week_dates
from app.planning.models import DayScheduleOutput, FixedBlock, Task, TaskRegistry
from app.planning.preferences import (
    DayPreferences,
    PreferenceOverrides,
    day_preferences_overrides_from_reward_settings,
    resolve_day_preferences,
)
from app.planning.service import DayResultStatus, SelectedDayState, generate_selected_day, initial_state, mark_stale_if_outdated
from app.reward import load_reward_settings
from app.ui.background import ControllerResult


class PlanningController:
    def __init__(self, *, timezone: str = "UTC", project_root: str | None = None) -> None:
        self._timezone = timezone
        self._tasks = TaskRegistry()
        self._fixed_blocks_by_date: dict[date_, list[FixedBlock]] = {}
        self._user_overrides: PreferenceOverrides | None = None
        self._date_overrides: dict[date_, PreferenceOverrides] = {}
        # Loaded once at construction, not reloaded from disk on every
        # resolution -- see app/optimizer.py's own "avoid reloading YAML"
        # performance note. Call refresh_yaml_layer() to pick up an edited
        # config/task_preference.yaml without recreating the controller.
        self._yaml_overrides = day_preferences_overrides_from_reward_settings(load_reward_settings(project_root=project_root))
        self._allocation: AllocationResult | None = None
        self._day_states: dict[date_, SelectedDayState] = {}

    # ------------------------------------------------------------------
    # Tasks (shared identity across Day/Week/Month views)
    # ------------------------------------------------------------------

    def add_or_update_task(self, task: Task) -> ControllerResult[Task]:
        def op() -> Task:
            self._tasks.add(task)
            self._invalidate_generated_results()
            return task

        return self._call(op)

    def remove_task(self, task_id: uuid.UUID) -> ControllerResult[None]:
        def op() -> None:
            self._tasks.tasks.pop(task_id, None)
            self._invalidate_generated_results()

        return self._call(op)

    def get_task(self, task_id: uuid.UUID) -> ControllerResult[Task | None]:
        return self._call(lambda: self._tasks.get(task_id))

    def list_tasks(self) -> ControllerResult[list[Task]]:
        return self._call(lambda: list(self._tasks.tasks.values()))

    # ------------------------------------------------------------------
    # Fixed blocks
    # ------------------------------------------------------------------

    def set_fixed_blocks(self, day: date_, blocks: list[FixedBlock]) -> ControllerResult[None]:
        def op() -> None:
            self._fixed_blocks_by_date[day] = list(blocks)
            self._invalidate_generated_results()

        return self._call(op)

    def get_fixed_blocks(self, day: date_) -> ControllerResult[list[FixedBlock]]:
        return self._call(lambda: list(self._fixed_blocks_by_date.get(day, [])))

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def refresh_yaml_layer(self, *, project_root: str | None = None) -> ControllerResult[None]:
        def op() -> None:
            self._yaml_overrides = day_preferences_overrides_from_reward_settings(
                load_reward_settings(project_root=project_root)
            )
            self._invalidate_generated_results()

        return self._call(op)

    def set_user_overrides(self, overrides: PreferenceOverrides | None) -> ControllerResult[None]:
        """Session-scoped overrides applied to every date unless a date-specific override says otherwise."""

        def op() -> None:
            self._user_overrides = overrides
            self._invalidate_generated_results()

        return self._call(op)

    def set_date_overrides(self, day: date_, overrides: PreferenceOverrides | None) -> ControllerResult[None]:
        def op() -> None:
            if overrides is None:
                self._date_overrides.pop(day, None)
            else:
                self._date_overrides[day] = overrides
            self._invalidate_generated_results()

        return self._call(op)

    def resolve_preferences(self, day: date_) -> ControllerResult[DayPreferences]:
        return self._call(lambda: self._resolve(day))

    def _resolve(self, day: date_) -> DayPreferences:
        return resolve_day_preferences(
            date=day, timezone=self._timezone,
            yaml_overrides=self._yaml_overrides,
            user_overrides=self._user_overrides,
            date_overrides=self._date_overrides.get(day),
        )

    # ------------------------------------------------------------------
    # Allocation (Week / Month) -- never calls the Day Scheduler
    # ------------------------------------------------------------------

    def allocate_range(self, start_date: date_, end_date: date_) -> ControllerResult[AllocationResult]:
        def op() -> AllocationResult:
            dates = [start_date]
            current = start_date
            while current < end_date:
                current = current + timedelta(days=1)
                dates.append(current)

            preferences_by_date = {day: self._resolve(day) for day in dates}
            result = allocate_tasks(
                start_date=start_date, end_date=end_date, tasks=self._tasks,
                task_ids=list(self._tasks.tasks.keys()), preferences_by_date=preferences_by_date,
                fixed_blocks_by_date=self._fixed_blocks_by_date,
            )
            self._allocation = result
            self._day_states = {
                day: mark_stale_if_outdated(state, result.id) for day, state in self._day_states.items()
            }
            return result

        return self._call(op)

    def allocate_week(self, start_date: date_) -> ControllerResult[AllocationResult]:
        dates = week_dates(start_date)
        return self.allocate_range(dates[0], dates[-1])

    def allocate_month(self, year: int, month: int) -> ControllerResult[AllocationResult]:
        dates = month_dates(year, month)
        return self.allocate_range(dates[0], dates[-1])

    def current_allocation(self) -> ControllerResult[AllocationResult | None]:
        return self._call(lambda: self._allocation)

    # ------------------------------------------------------------------
    # Selected-day generation -- calls the Day Scheduler exactly once
    # ------------------------------------------------------------------

    def generate_day(self, selected_date: date_) -> ControllerResult[DayScheduleOutput]:
        def op() -> DayScheduleOutput:
            if self._allocation is None:
                raise RuntimeError("Allocate a week/month (allocate_week/allocate_month) before generating a day.")

            preferences_by_date = {selected_date: self._resolve(selected_date)}
            previous_state = self._day_states.get(selected_date)
            previous_result = (
                previous_state.result
                if previous_state is not None and previous_state.status != DayResultStatus.ALLOCATED
                else None
            )

            result, state = generate_selected_day(
                self._allocation, selected_date, self._tasks, preferences_by_date, self._fixed_blocks_by_date,
                previous_result=previous_result,
            )
            self._day_states[selected_date] = state
            return result

        try:
            return ControllerResult.success(op())
        except MandatoryTaskSchedulingError as error:
            # A structured, expected outcome (not every day generates
            # successfully) -- surfaced through the same ControllerResult
            # channel as any other failure, with the per-task reasons
            # preserved in the message rather than swallowed.
            reasons = "; ".join(
                f"{failure.task_id} [{failure.reason_code.value}]: {failure.explanation}" for failure in error.failures
            )
            return ControllerResult.failure(f"Could not generate {selected_date}: {reasons}")
        except Exception as error:  # noqa: BLE001 - last-resort safety net
            return ControllerResult.failure(f"Unexpected error: {error}")

    def day_state(self, day: date_) -> ControllerResult[SelectedDayState]:
        return self._call(lambda: self._day_states.get(day, initial_state(day)))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _invalidate_generated_results(self) -> None:
        """
        A task/fixed-block/preference edit can affect any already-generated
        day, so every GENERATED state is marked STALE (kept, only
        relabeled) rather than silently left looking current. This is
        intentionally coarse -- like app.planning.service's own
        allocation-id-based invalidation, it can only over-invalidate, never
        under-invalidate. The current allocation itself is also cleared:
        Week/Month must be explicitly re-allocated after an edit, never
        silently re-run.
        """
        self._allocation = None
        self._day_states = {
            day: (
                SelectedDayState(
                    date=state.date, status=DayResultStatus.STALE, result=state.result,
                    generated_from_allocation_id=state.generated_from_allocation_id,
                )
                if state.status == DayResultStatus.GENERATED
                else state
            )
            for day, state in self._day_states.items()
        }

    def _call(self, operation):
        try:
            return ControllerResult.success(operation())
        except Exception as error:  # noqa: BLE001 - last-resort safety net
            return ControllerResult.failure(f"Unexpected error: {error}")
