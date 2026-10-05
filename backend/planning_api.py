"""
backend/planning_api.py

The scheduling application API for the web UI (Milestone 4): typed HTTP
operations over the shared planning layer -- app.planning.application.
PlanningService and app/planning/workflow.py -- documented in
docs/web-api.md and in the OpenAPI schema.

    GET  /planning/capabilities          what this deployment profile offers
    GET  /planning/snapshot              a bounded range: tasks, blocks, placements, per-date freshness
    GET  /planning/preferences           effective and inherited preferences, the editable layers, engines
    POST /planning/allocation/preview    allocation from persisted inputs (never generates placements)
    POST /planning/generate              selected-date generation (full or incremental), saved atomically
    POST /planning/reset/preview         what a range reset would remove (nothing is written)
    POST /planning/reset                 the confirmed reset
    POST /planning/placements/{id}/reschedule  move one placement (docs/execution-rescheduling.md)
    POST /planning/placements/{id}/release     release a placement's manual intent (it is not moved)
    POST /planning/recurrence/expand     materialize a range's recurring occurrences (docs/recurrence.md)
    POST /planning/occurrences/{id}/edit    change one occurrence ("this occurrence")
    POST /planning/occurrences/{id}/delete  skip or delete one occurrence
    POST /planning/series/{id}/edit      change a series: this and later occurrences, or the entire series
    POST /planning/series/{id}/delete    delete this and later occurrences, or the entire series
    GET  /planning/analytics/schedule-cohort  planned-versus-actual report of a date range (docs/analytics.md)
    POST /planning/csv/preview           validate a canonical v2 CSV against what is stored, apply nothing
    POST /planning/csv/import            apply it (all or nothing)
    GET  /planning/csv/export            the canonical v2 CSV of a range or of everything

The router is storage-agnostic: build_planning_router takes a FastAPI
dependency that yields a PlanningContext -- a PlanningService already scoped
to the authenticated owner. The hosted server (hosted_context below) backs it
with PostgreSQL through backend/planning_repository.py; the local web profile
backs it with the device's SQLite store. Neither copies scheduling logic:
every operation is a PlanningService/workflow call. Projects, tasks, fixed
blocks and preference layers are edited through the existing record
endpoints (/projects, /tasks, /fixed-blocks, /preferences).

Owner: always derived from authentication by the context dependency, never
from a request body. A CSV whose records belong to anyone else (ownerless
records included -- claiming those is the separate association step) is
refused; versions in a file are only ever preconditions.
"""

from __future__ import annotations

import dataclasses
import io
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.optimizer import MandatoryTaskSchedulingError
from app.planning import series as series_ops
from app.planning import workflow
from app.planning.allocation import AllocationResult
from app.planning.application import PlacementReschedule, PlanningService, RangeScope, ResetPreview
from app.planning.csv_canonical import is_canonical_csv, parse_canonical_csv
from app.planning.csv_export import FORMAT_VERSION, write_planning_csv
from app.planning.csv_import import CsvImportError
from app.planning.errors import (
    DuplicateEntityError,
    EntityInUseError,
    EntityNotFoundError,
    GenerationLimitError,
    HistoryProtectedError,
    InvalidEntityError,
    InvalidReferenceError,
    PlanningError,
    RecurrenceLimitError,
    RegenerationRequiredError,
    RescheduleRejectedError,
    ScopeError,
    SeriesConfigurationError,
    StaleInputsError,
    VersionConflictError,
)
from app.planning.fixed_block_rules import FixedBlockRuleViolation
from app.planning.models import FixedBlock, Project, ScheduledTask, Task
from app.planning.preferences import (
    ENGINE_DESCRIPTIONS,
    DayPreferences,
    OptimizerMode,
    PreferenceOverrides,
    PreferenceRecord,
)
from app.planning.time import AmbiguousLocalTimeError, UnsupportedSchedulingWindowError, validate_timezone
from app.productivity.schedule_cohort import ScheduleCohortReport, read_schedule_cohort_report
from backend.errors import ApiError
from backend.resources import FixedBlockOut, PlacementOut, PreferenceOut, ProjectOut, TaskFields, TaskOut

#: The largest canonical CSV one upload may carry.
MAX_CSV_BYTES = 5 * 1024 * 1024

#: The engine catalog (shared with the desktop: app.planning.preferences.ENGINE_DESCRIPTIONS).
ENGINES = ENGINE_DESCRIPTIONS


@dataclass(frozen=True)
class PlanningContext:
    """What one request works on: a PlanningService restricted to the authenticated owner."""

    service: PlanningService
    #: "hosted" (server persistence) or "local" (this device's SQLite store).
    profile: str


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _timezone(value: str) -> str:
    validate_timezone(value)
    return value


class RangeIn(Strict):
    start_date: date_
    end_date: date_
    #: The IANA timezone the dates are planned in (e.g. the browser's zone).
    timezone: str
    #: "planned": tasks planned on these dates plus eligible undated ones; "eligible": every task that could go here.
    scope: Literal["planned", "eligible"] = "planned"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        return _timezone(value)

    @model_validator(mode="after")
    def _bounded(self):
        workflow_range_error(self.start_date, self.end_date)
        return self


def workflow_range_error(start: date_, end: date_) -> None:
    if end < start:
        raise ValueError("end_date must not be before start_date")
    if (end - start).days + 1 > workflow.MAX_RANGE_DAYS:
        raise ValueError(f"a range may span at most {workflow.MAX_RANGE_DAYS} days")


