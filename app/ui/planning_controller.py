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

Authoritative data (Milestone 2): tasks, fixed blocks, and generated
placements are read from and written to the persistence-backed
app.planning.application.PlanningService -- the controller keeps no copy
of them. Every task/block/placement it returns is a fresh snapshot loaded
from the database, so mutating a returned model never changes saved state;
call add_or_update_task/set_fixed_blocks to save an edit. If no service is
injected, the controller opens a private in-memory SQLite database (the
previous in-memory behavior, now through the same code path); pass
`service=` to persist to a real database file.

PlanningController still holds, in memory only (temporary render state,
never stored):
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
      from, or whenever a successful task/fixed-block/preference edit could
      affect an already-generated day (see _invalidate_generated_results).
      A failed write changes nothing and invalidates nothing.

generate_day saves the generated placements for that one date through
PlanningService.replace_placements (only after generation succeeds; if the
save fails, the day's state is left as it was). The previous result used
for placement-id reuse is the stored placements of that date, so an
unchanged placement keeps its id -- and any execution history linked to
it -- across regenerations and restarts. schedule_range (the desktop
pages' "Make Schedule") allocates a range and then runs selected-day
generation once per date of it -- each call still optimizes exactly one
date -- and saves every date's placements in one transaction: either the
whole range's new schedule is committed, or none of it is.

Restart handling of derived state: allocation and SelectedDayStates are
not persisted. A date that has saved placements but no in-memory state
(e.g. after reopening the app) is reported by day_state as STALE with the
saved placements as its result -- never as GENERATED/current, because the
edits made since it was generated are unknown -- and the saved placements
are kept, not deleted. Generating the date again makes it GENERATED.

Every mutating/possibly-failing method returns a ControllerResult, matching
ExecutionController/ProductivityController's convention, so a Tk callback
can always branch on `.ok`/`.error` instead of risking an uncaught
exception reaching Tk's event loop. Methods are safe to call from a
background thread (app.ui.background.run_in_background): controller state
is guarded by a lock, and database access by the connection's own lock.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import date as date_
from datetime import timedelta

from app.execution.db import get_connection
from app.optimizer import MandatoryTaskSchedulingError
from app.planning.allocation import AllocationResult, allocate_tasks, month_dates, week_dates
from app.planning.application import (
    ImportApplyResult,
    PlacementReplacement,
    PlanningRange,
    PlanningService,
    RangeClearResult,
    RangeScope,
)
from app.planning.csv_export import PlanningExportResult, export_planning_csv
from app.planning.csv_import import ImportMode, ParsedImport, parse_legacy_csv_file
from app.planning.errors import PlanningError
from app.planning.models import DayScheduleOutput, FixedBlock, ScheduledTask, Task, TaskRegistry
from app.planning.preferences import (
    DayPreferences,
    PreferenceOverrides,
    day_preferences_overrides_from_reward_settings,
    resolve_day_preferences,
)
from app.planning.repository import PlanningRepository
from app.planning.service import DayResultStatus, SelectedDayState, generate_selected_day, initial_state, mark_stale_if_outdated
from app.reward import load_reward_settings
from app.ui.background import ControllerResult


@dataclass(frozen=True)
class RangeScheduleResult:
    """One committed Make Schedule run over a date range."""

    allocation: AllocationResult
    outputs: dict[date_, DayScheduleOutput]
    replacement: PlacementReplacement


def _scheduling_failure_message(selected_date: date_, error: MandatoryTaskSchedulingError) -> str:
    reasons = "; ".join(
        f"{failure.task_id} [{failure.reason_code.value}]: {failure.explanation}" for failure in error.failures
    )
    return f"Could not generate {selected_date}: {reasons}"


class PlanningController:
    def __init__(
        self,
        *,
        service: PlanningService | None = None,
        timezone: str = "UTC",
        project_root: str | None = None,
    ) -> None:
        self._owned_connection = None
        if service is None:
            self._owned_connection = get_connection(":memory:")
            service = PlanningService(PlanningRepository(self._owned_connection))
        self._service = service
        self._lock = threading.RLock()

        self._timezone = timezone
        self._user_overrides: PreferenceOverrides | None = None
        self._date_overrides: dict[date_, PreferenceOverrides] = {}
        # Loaded once at construction, not reloaded from disk on every
        # resolution -- see app/optimizer.py's own "avoid reloading YAML"
        # performance note. Call refresh_yaml_layer() to pick up an edited
        # config/task_preference.yaml without recreating the controller.
        self._yaml_overrides = day_preferences_overrides_from_reward_settings(load_reward_settings(project_root=project_root))
        self._allocation: AllocationResult | None = None
        self._allocation_scope = RangeScope.ELIGIBLE
        self._day_states: dict[date_, SelectedDayState] = {}

    def close(self) -> None:
        """Close the private in-memory database, if this controller opened one."""
        if self._owned_connection is not None:
            self._owned_connection.close()
            self._owned_connection = None

    # ------------------------------------------------------------------
    # Tasks (shared identity across Day/Week/Month views)
    # ------------------------------------------------------------------

    def add_or_update_task(self, task: Task) -> ControllerResult[Task]:
        """Save one task; the value is the stored snapshot (with any version bump)."""

        def op() -> Task:
            saved = self._service.save_task(task)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def add_or_update_tasks(self, tasks: list[Task]) -> ControllerResult[list[Task]]:
        """Save several tasks atomically (all or none)."""

        def op() -> list[Task]:
            saved = self._service.save_tasks(tasks)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def remove_task(self, task_id: uuid.UUID) -> ControllerResult[None]:
        def op() -> None:
            if self._service.delete_task(task_id):
                self._invalidate_generated_results()

        return self._call(op)

    def get_task(self, task_id: uuid.UUID) -> ControllerResult[Task | None]:
        return self._call(lambda: self._service.get_task(task_id))

    def get_tasks(self, task_ids: list[uuid.UUID]) -> ControllerResult[TaskRegistry]:
        return self._call(lambda: self._service.get_tasks(task_ids))

    def list_tasks(self) -> ControllerResult[list[Task]]:
        return self._call(self._service.list_tasks)

    # ------------------------------------------------------------------
    # Fixed blocks
    # ------------------------------------------------------------------

    def set_fixed_blocks(self, day: date_, blocks: list[FixedBlock]) -> ControllerResult[None]:
        def op() -> None:
            self._service.set_fixed_blocks_for_date(day, blocks)
            self._invalidate_generated_results()

        return self._call(op)

    def get_fixed_blocks(self, day: date_) -> ControllerResult[list[FixedBlock]]:
        return self._call(lambda: self._service.fixed_blocks_for_date(day))

    def save_fixed_block(self, block: FixedBlock) -> ControllerResult[FixedBlock]:
        """Create or edit one fixed block (its date may change)."""

        def op() -> FixedBlock:
            saved = self._service.save_fixed_block(block)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def delete_fixed_block(self, block_id: uuid.UUID) -> ControllerResult[bool]:
        def op() -> bool:
            deleted = self._service.delete_fixed_block(block_id)
            if deleted:
                self._invalidate_generated_results()
            return deleted

        return self._call(op)

    # ------------------------------------------------------------------
    # Stored planning data
    # ------------------------------------------------------------------

    def load_range(
        self, start_date: date_, end_date: date_, *, scope: RangeScope = RangeScope.ELIGIBLE
    ) -> ControllerResult[PlanningRange]:
        """The range's tasks (per `scope`), fixed blocks, and stored placements (a snapshot)."""
        return self._call(lambda: self._service.load_range(start_date, end_date, scope=scope))

    def apply_import(self, parsed: ParsedImport, mode: ImportMode) -> ControllerResult[ImportApplyResult]:
        """Write a validated CSV import in one transaction (see app/planning/csv_import.py for the modes)."""

        def op() -> ImportApplyResult:
            replace_range = (parsed.start_date, parsed.end_date) if mode == ImportMode.REPLACE else None
            result = self._service.apply_import(parsed.tasks, parsed.fixed_blocks, replace_range=replace_range)
            self._invalidate_generated_results()
            if replace_range is not None:
                for day in list(self._day_states):
                    if parsed.start_date <= day <= parsed.end_date:
                        del self._day_states[day]
            return result

        return self._call(op)

    def import_csv_file(
        self, path: str, *, anchor_date: date_, mode: ImportMode
    ) -> ControllerResult[ImportApplyResult]:
        """Parse and validate the whole file (day 1 == anchor_date, this controller's timezone), then apply it."""
        try:
            parsed = parse_legacy_csv_file(path, anchor_date=anchor_date, timezone=self._timezone)
        except PlanningError as error:
            return ControllerResult.failure(str(error))
        return self.apply_import(parsed, mode)

    def export_planning_csv(
        self, path: str, *, start_date: date_ | None = None, end_date: date_ | None = None
    ) -> ControllerResult[PlanningExportResult]:
        """Write the stored-planning CSV (read-only; see app/planning/csv_export.py)."""
        return self._call(lambda: export_planning_csv(self._service, path, start_date=start_date, end_date=end_date))

    def clear_range(
        self, start_date: date_, end_date: date_, *, include_planning_data: bool
    ) -> ControllerResult[RangeClearResult]:
        """Reset scope (see PlanningService.clear_range): never deletes execution history."""

        def op() -> RangeClearResult:
            result = self._service.clear_range(start_date, end_date, include_planning_data=include_planning_data)
            self._invalidate_generated_results()
            for day in list(self._day_states):
                if start_date <= day <= end_date:
                    del self._day_states[day]
            return result

        return self._call(op)

    def get_placements(self, day: date_) -> ControllerResult[list[ScheduledTask]]:
        """The stored (last generated and saved) placements for one date."""
        return self._call(lambda: self._service.placements_for_date(day))

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

    def allocate_range(
        self, start_date: date_, end_date: date_, *, scope: RangeScope = RangeScope.ELIGIBLE
    ) -> ControllerResult[AllocationResult]:
        def op() -> AllocationResult:
            result = self._compute_allocation(start_date, end_date, scope)
            self._adopt_allocation(result, scope)
            return result

        return self._call(op)

    def _compute_allocation(self, start_date: date_, end_date: date_, scope: RangeScope) -> AllocationResult:
        planning_range = self._service.load_range(start_date, end_date, scope=scope)
        dates = [start_date]
        current = start_date
        while current < end_date:
            current = current + timedelta(days=1)
            dates.append(current)

        preferences_by_date = {day: self._resolve(day) for day in dates}
        result = allocate_tasks(
            start_date=start_date, end_date=end_date, tasks=planning_range.tasks,
            task_ids=planning_range.task_ids, preferences_by_date=preferences_by_date,
            fixed_blocks_by_date=planning_range.fixed_blocks_by_date,
        )
        return result

    def _adopt_allocation(self, result: AllocationResult, scope: RangeScope) -> None:
        self._allocation = result
        self._allocation_scope = scope
        self._day_states = {
            day: mark_stale_if_outdated(state, result.id) for day, state in self._day_states.items()
        }
        return result

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

            allocation = self._allocation
            tasks = self._service.load_range(
                allocation.start_date, allocation.end_date, scope=self._allocation_scope
            ).tasks
            result, state = self._generate(allocation, selected_date, tasks)
            # Save first; only a successfully saved result becomes the day's state.
            self._service.replace_placements(selected_date, selected_date, result.placements)
            self._day_states[selected_date] = state
            return result

        try:
            with self._lock:
                return ControllerResult.success(op())
        except MandatoryTaskSchedulingError as error:
            # A structured, expected outcome (not every day generates
            # successfully) -- surfaced through the same ControllerResult
            # channel as any other failure, with the per-task reasons
            # preserved in the message rather than swallowed.
            return ControllerResult.failure(_scheduling_failure_message(selected_date, error))
        except PlanningError as error:
            return ControllerResult.failure(str(error))
        except Exception as error:  # noqa: BLE001 - last-resort safety net
            return ControllerResult.failure(f"Unexpected error: {error}")

    def schedule_range(
        self, start_date: date_, end_date: date_, *, scope: RangeScope = RangeScope.PLANNED
    ) -> ControllerResult[RangeScheduleResult]:
        """
        Allocate [start_date, end_date], generate each of its dates (one
        selected-day generation per date), and save all of the range's
        placements in one transaction. On any failure nothing is saved and
        no day state changes; the previously committed schedule stays.
        """
        current_date = start_date

        def op() -> RangeScheduleResult:
            nonlocal current_date
            allocation = self._compute_allocation(start_date, end_date, scope)
            tasks = self._service.load_range(start_date, end_date, scope=scope).tasks

            outputs: dict[date_, DayScheduleOutput] = {}
            states: dict[date_, SelectedDayState] = {}
            current_date = start_date
            while current_date <= end_date:
                outputs[current_date], states[current_date] = self._generate(allocation, current_date, tasks)
                current_date += timedelta(days=1)

            placements = [placement for output in outputs.values() for placement in output.placements]
            replacement = self._service.replace_placements(start_date, end_date, placements)
            # Committed: only now does the new allocation/day state become visible.
            self._adopt_allocation(allocation, scope)
            self._day_states.update(states)
            return RangeScheduleResult(allocation=allocation, outputs=outputs, replacement=replacement)

        try:
            with self._lock:
                return ControllerResult.success(op())
        except MandatoryTaskSchedulingError as error:
            return ControllerResult.failure(_scheduling_failure_message(current_date, error))
        except PlanningError as error:
            return ControllerResult.failure(str(error))
        except Exception as error:  # noqa: BLE001 - last-resort safety net
            return ControllerResult.failure(f"Unexpected error: {error}")

    def _generate(
        self, allocation: AllocationResult, selected_date: date_, tasks: TaskRegistry
    ) -> tuple[DayScheduleOutput, SelectedDayState]:
        preferences = self._resolve(selected_date)
        previous_result = self._service.stored_day_output(selected_date, preferences.timezone)
        return generate_selected_day(
            allocation, selected_date, tasks, {selected_date: preferences},
            {selected_date: self._service.fixed_blocks_for_date(selected_date)},
            previous_result=previous_result,
        )

    def day_state(self, day: date_) -> ControllerResult[SelectedDayState]:
        """In-memory state; else STALE for saved placements from an earlier session (see module docstring)."""

        def op() -> SelectedDayState:
            state = self._day_states.get(day)
            if state is not None:
                return state
            stored = self._service.stored_day_output(day, self._timezone)
            if stored is None:
                return initial_state(day)
            return SelectedDayState(
                date=day, status=DayResultStatus.STALE, result=stored, generated_from_allocation_id=None
            )

        return self._call(op)

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
            with self._lock:
                return ControllerResult.success(operation())
        except PlanningError as error:
            return ControllerResult.failure(str(error))
        except Exception as error:  # noqa: BLE001 - last-resort safety net
            return ControllerResult.failure(f"Unexpected error: {error}")
