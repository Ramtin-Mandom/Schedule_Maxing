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

import io
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.optimizer import MandatoryTaskSchedulingError
from app.planning import workflow
from app.planning.allocation import AllocationResult
from app.planning.application import PlanningService, RangeScope, ResetPreview
from app.planning.csv_canonical import is_canonical_csv, parse_canonical_csv
from app.planning.csv_export import FORMAT_VERSION, write_planning_csv
from app.planning.csv_import import CsvImportError
from app.planning.errors import (
    DuplicateEntityError,
    EntityInUseError,
    EntityNotFoundError,
    InvalidEntityError,
    InvalidReferenceError,
    PlanningError,
    RegenerationRequiredError,
    ScopeError,
    StaleInputsError,
    VersionConflictError,
)
from app.planning.fixed_block_rules import FixedBlockRuleViolation
from app.planning.models import FixedBlock, Project, ScheduledTask, Task
from app.planning.preferences import (
    DayPreferences,
    OptimizerMode,
    PreferenceOverrides,
    PreferenceRecord,
    resolve_day_preferences,
)
from app.planning.time import AmbiguousLocalTimeError, UnsupportedSchedulingWindowError, validate_timezone
from backend.errors import ApiError
from backend.resources import FixedBlockOut, PlacementOut, PreferenceOut, ProjectOut, TaskFields, TaskOut

#: The largest canonical CSV one upload may carry.
MAX_CSV_BYTES = 5 * 1024 * 1024

ENGINES = {
    OptimizerMode.PRECISE_GREEDY: "Places each task at the best-scoring minute; the protected greedy baseline.",
    OptimizerMode.ADHD_FRIENDLY: "Starts tasks over 30 minutes on quarter hours and rewards filling short gaps.",
}


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


class GenerateOut(BaseModel):
    #: generated: saved now; already_current: the saved schedule already matched, nothing was written.
    status: Literal["generated", "already_current"]
    mode: Literal["full", "incremental"]
    fingerprint: str
    days: list[GeneratedDayOut]
    unallocated: list[UnallocatedOut]
    #: Placements outside the generated dates that this run replaced (the same occurrence).
    superseded_placement_ids: list[uuid.UUID]
    #: Placements outside the generated dates kept because their execution started or finished.
    history_protected_placement_ids: list[uuid.UUID]
    #: Replaced placements that execution history references (the history itself is kept).
    removed_with_history_placement_ids: list[uuid.UUID]


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
        "task_id", "planned_date", "timezone", "planned_start", "planned_end", "score", "optimization_metadata"}),
        **_meta(placement)})


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
    if isinstance(error, StaleInputsError):
        return ApiError(409, "inputs_changed", str(error), expected_fingerprint=error.expected,
                        current_fingerprint=error.current)
    if isinstance(error, RegenerationRequiredError):
        return ApiError(409, "regenerate_required", str(error), problems=[
            {"placement_id": p.placement_id, "task_id": p.task_id, "date": p.date, "reason": p.reason,
             "explanation": p.explanation} for p in error.problems])
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
    )


def preferences(service: PlanningService, query: RangeIn) -> PreferencesOut:
    dates = workflow.range_dates(query.start_date, query.end_date)
    template = service.preference_template()
    user = service.user_preferences()
    layers = service.date_preferences_for_range(query.start_date, query.end_date)
    effective = service.resolve_preferences(dates, query.timezone)
    days = [
        DayPreferenceViewOut(
            date=day, effective=effective[day],
            inherited=resolve_day_preferences(date=day, timezone=query.timezone, yaml_overrides=template,
                                              user_overrides=user.overrides if user else None),
            date_layer_id=layers[day].id if day in layers else None,
        )
        for day in dates
    ]
    return PreferencesOut(
        timezone=query.timezone, template=template or PreferenceOverrides(),
        user_layer=preference_out(user) if user else None,
        date_layers=[preference_out(layers[day]) for day in sorted(layers)], days=days,
        engines=[EngineOut(mode=mode, description=text) for mode, text in ENGINES.items()],
    )


def allocation_preview(service: PlanningService, query: RangeIn) -> AllocationPreviewOut:
    inputs = workflow.read_inputs(service, query.start_date, query.end_date, scope=RangeScope(query.scope),
                                  timezone_name=query.timezone)
    allocation = workflow.allocate(inputs)
    freshness = workflow.day_freshness(service, workflow.range_dates(query.start_date, query.end_date), query.timezone)
    return AllocationPreviewOut(
        start_date=query.start_date, end_date=query.end_date, timezone=query.timezone, scope=query.scope,
        fingerprint=inputs.fingerprint,
        assignments=[AssignmentOut(task_id=task_id, date=day) for task_id, day in
                     sorted(allocation.assignments.items(), key=lambda item: (item[1], str(item[0])))],
        unallocated=_unallocated(allocation),
        capacity=[CapacityOut(date=day, remaining_minutes=minutes)
                  for day, minutes in sorted(allocation.capacity_remaining.items())],
        diagnostics=[diagnostic.message for diagnostic in allocation.diagnostics],
        days=[day_state_out(state) for state in freshness.values()],
    )


def generate(service: PlanningService, request: GenerateIn, clock) -> GenerateOut:
    outcome = workflow.generate(
        service, range_start=request.start_date, range_end=request.end_date, generate_start=request.generate_start,
        generate_end=request.generate_end, scope=RangeScope(request.scope), timezone_name=request.timezone,
        mode=workflow.GenerationMode(request.mode), protect_history=True,
        expected_fingerprint=request.expected_fingerprint, clock=clock,
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
        ))
    reschedule = outcome.reschedule
    return GenerateOut(
        status=outcome.status, mode=request.mode, fingerprint=outcome.inputs.fingerprint, days=days,
        unallocated=_unallocated(outcome.allocation),
        superseded_placement_ids=reschedule.superseded_ids if reschedule else [],
        history_protected_placement_ids=reschedule.history_protected_ids if reschedule else [],
        removed_with_history_placement_ids=reschedule.replacement.removed_with_history_ids if reschedule else [],
    )


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


class _DryRun(Exception):
    def __init__(self, result) -> None:
        self.result = result


def import_csv(service: PlanningService, content: bytes, *, allow_updates: bool, apply: bool) -> CsvResultOut:
    """Parse and validate the whole file, then apply it in one transaction -- or, for a preview, roll it back."""
    batch = parse_canonical_csv(_csv_text(content))
    if apply:
        result = service.apply_record_batch(batch, allow_updates=allow_updates)
    else:
        try:
            with service.transaction():
                raise _DryRun(service.apply_record_batch(batch, allow_updates=allow_updates))
        except _DryRun as dry_run:
            result = dry_run.result
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
        return _run(lambda: generate(context.service, body, request.app.state.clock))

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
                       "only when that device synchronizes; this server cannot see them."},
    )