class DayStateOut(BaseModel):
    date: date_
    #: none: nothing saved; current: the saved schedule matches today's inputs; stale: it does not.
    status: Literal["none", "current", "stale"]
    #: Why a stale date is stale: no_provenance, inputs_changed or placements_changed.
    stale_reason: str | None = None
    generated_at: datetime | None = None
    engine_mode: OptimizerMode | None = None
    #: The allocation range the saved schedule was generated from.
    range_start: date_ | None = None
    range_end: date_ | None = None
    placement_count: int
    #: How many tasks that generation could not place. Their reasons are not stored, so they are never shown again.
    unscheduled_count: int | None = None
    total_score: float | None = None


class SnapshotOut(BaseModel):
    start_date: date_
    end_date: date_
    timezone: str
    scope: Literal["planned", "eligible"]
    #: The range's tasks (per scope) plus the tasks of its placements.
    tasks: list[TaskOut]
    #: Projects those tasks reference.
    projects: list[ProjectOut]
    fixed_blocks: list[FixedBlockOut]
    placements: list[PlacementOut]
    days: list[DayStateOut]
    #: The placements generation keeps as manual intent: preserved ones, and older ones whose recorded lineage
    #: proves they are a move's destination (docs/execution-rescheduling.md, "Manual placements").
    preserved_placement_ids: list[uuid.UUID] = Field(default_factory=list)


class EngineOut(BaseModel):
    mode: OptimizerMode
    description: str


class DayPreferenceViewOut(BaseModel):
    date: date_
    #: What scheduling uses on this date: defaults -> template -> user layer -> date layer.
    effective: DayPreferences
    #: What the date would use without its own layer (defaults -> template -> user layer).
    inherited: DayPreferences
    date_layer_id: uuid.UUID | None = None


class PreferencesOut(BaseModel):
    timezone: str
    #: The read-only YAML template layer (config/task_preference.yaml).
    template: PreferenceOverrides
    #: The editable user layer (edit through /preferences), if any.
    user_layer: PreferenceOut | None
    #: The editable date layers of the range.
    date_layers: list[PreferenceOut]
    days: list[DayPreferenceViewOut]
    engines: list[EngineOut]


class UnallocatedOut(BaseModel):
    task_id: uuid.UUID
    reason_code: str
    explanation: str
    required: bool
    proven_infeasible: bool


class AssignmentOut(BaseModel):
    task_id: uuid.UUID
    date: date_


class CapacityOut(BaseModel):
    date: date_
    remaining_minutes: int


class AllocationPreviewOut(BaseModel):
    start_date: date_
    end_date: date_
    timezone: str
    scope: Literal["planned", "eligible"]
    #: The inputs fingerprint; pass it as expected_fingerprint to generate exactly what was previewed.
    fingerprint: str
    assignments: list[AssignmentOut]
    unallocated: list[UnallocatedOut]
    capacity: list[CapacityOut]
    diagnostics: list[str]
    days: list[DayStateOut]


class GenerateIn(RangeIn):
    #: The dates to generate (a contiguous part of the range; default: the whole range).
    generate_start: date_ | None = None
    generate_end: date_ | None = None
    #: full: generate from scratch (work already started or finished is kept); incremental: keep every saved
    #: placement that still fits and schedule only new work around it.
    mode: Literal["full", "incremental"] = "full"
    #: The fingerprint of the allocation preview acted on; a change since then is a 409 inputs_changed.
    expected_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)


class UnscheduledOut(BaseModel):
    task_id: uuid.UUID
    reason_code: str
    explanation: str


class GeneratedDayOut(BaseModel):
    date: date_
    engine_mode: OptimizerMode
    placements: list[PlacementOut]
    #: Placements kept exactly as they were (incremental mode, or work already started or finished).
    kept_placement_ids: list[uuid.UUID]
    #: Why tasks allocated here were not placed; null when unknown (an already-current schedule from earlier).
    unscheduled: list[UnscheduledOut] | None
    unscheduled_count: int | None
    total_score: float
    #: The day's objective under its mode, recomputed after generation (docs/scheduling-modes.md): baseline
    #: reward B(S), the mode's component, the objective, first start / last finish (minutes into the day
    #: window), idle and fixed minutes. Null for an already-current answer.
    evaluation: dict[str, Any] | None = None


class KeptElsewhereOut(BaseModel):
    task_id: uuid.UUID
    placement_id: uuid.UUID
    date: date_


class PlacementProblemOut(BaseModel):
    """A kept placement that no longer fits (workflow.PlacementProblem)."""

    placement_id: uuid.UUID
    task_id: uuid.UUID
    date: date_
    reason: str
    explanation: str
    #: Why it is kept: history / manual / kept (incremental) / destination (a move).
    kept_as: str
    blocking: bool
    #: What resolves it: edit_constraint, move, release_manual_intent, choose_another_range, regenerate_full.
    remedies: list[str]


class GenerateOut(BaseModel):
    #: generated: saved now; already_current: the saved schedule already matched, nothing was written.
    status: Literal["generated", "already_current"]
    mode: Literal["full", "incremental"]
    fingerprint: str
    days: list[GeneratedDayOut]
    unallocated: list[UnallocatedOut]
    #: Always empty since Milestone 6: generation never removes placements outside the generated dates
    #: (kept for compatibility; see kept_elsewhere).
    superseded_placement_ids: list[uuid.UUID]
    #: Always empty since Milestone 6 (kept for compatibility).
    history_protected_placement_ids: list[uuid.UUID]
    #: Replaced placements that execution history references (the history itself is kept).
    removed_with_history_placement_ids: list[uuid.UUID]
    #: Occurrences the range plans that are already live outside the generated dates: left there, not placed again.
    kept_elsewhere: list[KeptElsewhereOut] = Field(default_factory=list)
    #: Kept history that no longer fits the current inputs (non-blocking: it stays exactly as recorded).
    notices: list[PlacementProblemOut] = Field(default_factory=list)


