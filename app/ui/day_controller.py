"""
app/ui/day_controller.py

The Tk-free presenter behind the desktop Day Schedule (Milestone 4,
Prompt 4). It extends SchedulePageController (one date), so the task form,
edits, removal and export are exactly the Week/Month ones, and adds what the
Day workspace shows and does -- all through PlanningController and the
shared workflow (app/planning/workflow.py), never through HTTP:

- DaySnapshot: the date's timeline (fixed blocks in their stored category,
  saved placements at their exact minutes, lanes for anything that
  overlaps), display-only free gaps inside the effective scheduling window
  (never saved), the tasks still available for the date, the persisted
  freshness (Current / Out of date and why) and the effective engine.
- Engine choice: the supported engines come from OptimizerMode and the
  shared catalog (PlanningController.engine_descriptions), with their
  labels in app.planning.preferences.ENGINE_LABELS. Choosing one saves only
  this date's optimizer_mode override (every other field of the date layer
  and the user defaults are kept); "Use default" removes just that field.
  Nothing is written at startup, so an existing preference is never
  overwritten, and Normal is only the fallback when no layer chooses.
- Make Schedule: a date with nothing saved is generated in full; a current
  date returns already_current and writes nothing; an out-of-date date with
  saved work adds new work incrementally, keeping every placement that still
  fits (ids, times, execution links). When kept work no longer fits,
  the run says which and why and nothing changes until the user asks for an
  explicit regeneration (full, history protected). A run that fails, or
  would place nothing while saved work exists, keeps the previous schedule.
  An engine change alone never triggers a regeneration.
- Unscheduled explanations are shown only when genuinely known: the reasons
  of a run made in this session, for as long as the saved record is that
  run's. After a restart only the stored count is known and is said so.
- Day Preferences (app/ui/preferences_model.py), Reset Day (previewed,
  confirmed with the preview's token) and canonical CSV v2 import with a
  preview (a legacy half-hour CSV is only imported when explicitly chosen).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from datetime import date as date_
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from app.optimizer import MandatoryTaskSchedulingError
from app.planning.application import BatchApplyResult, RangeScope, ResetPreview, task_planned_date
from app.planning.csv_canonical import is_canonical_csv
from app.planning.csv_import import ImportMode, read_csv_text
from app.planning.errors import RegenerationRequiredError, StaleInputsError
from app.planning.models import FixedBlock, ScheduledTask, Task
from app.planning.preferences import (
    ENGINE_LABELS,
    DayPreferences,
    OptimizerMode,
    PreferenceOverrides,
    resolve_day_preferences,
)
from app.planning.provenance import StaleReason
from app.planning.time import local_minutes
from app.planning.workflow import DayFreshness, Freshness, GenerationMode, GenerationOutcome, PreferenceViews
from app.ui import preferences_model as prefs
from app.ui.background import ControllerResult
from app.ui.planning_controller import PlanningController
from app.ui.schedule_page_controller import (
    ImportRun,
    PageSnapshot,
    RowRef,
    SchedulePageController,
    _batch_summary,
    _Failure,
    day_label,
)
from app.ui.time_fields import FieldError, format_clock, format_duration

MINUTES_PER_DAY = 1440

#: What each engine does in practice, for the Engine dropdown (no claims beyond the scheduling rule).
ENGINE_EXPLANATIONS: dict[OptimizerMode, str] = {
    OptimizerMode.PRECISE_GREEDY: "Tasks may start at any minute.",
    OptimizerMode.ADHD_FRIENDLY: "Tasks longer than 30 minutes start on the quarter hour (:00, :15, :30, :45); "
                                 "shorter tasks still start at any minute. Durations never change.",
    OptimizerMode.EARLY_FINISH: "Plans the same tasks as Normal, then packs them so the day finishes as early as "
                                "possible with few idle gaps. Nothing is dropped or shortened.",
    OptimizerMode.NIGHT_OWL: "Plans the same tasks as Normal, then packs them late in the day with few idle gaps. "
                             "Nothing is dropped or shortened.",
    OptimizerMode.CATCH_UP: "Gives priority to categories you skipped often in the last 90 days (at least 5 "
                            "finished or skipped tasks). Without that history it plans exactly like Normal.",
}

_STALE_TEXT = {
    StaleReason.INPUTS_CHANGED: "Tasks, fixed blocks or preferences it was made from changed since.",
    StaleReason.PLACEMENTS_CHANGED: "Its saved entries changed since it was made (for example by an import or "
                                    "a schedule made for another range).",
    StaleReason.NO_PROVENANCE: "It was saved before schedules were tracked, so what it was made from is unknown.",
    StaleReason.SUPERSEDED_ALLOCATION: "A newer allocation replaced the one it was made from.",
}


def mode_summary(evaluation, window_start_minute: int) -> str:
    """One line on what the day's mode achieved (docs/scheduling-modes.md), in words and local clock times."""
    label = ENGINE_LABELS.get(OptimizerMode(evaluation.mode), evaluation.mode)
    if evaluation.scheduled_count == 0:
        return f"{label}: nothing was scheduled, so the mode had nothing to arrange."
    parts = [f"{label}: first task at {format_clock(window_start_minute + evaluation.first_start_minute)}, "
             f"last ends {format_clock(window_start_minute + evaluation.last_finish_minute)}, "
             f"{evaluation.idle_minutes} idle minute(s) between tasks"]
    parts.append(f"baseline reward {evaluation.baseline_reward:.1f} + mode bonus {evaluation.mode_bonus:.1f}")
    return "; ".join(parts) + "."


