"""
app/ui/planning_controller.py

The shared, testable state/controller boundary for canonical week/month/day
planning (Task 6 / Schedule Maxing v2). Deliberately Tk-free -- every method
here is plain Python, exercised by tests/ui/test_planning_controller.py
without a display -- so app/app.py's widget wiring has a single,
already-tested source of truth for task identities, preferences,
allocation, and generated results, instead of reimplementing any of
app/planning/'s logic in a callback. Domain decisions (scheduling,
allocation, scoring) stay in app/planning/ and app/optimizer.py; this class
only delegates.

Authoritative data: tasks, fixed blocks, generated placements, the user
and per-date preference layers, and schedule provenance (Milestone 3) are
read from and written to the persistence-backed
app.planning.application.PlanningService -- the controller keeps no copy
of them. Every task/block/placement it returns is a fresh snapshot loaded
from the database, so mutating a returned model never changes saved state;
call add_or_update_task/set_fixed_blocks/... to save an edit. If no
service is injected, the controller opens a private in-memory SQLite
database; pass `service=` to persist to a real database file.

Preconditions: an update or delete passes the version the caller last read
(expected_version / expected_versions); a stale write fails with the
service's VersionConflictError message and changes nothing. Creates omit it.

Preferences: resolved on demand as YAML template (loaded once, see
refresh_yaml_layer) -> the stored user layer -> the stored layer of that
date. Nothing is cached, so a value saved by any caller is what the next
resolution uses.

Derived state kept in memory only: the current AllocationResult of an
explicit allocate_range/allocate_week/allocate_month (Week/Month
allocation is a preview, and generate_day generates from it). A
successful task/fixed-block/preference edit clears it (it must be re-run,
never silently redone). A failed write changes nothing and clears nothing.

Day state (day_state/day_states) is derived from SQLite, so it survives a
restart: a date with a generation record whose inputs fingerprint and
placement digest still match is GENERATED (current) -- even if it placed
nothing; one whose inputs or placements changed is STALE; saved placements
without any record (pre-v4 data) are STALE with StaleReason.NO_PROVENANCE;
nothing at all is ALLOCATED (initial). See app/planning/provenance.py.

generate_day and schedule_range (the desktop pages' "Make Schedule") save
through PlanningService.reschedule_range: the previous stored placements
of each generated date are the engine's previous_result (so an unchanged
placement keeps its id -- and any execution history linked to it -- across
regenerations and restarts) *and* the save's precondition; placements of
the same occurrences outside the range are superseded; and the provenance
is written in the same transaction. Either the whole range's new schedule
is committed, or none of it is.

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
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, timedelta, timezone

from app.execution.db import get_connection
from app.optimizer import MandatoryTaskSchedulingError
from app.planning.allocation import AllocationResult, allocate_tasks, month_dates, week_dates
from app.planning.application import (
    BatchApplyResult,
    GenerationProvenance,
    ImportApplyResult,
    PlacementReplacement,
    PlanningRange,
    PlanningService,
    RangeClearResult,
    RangeScope,
)
from app.planning.csv_canonical import is_canonical_csv, parse_canonical_csv_file
from app.planning.csv_export import PlanningExportResult, export_planning_csv
from app.planning.csv_import import ImportMode, ParsedImport, read_csv_text, parse_legacy_csv_file
from app.planning.errors import PlanningError, VersionConflictError
from app.planning.external_dependencies import (
    ExternalDependency,
    allocation_dates,
    explain_unallocated,
    satisfaction_instants,
)
from app.planning.models import DayScheduleOutput, FixedBlock, ScheduledTask, Task, TaskRegistry
from app.planning.preferences import (
    DayPreferences,
    OptimizerMode,
    PreferenceOverrides,
    PreferenceRecord,
    day_preferences_overrides_from_reward_settings,
    resolve_day_preferences,
)
from app.planning.provenance import classify_generation, inputs_fingerprint, placements_digest
from app.planning.repository import PlanningRepository
from app.planning.service import DayResultStatus, SelectedDayState, generate_selected_day, initial_state
from app.reward import load_reward_settings
from app.ui.background import ControllerResult


@dataclass(frozen=True)
class RangeScheduleResult:
    """One committed Make Schedule run over a date range."""

    allocation: AllocationResult
    outputs: dict[date_, DayScheduleOutput]
    replacement: PlacementReplacement
    #: Active placements outside the range that this run superseded (removed).
    superseded_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class _SchedulingInputs:
    """One consistent snapshot of everything a range generation reads, plus its fingerprint."""

    start_date: date_
    end_date: date_
    scope: RangeScope
    planning_range: PlanningRange
    preferences_by_date: dict[date_, DayPreferences]
    external: dict[uuid.UUID, ExternalDependency]
    fingerprint: str


def _scheduling_failure_message(selected_date: date_, error: MandatoryTaskSchedulingError) -> str:
    reasons = "; ".join(
        f"{failure.task_id} [{failure.reason_code.value}]: {failure.explanation}" for failure in error.failures
    )
    return f"Could not generate {selected_date}: {reasons}"


def _range_dates(start_date: date_, end_date: date_) -> list[date_]:
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


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
        # Loaded once at construction, not reloaded from disk on every
        # resolution -- see app/optimizer.py's own "avoid reloading YAML"
        # performance note. Call refresh_yaml_layer() to pick up an edited
        # config/task_preference.yaml without recreating the controller.
        self._yaml_overrides = day_preferences_overrides_from_reward_settings(load_reward_settings(project_root=project_root))
        self._allocation: AllocationResult | None = None
        self._allocation_inputs: _SchedulingInputs | None = None

    def close(self) -> None:
        """Close the private in-memory database, if this controller opened one."""
        if self._owned_connection is not None:
            self._owned_connection.close()
            self._owned_connection = None

    # ------------------------------------------------------------------
    # Tasks (shared identity across Day/Week/Month views)
    # ------------------------------------------------------------------

    def add_or_update_task(self, task: Task, *, expected_version: int | None = None) -> ControllerResult[Task]:
        """Create (no expected_version) or update one task; the value is the stored snapshot."""

        def op() -> Task:
            saved = self._service.save_task(task, expected_version=expected_version)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def add_or_update_tasks(
        self, tasks: list[Task], *, expected_versions: dict[uuid.UUID, int] | None = None
    ) -> ControllerResult[list[Task]]:
        """Save several tasks atomically (all or none); ids in expected_versions are updates."""

        def op() -> list[Task]:
            saved = self._service.save_tasks(tasks, expected_versions=expected_versions)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def remove_task(self, task_id: uuid.UUID, *, expected_version: int) -> ControllerResult[None]:
        def op() -> None:
            if self._service.delete_task(task_id, expected_version=expected_version):
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

    def set_fixed_blocks(
        self, day: date_, blocks: list[FixedBlock], *, expected_versions: dict[uuid.UUID, int] | None = None
    ) -> ControllerResult[None]:
        """Make `day`'s blocks exactly `blocks`; expected_versions must cover every block stored on `day`."""

        def op() -> None:
            self._service.set_fixed_blocks_for_date(day, blocks, expected_versions=expected_versions)
            self._invalidate_generated_results()

        return self._call(op)

    def get_fixed_blocks(self, day: date_) -> ControllerResult[list[FixedBlock]]:
        return self._call(lambda: self._service.fixed_blocks_for_date(day))

    def save_fixed_block(self, block: FixedBlock, *, expected_version: int | None = None) -> ControllerResult[FixedBlock]:
        """Create (no expected_version) or edit one fixed block (its date may change)."""

        def op() -> FixedBlock:
            saved = self._service.save_fixed_block(block, expected_version=expected_version)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def delete_fixed_block(self, block_id: uuid.UUID, *, expected_version: int) -> ControllerResult[bool]:
        def op() -> bool:
            deleted = self._service.delete_fixed_block(block_id, expected_version=expected_version)
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
        """Write a validated legacy CSV import in one transaction (see app/planning/csv_import.py for the modes)."""

        def op() -> ImportApplyResult:
            replace_range = (parsed.start_date, parsed.end_date) if mode == ImportMode.REPLACE else None
            result = self._service.apply_import(parsed.tasks, parsed.fixed_blocks, replace_range=replace_range)
            self._invalidate_generated_results()
            return result

        return self._call(op)

    def import_csv_file(
        self, path: str, *, anchor_date: date_, mode: ImportMode, allow_updates: bool = False
    ) -> ControllerResult[ImportApplyResult | BatchApplyResult]:
        """
        Import a CSV. A legacy schedule CSV is parsed with day 1 ==
        anchor_date in this controller's timezone and applied per `mode`. A
        canonical stored-planning CSV (it has `record_type`/`id` columns) is
        merged by identity (app/planning/csv_canonical.py); the anchor date
        does not apply to it, and REPLACE is refused for it rather than
        silently reinterpreted.
        """
        try:
            text = read_csv_text(path)
        except (OSError, UnicodeDecodeError) as error:
            return ControllerResult.failure(f"Could not read {path}: {error}")

        if is_canonical_csv(text):
            if mode == ImportMode.REPLACE:
                return ControllerResult.failure(
                    "This is a stored-planning CSV with record ids; it is merged by id and cannot be imported "
                    "with Replace. Use Append."
                )
            try:
                batch = parse_canonical_csv_file(path)
            except PlanningError as error:
                return ControllerResult.failure(str(error))

            def op() -> BatchApplyResult:
                result = self._service.apply_record_batch(batch, allow_updates=allow_updates)
                self._invalidate_generated_results()
                return result

            return self._call(op)

        try:
            parsed = parse_legacy_csv_file(path, anchor_date=anchor_date, timezone=self._timezone)
        except PlanningError as error:
            return ControllerResult.failure(str(error))
        return self.apply_import(parsed, mode)

    def export_planning_csv(
        self, path: str, *, start_date: date_ | None = None, end_date: date_ | None = None, include_deleted: bool = False
    ) -> ControllerResult[PlanningExportResult]:
        """Write the stored-planning CSV (read-only; see app/planning/csv_export.py)."""
        return self._call(
            lambda: export_planning_csv(
                self._service, path, start_date=start_date, end_date=end_date, include_deleted=include_deleted
            )
        )

    def clear_range(
        self, start_date: date_, end_date: date_, *, include_planning_data: bool
    ) -> ControllerResult[RangeClearResult]:
        """Reset scope (see PlanningService.clear_range): never deletes execution history."""

        def op() -> RangeClearResult:
            result = self._service.clear_range(start_date, end_date, include_planning_data=include_planning_data)
            self._invalidate_generated_results()
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

    def user_preferences(self) -> ControllerResult[PreferenceRecord | None]:
        """The stored user layer (with its version, the precondition for changing it)."""
        return self._call(self._service.user_preferences)

    def date_preferences(self, day: date_) -> ControllerResult[PreferenceRecord | None]:
        return self._call(lambda: self._service.date_preferences(day))

    def set_user_overrides(
        self, overrides: PreferenceOverrides | None, *, expected_version: int | None = None
    ) -> ControllerResult[PreferenceRecord | None]:
        """
        Save the user layer (applied to every date unless a date layer says
        otherwise), or delete it with overrides=None. Creating omits
        expected_version; changing or deleting an existing layer needs it.
        """

        def op() -> PreferenceRecord | None:
            if overrides is None:
                self._delete_preference_layer(None, expected_version)
                self._invalidate_generated_results()
                return None
            saved = self._service.save_user_preferences(overrides, expected_version=expected_version)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def set_date_overrides(
        self, day: date_, overrides: PreferenceOverrides | None, *, expected_version: int | None = None
    ) -> ControllerResult[PreferenceRecord | None]:
        """Save (or, with overrides=None, delete) the layer of one date; see set_user_overrides."""

        def op() -> PreferenceRecord | None:
            if overrides is None:
                self._delete_preference_layer(day, expected_version)
                self._invalidate_generated_results()
                return None
            saved = self._service.save_date_preferences(day, overrides, expected_version=expected_version)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def set_engine_mode(self, mode: OptimizerMode) -> ControllerResult[PreferenceRecord]:
        """
        An explicit "schedule in this mode from now on" command (e.g. the
        CLI's --mode): sets optimizer_mode on the stored user layer, keeping
        every other stored field. The read and the compare-and-update happen
        in one transaction, so the precondition is the version just read and
        no concurrent change to the layer is overwritten.
        """

        def op() -> PreferenceRecord:
            with self._service.transaction():
                stored = self._service.user_preferences()
                if stored is None:
                    saved = self._service.save_user_preferences(PreferenceOverrides(optimizer_mode=mode))
                else:
                    updated = stored.overrides.model_copy(update={"optimizer_mode": mode})
                    saved = self._service.save_user_preferences(updated, expected_version=stored.version)
            self._invalidate_generated_results()
            return saved

        return self._call(op)

    def _delete_preference_layer(self, day: date_ | None, expected_version: int | None) -> None:
        stored = self._service.user_preferences() if day is None else self._service.date_preferences(day)
        if stored is None:
            return
        if expected_version is None:
            raise VersionConflictError(
                "preference", stored.id, expected_version=None, current_version=stored.version,
                message="These preferences exist (version "
                f"{stored.version}); pass the version you read to delete them.",
            )
        if day is None:
            self._service.delete_user_preferences(expected_version=expected_version)
        else:
            self._service.delete_date_preferences(day, expected_version=expected_version)

    def resolve_preferences(self, day: date_) -> ControllerResult[DayPreferences]:
        return self._call(lambda: self._resolve_range([day])[day])

    def _resolve_range(self, dates: list[date_]) -> dict[date_, DayPreferences]:
        user = self._service.user_preferences()
        by_date = self._service.date_preferences_for_range(min(dates), max(dates))
        return {
            day: resolve_day_preferences(
                date=day, timezone=self._timezone,
                yaml_overrides=self._yaml_overrides,
                user_overrides=user.overrides if user is not None else None,
                date_overrides=by_date[day].overrides if day in by_date else None,
            )
            for day in dates
        }

    # ------------------------------------------------------------------
    # Allocation (Week / Month) -- never calls the Day Scheduler
    # ------------------------------------------------------------------

    def allocate_range(
        self, start_date: date_, end_date: date_, *, scope: RangeScope = RangeScope.ELIGIBLE
    ) -> ControllerResult[AllocationResult]:
        def op() -> AllocationResult:
            inputs = self._scheduling_inputs(start_date, end_date, scope)
            result = self._allocate(inputs)
            self._allocation, self._allocation_inputs = result, inputs
            return result

        return self._call(op)

    def _scheduling_inputs(self, start_date: date_, end_date: date_, scope: RangeScope) -> _SchedulingInputs:
        """Everything a generation of [start_date, end_date] reads, as one consistent snapshot."""
        dates = _range_dates(start_date, end_date)
        with self._service.transaction():
            planning_range = self._service.load_range(start_date, end_date, scope=scope)
            preferences_by_date = self._resolve_range(dates)
            external = self._service.external_dependencies(
                planning_range.tasks.tasks.values(), start_date, end_date, self._timezone
            )
        fingerprint = inputs_fingerprint(
            start_date=start_date, end_date=end_date, scope=scope.value, timezone_name=self._timezone,
            tasks=planning_range.tasks.tasks.values(),
            fixed_blocks=[block for day in dates for block in planning_range.fixed_blocks_by_date[day]],
            preferences_by_date=preferences_by_date, external_dependencies=external,
        )
        return _SchedulingInputs(
            start_date=start_date, end_date=end_date, scope=scope, planning_range=planning_range,
            preferences_by_date=preferences_by_date, external=external, fingerprint=fingerprint,
        )

    def _allocate(self, inputs: _SchedulingInputs) -> AllocationResult:
        planning_range = inputs.planning_range
        result = allocate_tasks(
            start_date=inputs.start_date, end_date=inputs.end_date, tasks=planning_range.tasks,
            task_ids=planning_range.task_ids, preferences_by_date=inputs.preferences_by_date,
            fixed_blocks_by_date=planning_range.fixed_blocks_by_date,
            external_dependency_dates=allocation_dates(inputs.external),
        )
        return explain_unallocated(result, planning_range.tasks.tasks, inputs.external)

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
            if self._allocation is None or self._allocation_inputs is None:
                raise RuntimeError("Allocate a week/month (allocate_week/allocate_month) before generating a day.")
            allocation, inputs = self._allocation, self._allocation_inputs
            if not inputs.start_date <= selected_date <= inputs.end_date:
                raise RuntimeError(f"{selected_date} is outside the allocated range {inputs.start_date} .. {inputs.end_date}.")

            output, expected = self._generate(allocation, inputs, selected_date)
            # Save first (placements + provenance, atomically); only then is it the day's result.
            self._service.reschedule_range(
                selected_date, selected_date, {selected_date: output},
                expected_versions=expected, provenance=self._provenance(allocation, inputs, [selected_date]),
            )
            return output

        return self._generation_call(op, lambda: selected_date)

    def schedule_range(
        self, start_date: date_, end_date: date_, *, scope: RangeScope = RangeScope.PLANNED
    ) -> ControllerResult[RangeScheduleResult]:
        """
        Allocate [start_date, end_date], generate each of its dates (one
        selected-day generation per date), and save the whole range --
        placements, superseded-placement cleanup, and provenance -- in one
        transaction. On any failure nothing is saved; the previously
        committed schedule stays.
        """
        current_date = start_date

        def op() -> RangeScheduleResult:
            nonlocal current_date
            inputs = self._scheduling_inputs(start_date, end_date, scope)
            allocation = self._allocate(inputs)

            outputs: dict[date_, DayScheduleOutput] = {}
            expected: dict[uuid.UUID, int] = {}
            for current_date in _range_dates(start_date, end_date):
                outputs[current_date], previous_versions = self._generate(allocation, inputs, current_date)
                expected.update(previous_versions)

            result = self._service.reschedule_range(
                start_date, end_date, outputs, expected_versions=expected,
                provenance=self._provenance(allocation, inputs, list(outputs)),
            )
            # Committed: only now does the new allocation become the current one.
            self._allocation, self._allocation_inputs = allocation, inputs
            return RangeScheduleResult(
                allocation=allocation, outputs=outputs, replacement=result.replacement,
                superseded_ids=result.superseded_ids,
            )

        return self._generation_call(op, lambda: current_date)

    def _generate(
        self, allocation: AllocationResult, inputs: _SchedulingInputs, selected_date: date_
    ) -> tuple[DayScheduleOutput, dict[uuid.UUID, int]]:
        """Generate one date; also return the stored placements it replaces, {id: version} (the save's precondition)."""
        preferences = inputs.preferences_by_date[selected_date]
        previous_result = self._service.stored_day_output(selected_date, preferences.timezone)
        output, _ = generate_selected_day(
            allocation, selected_date, inputs.planning_range.tasks, {selected_date: preferences},
            {selected_date: inputs.planning_range.fixed_blocks_by_date[selected_date]},
            previous_result=previous_result,
            external_dependency_satisfaction=satisfaction_instants(inputs.external),
        )
        previous = {placement.id: placement.version for placement in previous_result.placements} if previous_result else {}
        return output, previous

    def _provenance(
        self, allocation: AllocationResult, inputs: _SchedulingInputs, dates: list[date_]
    ) -> GenerationProvenance:
        return GenerationProvenance(
            allocation_id=allocation.id,
            range_start=inputs.start_date,
            range_end=inputs.end_date,
            range_scope=inputs.scope.value,
            fingerprint=inputs.fingerprint,
            timezone=self._timezone,
            engine_modes={day: inputs.preferences_by_date[day].optimizer_mode for day in dates},
            generated_at=datetime.now(timezone.utc),
        )

    def day_state(self, day: date_) -> ControllerResult[SelectedDayState]:
        """The persisted state of one date (see the module docstring)."""
        return self._call(lambda: self._day_states([day])[day])

    def day_states(self, dates: list[date_]) -> ControllerResult[dict[date_, SelectedDayState]]:
        """day_state for several dates, recomputing each distinct recorded range's inputs only once."""
        return self._call(lambda: self._day_states(dates))

    def _day_states(self, dates: list[date_]) -> dict[date_, SelectedDayState]:
        if not dates:
            return {}
        start, end = min(dates), max(dates)
        records = self._service.generation_records(start, end)
        placements_by_date = self._service.placements_for_range(start, end)
        fingerprints: dict[tuple, str | None] = {}
        states: dict[date_, SelectedDayState] = {}

        for day in dates:
            record = records.get(day)
            placements = placements_by_date[day]
            current_fingerprint = None
            if record is not None:
                key = (record.range_start, record.range_end, record.range_scope)
                if key not in fingerprints:
                    try:
                        fingerprints[key] = self._scheduling_inputs(
                            record.range_start, record.range_end, RangeScope(record.range_scope)
                        ).fingerprint
                    except (PlanningError, ValueError):
                        fingerprints[key] = None  # the recorded inputs can no longer be recomputed
                current_fingerprint = fingerprints[key]

            is_current, reason = classify_generation(
                record, has_placements=bool(placements), current_fingerprint=current_fingerprint,
                current_placements_digest=placements_digest(placements),
            )
            if is_current is None:
                states[day] = initial_state(day)
                continue
            timezone_name = record.timezone if record is not None else self._timezone
            result = self._service.stored_day_output(day, timezone_name) or DayScheduleOutput(
                date=day, timezone=timezone_name
            )
            states[day] = SelectedDayState(
                date=day,
                status=DayResultStatus.GENERATED if is_current else DayResultStatus.STALE,
                result=result,
                generated_from_allocation_id=record.allocation_id if record is not None else None,
                stale_reason=reason,
            )
        return states

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _invalidate_generated_results(self) -> None:
        """
        A successful task/fixed-block/preference edit clears the current
        in-memory allocation: Week/Month must be explicitly re-allocated,
        never silently re-run. Saved schedules need no invalidation here --
        their current/stale state is recomputed from SQLite (provenance),
        so any edit, by any caller, is taken into account.
        """
        self._allocation = None
        self._allocation_inputs = None

    def _generation_call(self, operation, current_date):
        try:
            with self._lock:
                return ControllerResult.success(operation())
        except MandatoryTaskSchedulingError as error:
            # A structured, expected outcome (not every day generates
            # successfully) -- surfaced through the same ControllerResult
            # channel as any other failure, with the per-task reasons
            # preserved in the message rather than swallowed.
            return ControllerResult.failure(_scheduling_failure_message(current_date(), error))
        except PlanningError as error:
            return ControllerResult.failure(str(error))
        except Exception as error:  # noqa: BLE001 - last-resort safety net
            return ControllerResult.failure(f"Unexpected error: {error}")

    def _call(self, operation):
        try:
            with self._lock:
                return ControllerResult.success(operation())
        except PlanningError as error:
            return ControllerResult.failure(str(error))
        except Exception as error:  # noqa: BLE001 - last-resort safety net
            return ControllerResult.failure(f"Unexpected error: {error}")