class ReleaseIn(Strict):
    #: The version of the placement the release is based on (the precondition).
    base_version: int = Field(gt=0)


class ResetRangeIn(Strict):
    start_date: date_
    end_date: date_

    @model_validator(mode="after")
    def _bounded(self):
        workflow_range_error(self.start_date, self.end_date)
        return self


class ResetCommitIn(ResetRangeIn):
    #: The token of the preview being confirmed.
    confirmation: str = Field(min_length=64, max_length=64)


class ResetPreviewOut(BaseModel):
    start_date: date_
    end_date: date_
    #: Records that will be deleted, per kind (cascade_placements: dated outside the range).
    counts: dict[str, int]
    placement_ids: list[uuid.UUID]
    cascade_placement_ids: list[uuid.UUID]
    generation_ids: list[uuid.UUID]
    fixed_block_ids: list[uuid.UUID]
    task_ids: list[uuid.UUID]
    date_preference_ids: list[uuid.UUID]
    placements_with_history_ids: list[uuid.UUID]
    tasks_with_history_ids: list[uuid.UUID]
    protected_recurring_task_ids: list[uuid.UUID]
    #: Task to delete -> tasks outside the reset that depend on it; non-empty means the reset will be refused.
    blocking_dependents: dict[uuid.UUID, list[uuid.UUID]]
    blocked: bool
    token: str


class ResetResultOut(BaseModel):
    start_date: date_
    end_date: date_
    deleted: dict[str, int]
    placements_with_history_ids: list[uuid.UUID]
    protected_recurring_task_ids: list[uuid.UUID]


class RescheduleIn(Strict):
    #: The version of the placement the move is based on (the precondition).
    base_version: int = Field(gt=0)
    planned_date: date_
    timezone: str
    planned_start: AwareDatetime
    planned_end: AwareDatetime
    #: The new placement's id; a client that chooses it can recognize its own move after a lost response
    #: (the moved placement's tombstone names it as superseded_by_id).
    replacement_id: uuid.UUID | None = None

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        return _timezone(value)


class ExpandIn(Strict):
    start_date: date_
    end_date: date_

    @model_validator(mode="after")
    def _bounded(self):
        workflow_range_error(self.start_date, self.end_date)
        return self


class SlotProblemOut(BaseModel):
    series_id: uuid.UUID
    slot: date_ | None
    code: str
    message: str


class LegacyCollisionOut(BaseModel):
    series_id: uuid.UUID
    slot: date_
    placement_ids: list[uuid.UUID]


class ExpansionOut(BaseModel):
    start_date: date_
    end_date: date_
    #: The occurrences this call materialized (empty when the range was already expanded).
    created: list[TaskOut]
    existing_count: int
    #: Series without an explicit start date and time zone: they produce no occurrences until configured.
    needs_configuration: list[uuid.UUID]
    problems: list[SlotProblemOut]
    warnings: list[SlotProblemOut]
    legacy_collisions: list[LegacyCollisionOut]


class OccurrenceEditIn(Strict):
    base_version: int = Field(gt=0)
    #: The occurrence's content as it should be (its series and original slot cannot change).
    task: TaskFields


class OccurrenceDeleteIn(Strict):
    base_version: int = Field(gt=0)
    #: True: skipped; False: deleted. Either way its slot stays reserved.
    skip: bool = False


class SeriesEditIn(Strict):
    base_version: int = Field(gt=0)
    scope: Literal["series", "future"]
    #: For "future": the original slot date of the first occurrence to change.
    cutoff: date_ | None = None
    #: The series definition as it should be.
    definition: TaskFields


class SeriesDeleteIn(Strict):
    base_version: int = Field(gt=0)
    scope: Literal["series", "future"]
    cutoff: date_ | None = None


class PreservedOut(BaseModel):
    task: TaskOut
    #: "history" (started or finished) or "modified" (edited on its own).
    reason: str


class SeriesChangeOut(BaseModel):
    scope: Literal["occurrence", "future", "series"]
    series: TaskOut | None
    successor: TaskOut | None
    occurrence: TaskOut | None
    updated: list[TaskOut]
    superseded: list[TaskOut]
    preserved: list[PreservedOut]
    problems: list[SlotProblemOut]
    explanation: str


class RescheduleOut(BaseModel):
    #: The moved placement's tombstone: its original planned values, removal_reason "rescheduled",
    #: superseded_by_id = replacement.id.
    previous: PlacementOut
    replacement: PlacementOut
    #: The never-started execution of the previous placement that the move cancelled, if any.
    cancelled_execution_id: str | None = None


class CsvResultOut(BaseModel):
    #: False for a preview: nothing was written.
    applied: bool
    format_version: int
    #: Per record kind (project, task, fixed_block, placement): how many records were / would be ...
    created: dict[str, int]
    updated: dict[str, int]
    deleted: dict[str, int]
    unchanged: dict[str, int]


class CapabilitiesOut(BaseModel):
    profile: Literal["hosted", "local"]
    #: Where accepted changes are stored: "server" (persisted directly) or "device" (this device's database).
    persistence: Literal["server", "device"]
    #: Whether this profile can know about changes waiting on some device to be synchronized. The hosted server
    #: cannot see a device's unsynchronized changes, so it never reports any.
    reports_device_pending_changes: bool
    engines: list[EngineOut]
    generation_modes: list[Literal["full", "incremental"]]
    max_range_days: int
    csv_format_version: int
    max_csv_bytes: int
    auth: dict[str, bool]
    extra: dict[str, Any] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# Conversions
# -----------------------------------------------------------------------------


def _meta(record) -> dict:
    return {"id": record.id, "version": record.version, "created_at": record.created_at,
            "updated_at": record.updated_at, "deleted_at": record.deleted_at}