def engine_label(mode: OptimizerMode) -> str:
    return ENGINE_LABELS.get(mode, mode.value)


@dataclass(frozen=True)
class EngineOption:
    mode: OptimizerMode
    label: str
    explanation: str


def engine_options(catalog: Mapping[OptimizerMode, str]) -> list[EngineOption]:
    """The engines the service supports (OptimizerMode members in its catalog), with their labels."""
    return [EngineOption(mode, engine_label(mode), ENGINE_EXPLANATIONS.get(mode, catalog[mode]))
            for mode in OptimizerMode if mode in catalog]


@dataclass(frozen=True)
class EngineState:
    effective: OptimizerMode
    #: What the date would use without its own override (the user default, else Normal).
    inherited: OptimizerMode
    #: True when this date's own layer chooses the engine.
    overridden: bool

    @property
    def label(self) -> str:
        return engine_label(self.effective)

    @property
    def summary(self) -> str:
        if self.overridden:
            return f"{self.label} (set for this date; default: {engine_label(self.inherited)})"
        return f"{self.label} (default)"


TimelineKind = Literal["fixed", "scheduled", "stale"]
_KIND_WORDS = {"fixed": "Fixed block", "scheduled": "Scheduled", "stale": "Scheduled (out of date)"}
#: What resolves a kept placement's problem, by why it is kept (workflow.PlacementProblem.kept_as).
_REMEDY_HINTS = {"manual": " (you placed it: move it, change the conflicting setting, or release it)"}


@dataclass(frozen=True)
class TimelineItem:
    key: str
    kind: TimelineKind
    name: str
    category: str
    #: Minutes from local midnight (the end may be 1440, the following midnight).
    start_minute: int
    end_minute: int
    #: Wall-clock text of the interval, e.g. "9:13 AM – 10:00 AM".
    time_text: str
    #: The record the item's actions (edit, remove) act on.
    ref: RowRef
    lane: int = 0
    placement_id: uuid.UUID | None = None
    #: The placement's version (the precondition of its actions).
    placement_version: int | None = None
    #: The user's manual placement: Make Schedule keeps it where it is until it is released.
    preserved: bool = False

    @property
    def duration_minutes(self) -> int:
        return self.end_minute - self.start_minute

    @property
    def description(self) -> str:
        manual = ", placed by you (Make Schedule keeps it here)" if self.preserved else ""
        return (f"{self.name}: {self.time_text} ({format_duration(max(1, self.duration_minutes))}), "
                f"{_KIND_WORDS[self.kind].lower()}, category {self.category}{manual}")


@dataclass(frozen=True)
class FreeGap:
    start_minute: int
    end_minute: int

    @property
    def text(self) -> str:
        return (f"Free {format_clock(self.start_minute)} – {format_clock(self.end_minute)} "
                f"({format_duration(self.end_minute - self.start_minute)})")


@dataclass(frozen=True)
class UnplacedTask:
    ref: RowRef
    name: str
    category: str
    duration_minutes: int
    required: bool
    #: "Wed Jun 5", or "Any date" for an undated task.
    date_text: str
    #: A genuine explanation from a run made in this session, when known.
    reason: str | None = None
    #: Dates it is already scheduled on elsewhere.
    elsewhere: tuple[date_, ...] = ()

    @property
    def text(self) -> str:
        parts = [self.name, format_duration(self.duration_minutes)]
        if self.required:
            parts.append("required")
        if self.date_text == "Any date":
            parts.append("any date")
        if self.elsewhere:
            parts.append("scheduled on " + ", ".join(day_label(day) for day in self.elsewhere))
        return " · ".join(parts)


@dataclass(frozen=True)
class DaySnapshot(PageSnapshot):
    day: date_ | None = None
    #: The effective scheduling window as local minutes, or None with window_error.
    window: tuple[int, int] | None = None
    window_error: str | None = None
    timeline: list[TimelineItem] = field(default_factory=list)
    lane_count: int = 1
    free_gaps: list[FreeGap] = field(default_factory=list)
    unplaced: list[UnplacedTask] = field(default_factory=list)
    freshness: Freshness = Freshness.NONE
    freshness_label: str = ""
    freshness_detail: str = ""
    engine: EngineState | None = None
    #: The date layer's version (the precondition for changing it); None: the date has no layer.
    preference_version: int | None = None
    #: Tasks the saved run could not place whose reasons are not stored (known only as a count).
    unexplained_count: int = 0


RunStatus = Literal["generated", "already_current", "nothing_placed", "needs_regeneration", "failed"]


@dataclass(frozen=True)
class DayRun:
    status: RunStatus
    snapshot: DaySnapshot | None
    message: str
    #: Genuine reasons of this run: required-task failures and optional tasks it could not place.
    reasons: list[str] = field(default_factory=list)
    #: Why kept work no longer fits (needs_regeneration).
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DayPreferencesView:
    day: date_
    timezone: str
    engine: EngineState
    rows: list[prefs.PreferenceRow]
    #: The date layer's version; None: the date has no layer of its own.
    layer_version: int | None


@dataclass(frozen=True)
class ResetPlan:
    preview: ResetPreview
    message: str

    @property
    def blocked(self) -> bool:
        return self.preview.blocked


@dataclass(frozen=True)
class CsvPlan:
    path: str
    #: "canonical" (format v2 with record ids) or "legacy" (the half-hour schedule CSV).
    kind: Literal["canonical", "legacy"]
    summary: str
    counts: BatchApplyResult | None = None

    @property
    def has_updates(self) -> bool:
        return self.counts is not None and bool(sum(self.counts.updated.values()) + sum(self.counts.deleted.values()))


def assign_lanes(intervals: list[tuple[int, int]]) -> list[int]:
    """A lane per interval (in the given order) so intervals sharing a lane never overlap; lane 0 first."""
    order = sorted(range(len(intervals)), key=lambda index: (intervals[index][0], intervals[index][1]))
    lane_ends: list[int] = []
    lanes = [0] * len(intervals)
    for index in order:
        start, end = intervals[index]
        for lane, lane_end in enumerate(lane_ends):
            if lane_end <= start:
                lanes[index], lane_ends[lane] = lane, end
                break
        else:
            lanes[index] = len(lane_ends)
            lane_ends.append(end)
    return lanes