def task_out(task: Task) -> TaskOut:
    return TaskOut.model_validate({**task.model_dump(include=set(TaskFields.model_fields)), **_meta(task)})


def project_out(project: Project) -> ProjectOut:
    return ProjectOut.model_validate({"name": project.name, "description": project.description, **_meta(project)})


def block_out(block: FixedBlock) -> FixedBlockOut:
    return FixedBlockOut.model_validate({**block.model_dump(include={
        "label", "category", "planned_date", "timezone", "planned_start", "planned_end"}), **_meta(block)})


def placement_out(placement: ScheduledTask) -> PlacementOut:
    return PlacementOut.model_validate({**placement.model_dump(include={
        "task_id", "planned_date", "timezone", "planned_start", "planned_end", "score", "optimization_metadata",
        "task_category", "removal_reason", "superseded_by_id", "origin", "preserved"}), **_meta(placement)})


def problem_out(problem: workflow.PlacementProblem) -> PlacementProblemOut:
    return PlacementProblemOut(placement_id=problem.placement_id, task_id=problem.task_id, date=problem.date,
                               reason=problem.reason, explanation=problem.explanation, kept_as=problem.kept_as,
                               blocking=problem.blocking, remedies=list(problem.remedies))


def preference_out(record: PreferenceRecord) -> PreferenceOut:
    return PreferenceOut.model_validate({"scope": record.scope.value, "date": record.date,
                                         "overrides": record.overrides, **_meta(record)})


def day_state_out(state: workflow.DayFreshness) -> DayStateOut:
    record = state.record
    return DayStateOut(
        date=state.date, status=state.status.value,
        stale_reason=state.stale_reason.value if state.stale_reason else None,
        generated_at=record.generated_at if record else None, engine_mode=record.engine_mode if record else None,
        range_start=record.range_start if record else None, range_end=record.range_end if record else None,
        placement_count=len(state.placements), unscheduled_count=record.unscheduled_count if record else None,
        total_score=record.total_score if record else None,
    )


def _unallocated(allocation: AllocationResult) -> list[UnallocatedOut]:
    return [UnallocatedOut(task_id=entry.task_id, reason_code=entry.reason_code.value, explanation=entry.explanation,
                           required=entry.required, proven_infeasible=entry.proven_infeasible)
            for entry in allocation.unallocated]


def reset_preview_out(preview: ResetPreview) -> ResetPreviewOut:
    return ResetPreviewOut(
        start_date=preview.start_date, end_date=preview.end_date, counts=preview.counts,
        placement_ids=preview.placement_ids, cascade_placement_ids=preview.cascade_placement_ids,
        generation_ids=preview.generation_ids, fixed_block_ids=preview.fixed_block_ids, task_ids=preview.task_ids,
        date_preference_ids=preview.date_preference_ids, placements_with_history_ids=preview.placements_with_history_ids,
        tasks_with_history_ids=preview.tasks_with_history_ids,
        protected_recurring_task_ids=preview.protected_recurring_task_ids,
        blocking_dependents=preview.blocking_dependents, blocked=preview.blocked, token=preview.token,
    )


# -----------------------------------------------------------------------------
# Errors: the planning layer's structured failures, as the API's error shape
# -----------------------------------------------------------------------------


def api_error(error: Exception) -> ApiError:
    if isinstance(error, ApiError):
        return error
    if isinstance(error, GenerationLimitError):
        return ApiError(503, "generation_limit", str(error))
    if isinstance(error, StaleInputsError):
        return ApiError(409, "inputs_changed", str(error), expected_fingerprint=error.expected,
                        current_fingerprint=error.current)
    if isinstance(error, HistoryProtectedError):
        return ApiError(409, "history_protected", str(error), reason=error.status)
    if isinstance(error, RescheduleRejectedError):
        return ApiError(409, "reschedule_rejected", str(error), reason=error.problems[0].reason, problems=[
            {"location": ["destination", problem.reason], "message": problem.explanation}
            for problem in error.problems])
    if isinstance(error, RegenerationRequiredError):
        return ApiError(409, "regenerate_required", str(error), problems=[
            problem_out(p).model_dump(mode="json") for p in error.problems])
    if isinstance(error, MandatoryTaskSchedulingError):
        return ApiError(422, "generation_failed", str(error), date=getattr(error, "failed_date", None), failures=[
            {"task_id": f.task_id, "reason_code": f.reason_code.value, "explanation": f.explanation,
             "proven_infeasible": f.proven_infeasible} for f in error.failures])
    if isinstance(error, (UnsupportedSchedulingWindowError, AmbiguousLocalTimeError)):
        return ApiError(422, "unsupported_day_window", str(error))
    if isinstance(error, CsvImportError):
        return ApiError(422, "invalid_csv", str(error), problems=[
            {"line": issue.line, "message": issue.message} for issue in error.issues])
    if isinstance(error, FixedBlockRuleViolation):
        if error.code == "overlap":
            return ApiError(409, "fixed_block_overlap", str(error))
        return ApiError(422, "validation_error", str(error), reason=error.code)
    if isinstance(error, VersionConflictError):
        return ApiError(409, "deleted" if error.deleted else "version_conflict", str(error),
                        supplied_version=error.expected_version, current_version=error.current_version)
    if isinstance(error, EntityInUseError):
        return ApiError(409, "in_use", str(error), dependent_ids=error.dependent_ids)
    if isinstance(error, DuplicateEntityError):
        return ApiError(409, "already_exists", str(error))
    if isinstance(error, EntityNotFoundError):
        return ApiError(404, "not_found", str(error))
    if isinstance(error, InvalidReferenceError):
        return ApiError(422, "invalid_reference", str(error))
    if isinstance(error, ScopeError):
        return ApiError(422, "out_of_scope", str(error))
    if isinstance(error, RecurrenceLimitError):
        return ApiError(422, "recurrence_limit", str(error))
    if isinstance(error, SeriesConfigurationError):
        return ApiError(422, "series_configuration", str(error))
    if isinstance(error, (InvalidEntityError, PlanningError)):
        return ApiError(422, "validation_error", str(error))
    if isinstance(error, ValueError):  # the day engine's own input refusals (cycles, invalid blocks)
        return ApiError(422, "generation_failed", str(error))
    raise error