def free_gaps(window: tuple[int, int], busy: list[tuple[int, int]]) -> list[FreeGap]:
    """The parts of `window` no busy interval covers (display only; nothing is ever saved for them)."""
    start, end = window
    gaps: list[FreeGap] = []
    cursor = start
    for busy_start, busy_end in sorted(busy):
        if busy_end <= cursor:
            continue
        if busy_start >= end:
            break
        if busy_start > cursor:
            gaps.append(FreeGap(cursor, min(busy_start, end)))
        cursor = max(cursor, busy_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append(FreeGap(cursor, end))
    return gaps


def _wall_minutes(instant: datetime, day: date_, tz_name: str) -> int:
    """The wall-clock minute of `instant` on `day` (1440: the following midnight) -- for text only."""
    local = instant.astimezone(ZoneInfo(tz_name))
    if local.date() > day:
        return MINUTES_PER_DAY
    if local.date() < day:
        return 0
    return local.hour * 60 + local.minute


def _interval_text(start: datetime, end: datetime, day: date_, tz_name: str) -> str:
    return f"{format_clock(_wall_minutes(start, day, tz_name))} – {format_clock(_wall_minutes(end, day, tz_name))}"


class DayScheduleController(SchedulePageController):
    """One date's workspace (see the module docstring)."""

    def __init__(self, planning: PlanningController, *, anchor_date: date_, timezone: str,
                 today: Callable[[], date_] | None = None) -> None:
        super().__init__(planning, number_of_days=1, anchor_date=anchor_date, timezone=timezone)
        self._today = today
        self._explanations_lock = threading.Lock()
        #: (date, generation record id) -> task id -> genuine reason of the run that saved that record.
        self._explanations: dict[tuple[date_, uuid.UUID], dict[uuid.UUID, str]] = {}

    @property
    def day(self) -> date_:
        return self._anchor

    def today(self, now: datetime | None = None) -> date_:
        """Today's real date in the planning timezone (or the injected `today` provider's date)."""
        if now is None and self._today is not None:
            return self._today()
        return (now or datetime.now(ZoneInfo(self.timezone))).astimezone(ZoneInfo(self.timezone)).date()

    # ------------------------------------------------------------------
    # Engines
    # ------------------------------------------------------------------

    def engine_options(self) -> list[EngineOption]:
        return engine_options(self._planning.engine_descriptions())

    def set_engine(self, mode: OptimizerMode | str | None, *, expected_version: int | None) -> ControllerResult[DaySnapshot]:
        """
        Save only this date's engine (mode=None: remove it, so the date
        inherits the default again). Every other stored field of the date
        layer and the user defaults are kept. On a failure nothing is
        saved and the committed state is re-read.
        """
        if mode is not None:
            try:
                mode = OptimizerMode(mode)
            except ValueError:
                return self._fail_with_reload(f"{mode!r} is not a supported engine.")
            if mode not in self._planning.engine_descriptions():
                return self._fail_with_reload(f"The {engine_label(mode)} engine is not available.")
        result = self._planning.update_date_overrides(
            self._anchor, lambda overrides: overrides.model_copy(update={"optimizer_mode": mode}),
            expected_version=expected_version,
        )
        if not result.ok:
            return self._fail_with_reload(result.error)
        return self.load()

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def _snapshot(self) -> DaySnapshot:
        base = super()._snapshot()
        day = self._anchor
        planning = self._planning
        views: PreferenceViews = self._unwrap(planning.preference_views(day, day))
        view = views.days[day]
        freshness: DayFreshness = self._unwrap(planning.day_freshness([day]))[day]
        planning_range = self._unwrap(planning.load_range(day, day, scope=RangeScope.PLANNED))

        engine = self._engine_state(view.effective, view.inherited, view.date_layer)
        window, window_error = self._window(view.effective)

        blocks: list[FixedBlock] = planning_range.fixed_blocks_by_date[day]
        placements: list[ScheduledTask] = planning_range.placements_by_date[day]
        tasks: dict[uuid.UUID, Task] = dict(planning_range.tasks.tasks)
        missing = [p.task_id for p in placements if p.task_id not in tasks]
        if missing:
            tasks.update(self._unwrap(planning.get_tasks(missing)).tasks)

        preserved = self._unwrap(planning.preserved_placement_ids(placements)) if placements else set()
        items: list[TimelineItem] = []
        for block in blocks:
            start = local_minutes(block.planned_start, day, block.timezone)
            end = local_minutes(block.planned_end, day, block.timezone) or MINUTES_PER_DAY
            items.append(TimelineItem(
                key=f"block:{block.id}", kind="fixed", name=block.label, category=block.category,
                start_minute=start, end_minute=end,
                time_text=_interval_text(block.planned_start, block.planned_end, day, block.timezone),
                ref=RowRef("block", block.id, block.version),
            ))
        kind: TimelineKind = "scheduled" if freshness.status == Freshness.CURRENT else "stale"
        for placement in placements:
            task = tasks.get(placement.task_id)
            start = local_minutes(placement.planned_start, day, placement.timezone)
            end = local_minutes(placement.planned_end, day, placement.timezone) or MINUTES_PER_DAY
            items.append(TimelineItem(
                key=f"placement:{placement.id}", kind=kind, name=task.name if task else "(removed task)",
                category=task.category if task else "other", start_minute=start, end_minute=end,
                time_text=_interval_text(placement.planned_start, placement.planned_end, day, placement.timezone),
                ref=RowRef("task", placement.task_id, task.version if task else None), placement_id=placement.id,
                placement_version=placement.version, preserved=placement.id in preserved,
            ))
        items.sort(key=lambda item: (item.start_minute, item.end_minute, item.key))
        lanes = assign_lanes([(item.start_minute, item.end_minute) for item in items])
        items = [TimelineItem(**{**{f.name: getattr(item, f.name) for f in fields(TimelineItem)}, "lane": lane})
                 for item, lane in zip(items, lanes)]

        gaps = free_gaps(window, [(item.start_minute, item.end_minute) for item in items]) if window else []
        record = freshness.record
        with self._explanations_lock:
            known = self._explanations.get((day, record.id)) if record is not None else None
        unplaced = self._unplaced(planning_range.task_ids, tasks, placements, known or {})
        unexplained = record.unscheduled_count if record is not None and known is None else 0

        label, detail = self._freshness_text(freshness, engine)
        base_fields = {f.name: getattr(base, f.name) for f in fields(PageSnapshot)}
        base_fields["status_text"] = f"{label}. {detail}"
        return DaySnapshot(
            **base_fields, day=day, window=window, window_error=window_error, timeline=items,
            lane_count=max(lanes, default=0) + 1, free_gaps=gaps, unplaced=unplaced, freshness=freshness.status,
            freshness_label=label, freshness_detail=detail, engine=engine,
            preference_version=view.date_layer.version if view.date_layer is not None else None,
            unexplained_count=unexplained,
        )

    @staticmethod
    def _engine_state(effective: DayPreferences, inherited: DayPreferences, layer) -> EngineState:
        return EngineState(
            effective=effective.optimizer_mode, inherited=inherited.optimizer_mode,
            overridden=layer is not None and layer.overrides.optimizer_mode is not None,
        )

    @staticmethod
    def _window(preferences: DayPreferences) -> tuple[tuple[int, int] | None, str | None]:
        spec = preferences.day_window
        try:
            preferences.to_local_day_window().to_utc_instants()
        except ValueError as error:  # an unsupported or ambiguous window on this date
            return None, str(error)
        end = MINUTES_PER_DAY if spec.end_day_offset == 1 else spec.end_minute
        return (spec.start_minute, end), None

    def _unplaced(
        self, task_ids: list[uuid.UUID], tasks: dict[uuid.UUID, Task], placements: list[ScheduledTask],
        explanations: dict[uuid.UUID, str],
    ) -> list[UnplacedTask]:
        placed_here = {placement.task_id for placement in placements}
        candidates = [task_id for task_id in task_ids if task_id not in placed_here]
        elsewhere = self._unwrap(self._planning.active_placement_dates(candidates)) if candidates else {}
        rows: list[UnplacedTask] = []
        for task_id in candidates:
            task = tasks[task_id]
            planned = task_planned_date(task)
            other_dates = tuple(elsewhere.get(task_id, ()))
            if planned is None and other_dates:
                continue  # an undated task already scheduled on another date is not available here
            rows.append(UnplacedTask(
                ref=RowRef("task", task.id, task.version), name=task.name, category=task.category,
                duration_minutes=task.estimated_duration_minutes, required=task.required,
                date_text=day_label(planned) if planned is not None else "Any date",
                reason=explanations.get(task.id), elsewhere=other_dates,
            ))
        rows.sort(key=lambda row: (not row.required, row.date_text == "Any date", row.name.lower()))
        return rows

    @staticmethod
    def _freshness_text(freshness: DayFreshness, engine: EngineState) -> tuple[str, str]:
        if freshness.status == Freshness.NONE:
            return "Not scheduled yet", "Nothing has been generated for this date. Add tasks, then Make Schedule."
        if freshness.status == Freshness.CURRENT:
            return "Current", (f"The saved schedule matches the current tasks, fixed blocks and preferences "
                               f"({engine.label} engine).")
        detail = _STALE_TEXT.get(freshness.stale_reason, "Something it depends on changed.")
        record = freshness.record
        if record is not None and record.engine_mode != engine.effective:
            detail += (f" The engine changed from {engine_label(record.engine_mode)} to {engine.label}; "
                       "Make Schedule keeps the work that still fits.")
        return "Out of date", detail + " Run Make Schedule to update it."

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def make_schedule_for(self, day: date_) -> ControllerResult[DayRun]:
        """
        Make Schedule for `day` (captured by the caller when the run
        started): full when nothing is saved, already_current when nothing
        changed, incremental when saved work exists but is out of date.
        """
        fresh = self._planning.day_freshness([day])
        if not fresh.ok:
            return ControllerResult.failure(fresh.error, fresh.cause)
        state = fresh.value[day]
        mode = GenerationMode.INCREMENTAL if state.status == Freshness.STALE and state.placements else GenerationMode.FULL
        return self._run(day, mode)

    def make_schedule(self) -> ControllerResult[DayRun]:  # type: ignore[override] - the Day page's own result
        return self.make_schedule_for(self._anchor)

    def regenerate_for(self, day: date_) -> ControllerResult[DayRun]:
        """An explicit full regeneration of `day`: replaces saved work that has not started (history is protected)."""
        return self._run(day, GenerationMode.FULL)

    def _run(self, day: date_, mode: GenerationMode) -> ControllerResult[DayRun]:
        result = self._planning.generate(day, day, generate_start=day, generate_end=day, mode=mode,
                                         preserve_on_empty=True)
        if not result.ok:
            return self._run_failure(day, result)
        outcome: GenerationOutcome = result.value
        reasons, explanations = self._reasons(outcome, day)
        if outcome.status == "generated":
            record = next((record for record in outcome.reschedule.generations if record.planned_date == day), None)
            if record is not None:
                with self._explanations_lock:
                    self._explanations = {key: value for key, value in self._explanations.items() if key[0] != day}
                    self._explanations[(day, record.id)] = explanations
        snapshot = self._snapshot_for(day)
        if outcome.status == "already_current":
            return ControllerResult.success(DayRun(
                "already_current", snapshot, "The schedule is already current; nothing changed and nothing was saved."))
        if outcome.status == "nothing_placed":
            return ControllerResult.success(DayRun(
                "nothing_placed", snapshot,
                "Nothing could be placed on this date (no work to schedule, or none of it fits). "
                "The previous schedule was kept unchanged.", reasons))
        output = outcome.outputs[day]
        kept = len(outcome.kept_ids.get(day, []))
        placed = len(output.placements) - kept
        if mode == GenerationMode.INCREMENTAL:
            message = f"Kept {kept} scheduled task(s) in place and added {placed} new one(s)."
        else:
            message = (f"Scheduled {placed} task(s)"
                       + (f"; {kept} started, finished or manually placed task(s) kept in place" if kept else "") + ".")
        if reasons:
            message += f" {len(reasons)} task(s) could not be placed; see the reasons."
        reasons = reasons + self._kept_notes(outcome, day)
        evaluation = outcome.evaluations.get(day)
        if evaluation is not None and evaluation.mode in ("early_finish", "night_owl", "catch_up"):
            reasons.append(mode_summary(evaluation, self._anchor_timezone_day_start(day)))
        return ControllerResult.success(DayRun("generated", snapshot, message, reasons))

    def _anchor_timezone_day_start(self, day: date_) -> int:
        """The local minute the date's window starts at (evaluations count minutes from it)."""
        try:
            views = self._unwrap(self._planning.preference_views(day, day))
        except _Failure:
            return 0
        return views.days[day].effective.day_window.start_minute

    def _kept_notes(self, outcome: GenerationOutcome, day: date_) -> list[str]:
        """Work this run left alone that the user should know about: history that no longer fits, work elsewhere."""
        notices = outcome.notices.get(day, [])
        elsewhere = outcome.kept_elsewhere
        names = self._names([*(problem.task_id for problem in notices), *elsewhere])
        notes = [f"{names.get(problem.task_id, 'A task')} — kept as recorded (its work started or finished), "
                 f"although {problem.explanation}" for problem in notices]
        notes += [f"{names.get(task_id, 'A task')} — already planned on {placement.planned_date:%A, %B} "
                  f"{placement.planned_date.day}; left there (move it to plan it here)."
                  for task_id, placement in sorted(elsewhere.items(), key=lambda item: item[1].planned_date)]
        return notes

    def release_manual_placement(self, item: TimelineItem) -> ControllerResult[DaySnapshot]:
        """Release the manual intent of a timeline item's placement, then reload the date (nothing moves)."""
        if item.placement_id is None or item.placement_version is None:
            return ControllerResult.failure("Only a scheduled task can be released.")
        result = self._planning.release_manual_placement(item.placement_id, expected_version=item.placement_version)
        if not result.ok:
            return self._fail_with_reload(result.error)
        return self.load()

    def _snapshot_for(self, day: date_) -> DaySnapshot | None:
        """The page's snapshot if it still shows `day` (the date may have changed while a run was working)."""
        if day != self._anchor:
            return None
        loaded = self.load()
        return loaded.value if loaded.ok else None

    def _reasons(self, outcome: GenerationOutcome, day: date_) -> tuple[list[str], dict[uuid.UUID, str]]:
        entries = [(entry.task_id, entry.required, f"Not allocated to this date: {entry.explanation}")
                   for entry in outcome.allocation.unallocated]
        output = outcome.outputs.get(day)
        if output is not None and outcome.status != "already_current":
            required = {task_id for task_id, task in outcome.inputs.planning_range.tasks.tasks.items() if task.required}
            entries += [(entry.task_id, entry.task_id in required, f"Could not be placed: {entry.explanation}")
                        for entry in output.unscheduled]
        names = self._names([task_id for task_id, _, _ in entries])
        explanations = {task_id: text for task_id, _, text in entries}
        reasons = [f"{'Required: ' if required else ''}{names.get(task_id, 'A task')} — {text}"
                   for task_id, required, text in entries]
        return sorted(reasons, key=lambda text: not text.startswith("Required: ")), explanations

    def _run_failure(self, day: date_, result: ControllerResult) -> ControllerResult[DayRun]:
        cause = result.cause
        snapshot = self._snapshot_for(day)
        if isinstance(cause, RegenerationRequiredError):
            names = self._names([problem.task_id for problem in cause.problems])
            problems = [f"{names.get(problem.task_id, 'A removed task')} — {problem.explanation}"
                        + _REMEDY_HINTS.get(problem.kept_as, "") for problem in cause.problems]
            return ControllerResult.success(DayRun(
                "needs_regeneration", snapshot,
                "Some saved work no longer fits the current tasks, fixed blocks or engine. Nothing was changed. "
                "Regenerate to replace the work that has not started (started or finished work is kept; a task you "
                "placed yourself stays until you move it or release it).",
                problems=problems))
        if isinstance(cause, MandatoryTaskSchedulingError):
            names = self._names([failure.task_id for failure in cause.failures])
            reasons = [f"Required: {names.get(failure.task_id, 'A task')} — {failure.explanation}"
                       for failure in cause.failures]
            message = "Required tasks could not all be scheduled. Nothing was saved; the previous schedule is unchanged."
        elif isinstance(cause, StaleInputsError):
            reasons = []
            message = ("Something changed while the schedule was being made. Nothing was saved; the previous "
                       "schedule is unchanged. Try again.")
        else:
            reasons = []
            message = f"{result.error}\n\nNothing was saved; the previous schedule is unchanged."
        return ControllerResult(ok=False, value=DayRun("failed", snapshot, message, reasons), error=message, cause=cause)

    def _names(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        try:
            return self._task_names(list(dict.fromkeys(task_ids)))
        except _Failure:
            return {}

    # ------------------------------------------------------------------
    # Day Preferences
    # ------------------------------------------------------------------

    def preferences(self) -> ControllerResult[DayPreferencesView]:
        """The date's effective and inherited values of every active-engine field, and its own layer."""
        try:
            views = self._unwrap(self._planning.preference_views(self._anchor, self._anchor))
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)
        return ControllerResult.success(self._preferences_view(views))

    def _preferences_view(self, views: PreferenceViews) -> DayPreferencesView:
        view = views.days[self._anchor]
        layer = view.date_layer.overrides if view.date_layer is not None else None
        user = views.user_layer.overrides if views.user_layer is not None else None
        categories = prefs.field_categories(
            view.effective.category_multipliers, view.effective.category_preferred_windows,
            *((layer.category_multipliers, layer.category_preferred_windows) if layer else ()),
        )
        rows = prefs.preference_rows(
            prefs.field_specs(view.effective.optimizer_mode, categories), effective=view.effective,
            inherited=view.inherited, layer=layer, lower_layer=user,
        )
        return DayPreferencesView(
            day=self._anchor, timezone=views.timezone_name,
            engine=self._engine_state(view.effective, view.inherited, view.date_layer), rows=rows,
            layer_version=view.date_layer.version if view.date_layer is not None else None,
        )

    def save_preference(self, key: str, value: prefs.FieldInput, *, expected_version: int | None
                        ) -> ControllerResult[DayPreferencesView]:
        return self._change_preference(key, lambda o, spec: prefs.with_value(o, spec, value), expected_version)

    def inherit_preference(self, key: str, *, expected_version: int | None) -> ControllerResult[DayPreferencesView]:
        return self._change_preference(key, prefs.inherit, expected_version)

    def clear_preference(self, key: str, *, expected_version: int | None) -> ControllerResult[DayPreferencesView]:
        return self._change_preference(key, prefs.clear, expected_version)

    def reset_date_preferences(self, *, expected_version: int | None) -> ControllerResult[DayPreferencesView]:
        """Remove the whole date layer (engine included): the date inherits the defaults again."""
        return self._update_layer(lambda _overrides: PreferenceOverrides(), expected_version)

    def _change_preference(self, key: str, change, expected_version: int | None) -> ControllerResult[DayPreferencesView]:
        spec = next((spec for spec in prefs.field_specs(OptimizerMode.ADHD_FRIENDLY, [key.split(":", 1)[1]])
                     if spec.key == key), None) if ":" in key else next(
                         (spec for spec in prefs.BASE_FIELDS if spec.key == key), None)
        if spec is None:
            return ControllerResult.failure(f"Unknown preference {key!r}.")
        return self._update_layer(lambda overrides: change(overrides, spec), expected_version)

    def _update_layer(self, change: Callable[[PreferenceOverrides], PreferenceOverrides],
                      expected_version: int | None) -> ControllerResult[DayPreferencesView]:
        day = self._anchor
        try:
            views = self._unwrap(self._planning.preference_views(day, day))
            view = views.days[day]
            current = view.date_layer.overrides if view.date_layer is not None else PreferenceOverrides()
            updated = change(current)
            # Resolve it exactly as scheduling will, so an out-of-range value is refused before anything is saved.
            resolve_day_preferences(
                date=day, timezone=views.timezone_name, yaml_overrides=views.template,
                user_overrides=views.user_layer.overrides if views.user_layer is not None else None,
                date_overrides=updated,
            ).to_local_day_window()
        except (FieldError, ValueError) as error:
            return ControllerResult.failure(_short_error(error), error)
        except _Failure as failure:
            return ControllerResult.failure(failure.message, failure.cause)
        result = self._planning.update_date_overrides(day, change, expected_version=expected_version)
        if not result.ok:
            return ControllerResult.failure(result.error, result.cause)
        return self.preferences()

    # ------------------------------------------------------------------
    # Reset Day
    # ------------------------------------------------------------------

    def reset_plan(self) -> ControllerResult[ResetPlan]:
        """What Reset Day would delete for this date (nothing is written), in words."""
        preview = self._planning.reset_preview(self._anchor, self._anchor)
        if not preview.ok:
            return ControllerResult.failure(preview.error, preview.cause)
        return ControllerResult.success(ResetPlan(preview.value, self._reset_message(preview.value)))

    def _reset_message(self, preview: ResetPreview) -> str:
        return describe_reset(preview, day_label(self._anchor), self._names)

    def reset_day(self, plan: ResetPlan) -> ControllerResult[DaySnapshot]:
        """Apply exactly the previewed reset (refused, with nothing deleted, if anything changed since)."""
        result = self._planning.reset_range(self._anchor, self._anchor, confirmation=plan.preview.token)
        if not result.ok:
            return self._fail_with_reload(result.error)
        with self._explanations_lock:
            self._explanations = {key: value for key, value in self._explanations.items() if key[0] != self._anchor}
        return self.load()

    # ------------------------------------------------------------------
    # CSV (canonical v2; legacy only when explicitly chosen)
    # ------------------------------------------------------------------

    def csv_plan(self, path: str) -> ControllerResult[CsvPlan]:
        """Read and check a CSV without writing anything: a v2 file is previewed record by record."""
        try:
            text = read_csv_text(path)
        except (OSError, UnicodeDecodeError) as error:
            return ControllerResult.failure(f"Could not read {path}: {error}", error)
        if not is_canonical_csv(text):
            return ControllerResult.success(CsvPlan(
                path, "legacy",
                "This is a legacy schedule CSV (half-hour times, no record ids), not the canonical format "
                f"version 2. It can only be imported as new records, with day 1 = {self._anchor.isoformat()} "
                f"and times in {self.timezone}."))
        preview = self._planning.preview_csv_file(path, allow_updates=True)
        if not preview.ok:
            return ControllerResult.failure(f"The file cannot be imported; nothing was changed.\n\n{preview.error}",
                                            preview.cause)
        counts = preview.value
        plan = CsvPlan(path, "canonical", _batch_summary(counts).replace("Merged", "Importing would merge"), counts)
        note = (" Records that differ from what is saved are updated only if their version still matches."
                if plan.has_updates else "")
        return ControllerResult.success(CsvPlan(plan.path, plan.kind, plan.summary + note, counts))

    def apply_csv(self, plan: CsvPlan, *, legacy_mode: ImportMode | None = None) -> ControllerResult[ImportRun]:
        """Apply a previewed plan in one transaction (versions and ownership are checked again)."""
        if plan.kind == "legacy":
            if legacy_mode is None:
                return self._fail_with_reload("A legacy CSV is imported only when you choose Append or Replace.")
            return self.import_csv(plan.path, legacy_mode)
        result = self._planning.import_csv_file(plan.path, anchor_date=self._anchor, mode=ImportMode.APPEND,
                                                allow_updates=plan.has_updates)
        if not result.ok:
            return self._fail_with_reload(f"{result.error}\n\nNothing was imported.")
        loaded = self.load()
        if not loaded.ok:
            return ControllerResult.failure(loaded.error)
        return ControllerResult.success(ImportRun(snapshot=loaded.value, summary=_batch_summary(result.value)))


def describe_reset(preview: ResetPreview, label: str, names_of: Callable[[list[uuid.UUID]], dict[uuid.UUID, str]],
                   *, what: str = "this date") -> str:
    """A reset preview in words (Day, Week and Month use the same wording); `label` names the range."""
    if preview.blocked:
        names = names_of([*preview.blocking_dependents,
                          *(d for dependents in preview.blocking_dependents.values() for d in dependents)])
        lines = [f"{names.get(task, 'A task')} is needed by " + ", ".join(names.get(d, "a task") for d in dependents)
                 for task, dependents in preview.blocking_dependents.items()]
        return (f"{label} cannot be reset: tasks outside it depend on tasks planned in it.\n- "
                + "\n- ".join(lines) + "\nRemove those dependencies first. Nothing was deleted.")
    counts = preview.counts
    parts = [f"{counts['tasks']} task(s) planned for {what}",
             f"{counts['fixed_blocks']} fixed block(s)",
             f"{counts['placements']} scheduled entr(ies) and the saved schedule records"]
    if counts["date_preferences"]:
        parts.append(f"the date preferences and engines set for {what} ({counts['date_preferences']} date(s); "
                     "they then inherit your defaults again)")
    lines = [f"Reset {label}? This deletes " + ", ".join(parts) + "."]
    if counts["cascade_placements"]:
        lines.append(f"{counts['cascade_placements']} scheduled entr(ies) of these tasks outside {what} are "
                     "deleted with them.")
    if preview.protected_recurring_task_ids:
        lines.append(f"{len(preview.protected_recurring_task_ids)} repeating task(s) are kept; only their "
                     f"entries in {what} are removed.")
    if preview.placements_with_history_ids or preview.tasks_with_history_ids:
        lines.append("Execution history (work sessions and feedback) is kept.")
    lines.append(f"Kept: undated tasks, tasks planned outside {what}, projects, your default preferences and "
                 "all execution history.")
    return "\n\n".join(lines)


def _short_error(error: Exception) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        details = errors()
        if details:
            return str(details[0].get("msg", error))
    return str(error)