def _run(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except Exception as error:  # noqa: BLE001 - every planning failure becomes the API's structured error
        raise api_error(error) from None


# -----------------------------------------------------------------------------
# Operations (shared by the hosted and the local profile)
# -----------------------------------------------------------------------------


def snapshot(service: PlanningService, query: RangeIn) -> SnapshotOut:
    scope = RangeScope(query.scope)
    loaded = service.load_range(query.start_date, query.end_date, scope=scope, timezone_name=query.timezone)
    tasks = dict(loaded.tasks.tasks)
    placements = [p for day in workflow.range_dates(query.start_date, query.end_date) for p in loaded.placements_by_date[day]]
    tasks.update(service.get_tasks_including_deleted(p.task_id for p in placements if p.task_id not in tasks))
    project_ids = {task.project_id for task in tasks.values() if task.project_id is not None}
    projects = [project for project in service.list_projects(include_deleted=True) if project.id in project_ids]
    freshness = workflow.day_freshness(service, workflow.range_dates(query.start_date, query.end_date), query.timezone)
    ordered = sorted(tasks.values(), key=lambda task: (task.created_at, str(task.id)))
    return SnapshotOut(
        start_date=query.start_date, end_date=query.end_date, timezone=query.timezone, scope=query.scope,
        tasks=[task_out(task) for task in ordered], projects=[project_out(p) for p in projects],
        fixed_blocks=[block_out(b) for day in freshness for b in loaded.fixed_blocks_by_date[day]],
        placements=[placement_out(p) for p in placements],
        days=[day_state_out(state) for state in freshness.values()],
        preserved_placement_ids=sorted(service.preserved_placement_ids(placements), key=str),
    )


def preferences(service: PlanningService, query: RangeIn) -> PreferencesOut:
    views = workflow.preference_views(service, query.start_date, query.end_date, query.timezone)
    days = [
        DayPreferenceViewOut(date=view.date, effective=view.effective, inherited=view.inherited,
                             date_layer_id=view.date_layer.id if view.date_layer else None)
        for view in views.days.values()
    ]
    return PreferencesOut(
        timezone=query.timezone, template=views.template or PreferenceOverrides(),
        user_layer=preference_out(views.user_layer) if views.user_layer else None,
        date_layers=[preference_out(view.date_layer) for view in views.days.values() if view.date_layer],
        days=days, engines=[EngineOut(mode=mode, description=text) for mode, text in ENGINES.items()],
    )


def allocation_preview(service: PlanningService, query: RangeIn) -> AllocationPreviewOut:
    preview = workflow.preview_allocation(service, query.start_date, query.end_date, scope=RangeScope(query.scope),
                                          timezone_name=query.timezone)
    allocation = preview.allocation
    return AllocationPreviewOut(
        start_date=query.start_date, end_date=query.end_date, timezone=query.timezone, scope=query.scope,
        fingerprint=preview.fingerprint,
        assignments=[AssignmentOut(task_id=task_id, date=day) for task_id, day in
                     sorted(allocation.assignments.items(), key=lambda item: (item[1], str(item[0])))],
        unallocated=_unallocated(allocation),
        capacity=[CapacityOut(date=day, remaining_minutes=minutes)
                  for day, minutes in sorted(allocation.capacity_remaining.items())],
        diagnostics=[diagnostic.message for diagnostic in allocation.diagnostics],
        days=[day_state_out(state) for state in preview.freshness.values()],
    )


def generate(service: PlanningService, request: GenerateIn, clock, *, deadline: float | None = None) -> GenerateOut:
    outcome = workflow.generate(
        service, range_start=request.start_date, range_end=request.end_date, generate_start=request.generate_start,
        generate_end=request.generate_end, scope=RangeScope(request.scope), timezone_name=request.timezone,
        mode=workflow.GenerationMode(request.mode),
        expected_fingerprint=request.expected_fingerprint, clock=clock, deadline=deadline,
    )
    current = outcome.status == "already_current"
    records = service.generation_records(min(outcome.outputs), max(outcome.outputs)) if current else {}
    days = []
    for day, output in sorted(outcome.outputs.items()):
        stored = service.placements_for_date(day) if not current else output.placements
        record = records.get(day)
        days.append(GeneratedDayOut(
            date=day, engine_mode=record.engine_mode if record else outcome.inputs.preferences_by_date[day].optimizer_mode,
            placements=[placement_out(p) for p in stored], kept_placement_ids=outcome.kept_ids.get(day, []),
            unscheduled=None if current else [
                UnscheduledOut(task_id=e.task_id, reason_code=e.reason_code.value, explanation=e.explanation)
                for e in output.unscheduled],
            unscheduled_count=(record.unscheduled_count if record else None) if current else len(output.unscheduled),
            total_score=output.total_score,
            evaluation=dataclasses.asdict(outcome.evaluations[day]) if day in outcome.evaluations else None,
        ))
    reschedule = outcome.reschedule
    return GenerateOut(
        status=outcome.status, mode=request.mode, fingerprint=outcome.inputs.fingerprint, days=days,
        unallocated=_unallocated(outcome.allocation),
        superseded_placement_ids=reschedule.superseded_ids if reschedule else [],
        history_protected_placement_ids=reschedule.history_protected_ids if reschedule else [],
        removed_with_history_placement_ids=reschedule.replacement.removed_with_history_ids if reschedule else [],
        kept_elsewhere=[KeptElsewhereOut(task_id=task_id, placement_id=p.id, date=p.planned_date)
                        for task_id, p in sorted(outcome.kept_elsewhere.items(), key=lambda item: str(item[0]))],
        notices=[problem_out(problem) for day in sorted(outcome.notices) for problem in outcome.notices[day]],
    )


def reschedule(
    service: PlanningService,
    placement_id: uuid.UUID,
    request: RescheduleIn,
    *,
    at: datetime | None = None,
    task_category: str | None = None,
    snapshot: dict | None = None,
) -> PlacementReschedule:
    """
    workflow.reschedule_placement with the API's errors. A 409 (stale or
    tombstoned placement, protected history, rejected destination) carries
    the placement as it is stored now in `current` -- the read to reconcile
    with: after a lost response, a tombstone whose superseded_by_id is the
    client's replacement_id is the client's own move.
    """
    try:
        return workflow.reschedule_placement(
            service, placement_id, expected_version=request.base_version, planned_date=request.planned_date,
            timezone_name=request.timezone, planned_start=request.planned_start, planned_end=request.planned_end,
            replacement_id=request.replacement_id, task_category=task_category, at=at, snapshot=snapshot,
        )
    except Exception as error:  # noqa: BLE001 - every planning failure becomes the API's structured error
        failure = api_error(error)
        if failure.status == 409 and "current" not in failure.details:
            stored = service.get_placement(placement_id, include_deleted=True)
            if stored is not None:
                failure.details["current"] = placement_out(stored).model_dump(mode="json")
        raise failure from None


def _slot_problems(problems) -> list[SlotProblemOut]:
    return [SlotProblemOut(series_id=p.series_id, slot=p.slot, code=p.code, message=p.message) for p in problems]


def expansion_out(result: series_ops.ExpansionResult) -> ExpansionOut:
    return ExpansionOut(
        start_date=result.start_date, end_date=result.end_date, created=[task_out(task) for task in result.created],
        existing_count=result.existing_count, needs_configuration=result.needs_configuration,
        problems=_slot_problems(result.problems), warnings=_slot_problems(result.warnings),
        legacy_collisions=[LegacyCollisionOut(series_id=c.series_id, slot=c.slot, placement_ids=c.placement_ids)
                           for c in result.legacy_collisions],
    )


def series_change_out(change: series_ops.SeriesChange) -> SeriesChangeOut:
    def one(task):
        return task_out(task) if task is not None else None

    return SeriesChangeOut(
        scope=change.scope.value, series=one(change.series), successor=one(change.successor),
        occurrence=one(change.occurrence), updated=[task_out(task) for task in change.updated],
        superseded=[task_out(task) for task in change.superseded],
        preserved=[PreservedOut(task=task_out(item.task), reason=item.reason) for item in change.preserved],
        problems=_slot_problems(change.problems), explanation=change.explanation(),
    )


def _edited(stored: Task | None, fields: TaskFields, record_id: uuid.UUID) -> Task:
    """`stored` with the request's content; recurrence fields the request omits keep their stored values."""
    if stored is None:
        raise EntityNotFoundError("task", record_id)
    sent = fields.model_dump(include=fields.model_fields_set & set(TaskFields.model_fields))
    try:
        return Task.model_validate({**stored.model_dump(), **sent})
    except ValueError as error:
        raise ApiError(422, "validation_error", _first_message(error)) from None


def edit_occurrence_op(service: PlanningService, task_id: uuid.UUID, body: OccurrenceEditIn) -> SeriesChangeOut:
    stored = service.get_task(task_id)
    change = series_ops.edit_occurrence(service, _edited(stored, body.task, task_id), expected_version=body.base_version)
    return series_change_out(change)


def edit_series_op(service: PlanningService, series_id: uuid.UUID, body: SeriesEditIn) -> SeriesChangeOut:
    stored = service.get_task(series_id)
    change = series_ops.edit_series(service, _edited(stored, body.definition, series_id),
                                    expected_version=body.base_version, scope=series_ops.EditScope(body.scope),
                                    cutoff=body.cutoff)
    return series_change_out(change)


def reschedule_out(result: PlacementReschedule) -> RescheduleOut:
    return RescheduleOut(previous=placement_out(result.previous), replacement=placement_out(result.replacement),
                         cancelled_execution_id=result.cancelled_execution_id)


def _csv_text(content: bytes) -> str:
    if len(content) > MAX_CSV_BYTES:
        raise ApiError(413, "too_large", f"A CSV upload may be at most {MAX_CSV_BYTES} bytes.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ApiError(422, "invalid_csv", "The file is not UTF-8 text.") from None
    if not is_canonical_csv(text):
        raise ApiError(422, "invalid_csv", f"Only the canonical planning CSV (format version {FORMAT_VERSION}) with "
                                           "record ids can be uploaded here.")
    return text


def import_csv(service: PlanningService, content: bytes, *, allow_updates: bool, apply: bool) -> CsvResultOut:
    """Parse and validate the whole file, then apply it in one transaction -- or, for a preview, roll it back."""
    batch = parse_canonical_csv(_csv_text(content))
    if apply:
        result = service.apply_record_batch(batch, allow_updates=allow_updates)
    else:
        result = service.preview_record_batch(batch, allow_updates=allow_updates)
    return CsvResultOut(applied=apply, format_version=FORMAT_VERSION, created=result.created, updated=result.updated,
                        deleted=result.deleted, unchanged=result.unchanged)


def export_csv(service: PlanningService, start: date_ | None, end: date_ | None, include_deleted: bool, tz: str) -> Response:
    if (start is None) != (end is None):
        raise ApiError(422, "validation_error", "Pass both start_date and end_date, or neither.")
    if start is not None:
        workflow_range_error(start, end)
    buffer = io.StringIO()
    write_planning_csv(service, buffer, start_date=start, end_date=end, include_deleted=include_deleted, timezone_name=tz)
    name = "planning.csv" if start is None else f"planning-{start.isoformat()}-{end.isoformat()}.csv"
    return Response(buffer.getvalue().encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# -----------------------------------------------------------------------------
# The router
# -----------------------------------------------------------------------------


def _range_query(
    start_date: date_ = Query(), end_date: date_ = Query(), timezone: str = Query(),
    scope: Literal["planned", "eligible"] = Query(default="planned"),
) -> RangeIn:
    try:
        return RangeIn(start_date=start_date, end_date=end_date, timezone=timezone, scope=scope)
    except ValueError as error:
        raise ApiError(422, "validation_error", _first_message(error)) from None


def _first_message(error: ValueError) -> str:
    errors = getattr(error, "errors", None)
    return str(errors()[0].get("msg", error)) if callable(errors) and errors() else str(error)


def build_planning_router(
    get_context: Callable[..., PlanningContext], capabilities: Callable[[Request], CapabilitiesOut]
) -> APIRouter:
    router = APIRouter(prefix="/planning", tags=["planning"])

    @router.get("/capabilities", response_model=CapabilitiesOut, operation_id="planning_capabilities")
    def get_capabilities(request: Request) -> CapabilitiesOut:
        return capabilities(request)

    @router.get("/snapshot", response_model=SnapshotOut, operation_id="planning_snapshot",
                summary="A bounded date range: tasks, fixed blocks, placements and per-date freshness.")
    def get_snapshot(query: RangeIn = Depends(_range_query), context: PlanningContext = Depends(get_context)):
        return _run(lambda: snapshot(context.service, query))

    @router.get("/preferences", response_model=PreferencesOut, operation_id="planning_preferences",
                summary="Effective and inherited preferences per date, the editable layers, and the engines.")
    def get_preferences(query: RangeIn = Depends(_range_query), context: PlanningContext = Depends(get_context)):
        return _run(lambda: preferences(context.service, query))

    @router.post("/allocation/preview", response_model=AllocationPreviewOut, operation_id="planning_allocation_preview",
                 summary="Allocate a range from persisted inputs. Never generates or saves placements.")
    def post_allocation_preview(query: RangeIn, context: PlanningContext = Depends(get_context)):
        return _run(lambda: allocation_preview(context.service, query))

    @router.post("/generate", response_model=GenerateOut, operation_id="planning_generate",
                 summary="Generate and save selected dates (full or incremental); already_current writes nothing.")
    def post_generate(body: GenerateIn, request: Request, context: PlanningContext = Depends(get_context)):
        limit = getattr(getattr(request.app.state, "settings", None), "generation_time_limit_seconds", None)
        deadline = time.monotonic() + limit if limit else None
        return _run(lambda: generate(context.service, body, request.app.state.clock, deadline=deadline))

    @router.post("/reset/preview", response_model=ResetPreviewOut, operation_id="planning_reset_preview",
                 summary="What resetting a date range would delete, including disclosed cascades. Writes nothing.")
    def post_reset_preview(body: ResetRangeIn, context: PlanningContext = Depends(get_context)):
        return _run(lambda: reset_preview_out(context.service.reset_preview(body.start_date, body.end_date)))

    @router.post("/reset", response_model=ResetResultOut, operation_id="planning_reset",
                 summary="Apply a previewed reset atomically (the preview's token confirms it).")
    def post_reset(body: ResetCommitIn, context: PlanningContext = Depends(get_context)):
        def run() -> ResetResultOut:
            result = context.service.reset_range(body.start_date, body.end_date, confirmation=body.confirmation)
            return ResetResultOut(start_date=result.start_date, end_date=result.end_date, deleted=result.deleted,
                                  placements_with_history_ids=result.placements_with_history_ids,
                                  protected_recurring_task_ids=result.protected_recurring_task_ids)

        return _run(run)

    @router.post("/placements/{placement_id}/reschedule", response_model=RescheduleOut,
                 operation_id="planning_reschedule_placement",
                 summary="Move one placement that has not been started; validated and atomic (no regeneration).")
    def post_reschedule(placement_id: uuid.UUID, body: RescheduleIn, context: PlanningContext = Depends(get_context)):
        return reschedule_out(reschedule(context.service, placement_id, body))

    @router.post("/placements/{placement_id}/release", response_model=PlacementOut,
                 operation_id="planning_release_manual_placement",
                 summary="Release a placement's manual intent: it stays where it is, but generation may replace it.")
    def post_release(placement_id: uuid.UUID, body: ReleaseIn, context: PlanningContext = Depends(get_context)):
        return _run(lambda: placement_out(context.service.release_manual_placement(
            placement_id, expected_version=body.base_version)))

    @router.post("/recurrence/expand", response_model=ExpansionOut, operation_id="planning_expand_recurrence",
                 summary="Materialize the recurring occurrences of a bounded range (idempotent; generation does it too).")
    def post_expand(body: ExpandIn, context: PlanningContext = Depends(get_context)):
        return _run(lambda: expansion_out(series_ops.expand_occurrences(context.service, body.start_date, body.end_date)))

    @router.post("/occurrences/{task_id}/edit", response_model=SeriesChangeOut, operation_id="planning_edit_occurrence",
                 summary="Change one occurrence of a series on its own (it becomes modified; its slot stays).")
    def post_edit_occurrence(task_id: uuid.UUID, body: OccurrenceEditIn,
                             context: PlanningContext = Depends(get_context)):
        return _run(lambda: edit_occurrence_op(context.service, task_id, body))

    @router.post("/occurrences/{task_id}/delete", response_model=SeriesChangeOut,
                 operation_id="planning_delete_occurrence",
                 summary="Skip or delete one occurrence; its slot stays reserved and is never generated again.")
    def post_delete_occurrence(task_id: uuid.UUID, body: OccurrenceDeleteIn,
                               context: PlanningContext = Depends(get_context)):
        return _run(lambda: series_change_out(series_ops.delete_occurrence(
            context.service, task_id, expected_version=body.base_version, skip=body.skip)))

    @router.post("/series/{series_id}/edit", response_model=SeriesChangeOut, operation_id="planning_edit_series",
                 summary="Change a series from an occurrence on (a split with lineage) or entirely; history is kept.")
    def post_edit_series(series_id: uuid.UUID, body: SeriesEditIn, context: PlanningContext = Depends(get_context)):
        return _run(lambda: edit_series_op(context.service, series_id, body))

    @router.post("/series/{series_id}/delete", response_model=SeriesChangeOut, operation_id="planning_delete_series",
                 summary="Delete a series from an occurrence on, or entirely; started, finished and edited "
                         "occurrences are kept.")
    def post_delete_series(series_id: uuid.UUID, body: SeriesDeleteIn, context: PlanningContext = Depends(get_context)):
        return _run(lambda: series_change_out(series_ops.delete_series(
            context.service, series_id, expected_version=body.base_version, scope=series_ops.EditScope(body.scope),
            cutoff=body.cutoff)))

    @router.get("/analytics/schedule-cohort", response_model=ScheduleCohortReport,
                operation_id="planning_schedule_cohort",
                summary="Planned-versus-actual report of a local date range as of a cutoff (read-only).")
    def get_schedule_cohort(
        request: Request,
        start_date: date_ = Query(), end_date: date_ = Query(),
        timezone: str = Query(description="The IANA reporting timezone of the dates."),
        as_of: datetime | None = Query(default=None, description="Aware cutoff instant; default: now."),
        context: PlanningContext = Depends(get_context),
    ):
        now = request.app.state.clock()
        try:
            return read_schedule_cohort_report(
                context.service, start_date=start_date, end_date=end_date, timezone_name=_timezone(timezone),
                as_of=as_of or now, now=now,
            )
        except ValueError as error:  # an invalid range, timezone or cutoff
            raise ApiError(422, "validation_error", _first_message(error)) from None

    csv_body = Body(media_type="text/csv", description="A canonical planning CSV (format version 2), UTF-8.")

    @router.post("/csv/preview", response_model=CsvResultOut, operation_id="planning_csv_preview",
                 summary="Validate a canonical CSV against what is stored and report what it would change.")
    def post_csv_preview(content: bytes = csv_body, allow_updates: bool = Query(default=False),
                         context: PlanningContext = Depends(get_context)):
        return _run(lambda: import_csv(context.service, content, allow_updates=allow_updates, apply=False))

    @router.post("/csv/import", response_model=CsvResultOut, operation_id="planning_csv_import",
                 summary="Import a canonical CSV: the whole file or nothing.")
    def post_csv_import(content: bytes = csv_body, allow_updates: bool = Query(default=False),
                        context: PlanningContext = Depends(get_context)):
        return _run(lambda: import_csv(context.service, content, allow_updates=allow_updates, apply=True))

    @router.get("/csv/export", operation_id="planning_csv_export", response_class=Response,
                responses={200: {"content": {"text/csv": {}}, "description": "The canonical planning CSV."}},
                summary="Download the canonical CSV of a range (both dates) or of everything.")
    def get_csv_export(
        start_date: date_ | None = Query(default=None), end_date: date_ | None = Query(default=None),
        include_deleted: bool = Query(default=False), timezone: str = Query(default="UTC"),
        context: PlanningContext = Depends(get_context),
    ):
        return _run(lambda: export_csv(context.service, start_date, end_date, include_deleted, _timezone(timezone)))

    return router


# -----------------------------------------------------------------------------
# The hosted profile
# -----------------------------------------------------------------------------


def hosted_context_dependency():
    """The hosted profile's context: a PostgreSQL-backed PlanningService for the authenticated user."""
    from backend.api import current_user_id, get_session
    from backend.planning_repository import ServerPlanningRepository

    def hosted_context(
        request: Request, user_id: uuid.UUID = Depends(current_user_id), session: Session = Depends(get_session)
    ) -> PlanningContext:
        clock = request.app.state.clock
        return PlanningContext(
            service=PlanningService(ServerPlanningRepository(session, user_id, clock), clock), profile="hosted"
        )

    return hosted_context


def hosted_capabilities(_request: Request) -> CapabilitiesOut:
    return CapabilitiesOut(
        profile="hosted", persistence="server", reports_device_pending_changes=False,
        engines=[EngineOut(mode=mode, description=text) for mode, text in ENGINES.items()],
        generation_modes=["full", "incremental"], max_range_days=workflow.MAX_RANGE_DAYS,
        csv_format_version=FORMAT_VERSION, max_csv_bytes=MAX_CSV_BYTES,
        auth={"browser_sessions": True, "bearer_tokens": True, "registration": True},
        extra={"note": "Changes are saved on the server directly. Changes still waiting on a device reach the server "
                       "only when that device synchronizes; this server cannot see them.",
               "recurrence": {"expansion": True, "max_range_days": series_ops.MAX_EXPANSION_DAYS,
                              "max_occurrences": series_ops.MAX_EXPANSION_OCCURRENCES}},
    )
