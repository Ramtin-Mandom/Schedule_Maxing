"""The five scheduling modes on the canonical day engine (app/mode_objectives.py, docs/scheduling-modes.md):
Normal and ADHD placements are unchanged; Early Finish and Night Owl keep the same work (same tasks and
durations) and every hard constraint while finishing earlier / starting later with fewer gaps, never doing
worse than the baseline on their objective, and agreeing with an exhaustive reference on small fixtures;
results are deterministic; Catch-Up bonuses follow the smoothed formula, need five outcomes, count one
outcome per occurrence, and with no history plan exactly like Normal; the final evaluator scores actual
neighbors."""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

from app.execution.models import ExecutionStatus
from app.mode_objectives import mode_weight, schedule_metrics, time_bonus
from app.optimizer import evaluate_day_output, generate_day_schedule
from app.planning.catch_up import CatchUpEvidence, CategoryEvidence, summarize, task_bonuses

from app.planning.preferences import DayWindowSpec, OptimizerMode, PreferenceOverrides, RewardPreferencesOverride
from tests.test_day_engine import DAY, DAY_START, default_prefs, make_day_schedule, make_fixed, make_task


def flat_prefs(mode: OptimizerMode, **window):
    """No neighbor effects, so placement differences come from the mode alone."""
    return default_prefs(
        optimizer_mode=mode, day_window=DayWindowSpec(**window) if window else None,
        reward=RewardPreferencesOverride(weight_fragmentation_penalty=0.0, weight_tag_relation=0.0),
    )


def spans(output) -> dict[uuid.UUID, tuple[int, int]]:
    return {p.task_id: (int((p.planned_start - DAY_START).total_seconds() // 60),
                        int((p.planned_end - DAY_START).total_seconds() // 60)) for p in output.placements}


def assert_valid(output, fixed_spans, tasks) -> None:
    intervals = sorted(spans(output).values())
    for (a_start, a_end), (b_start, _) in zip(intervals, intervals[1:]):
        assert a_end <= b_start  # no overlap
    for start, end in intervals:
        assert all(end <= lo or start >= hi for lo, hi in fixed_spans)
    by_id = {task.id: task for task in tasks}
    for task_id, (start, end) in spans(output).items():
        assert end - start == by_id[task_id].estimated_duration_minutes
        for dependency in by_id[task_id].dependency_ids:
            assert spans(output)[dependency][1] <= start


def gappy_day():
    """Tasks whose baseline (preferred windows) leaves gaps the time modes can close."""
    from app.planning.models import LocalTimeWindow

    tasks = [
        make_task("A", duration=60, preferred_time_window=LocalTimeWindow(start_minute=540, end_minute=600)),
        make_task("B", duration=45, preferred_time_window=LocalTimeWindow(start_minute=780, end_minute=840)),
        make_task("C", duration=30, preferred_time_window=LocalTimeWindow(start_minute=1020, end_minute=1050)),
    ]
    tasks.append(make_task("D", duration=30, dependency_ids=[tasks[0].id]))
    fixed = [make_fixed("Lunch", 720, 750)]
    return tasks, fixed


def test_normal_and_adhd_are_unchanged_by_the_new_modes() -> None:
    tasks, fixed = gappy_day()
    for mode in (OptimizerMode.PRECISE_GREEDY, OptimizerMode.ADHD_FRIENDLY):
        first = generate_day_schedule(make_day_schedule(tasks, fixed), default_prefs(optimizer_mode=mode))
        again = generate_day_schedule(make_day_schedule(tasks, fixed), default_prefs(optimizer_mode=mode))
        assert spans(first) == spans(again)
        # Catch-Up without bonuses is exactly Normal.
    normal = generate_day_schedule(make_day_schedule(tasks, fixed), default_prefs(optimizer_mode=OptimizerMode.PRECISE_GREEDY))
    catch_up = generate_day_schedule(make_day_schedule(tasks, fixed), default_prefs(optimizer_mode=OptimizerMode.CATCH_UP))
    assert spans(normal) == spans(catch_up)
    assert [p.score for p in normal.placements] == [p.score for p in catch_up.placements]


def test_early_finish_and_night_owl_keep_the_work_and_every_constraint() -> None:
    tasks, fixed = gappy_day()
    fixed_spans = [(720, 750)]
    normal = generate_day_schedule(make_day_schedule(tasks, fixed), default_prefs())
    early = generate_day_schedule(make_day_schedule(tasks, fixed), default_prefs(optimizer_mode=OptimizerMode.EARLY_FINISH))
    night = generate_day_schedule(make_day_schedule(tasks, fixed), default_prefs(optimizer_mode=OptimizerMode.NIGHT_OWL))
    for output in (normal, early, night):
        assert_valid(output, fixed_spans, tasks)
        assert set(spans(output)) == set(spans(normal))  # same tasks, nothing dropped
    assert max(end for _, end in spans(early).values()) < max(end for _, end in spans(normal).values())
    assert min(start for start, _ in spans(night).values()) > min(start for start, _ in spans(normal).values())
    for mode, output in ((OptimizerMode.EARLY_FINISH, early), (OptimizerMode.NIGHT_OWL, night)):
        refined = evaluate_day_output(output, default_prefs(optimizer_mode=mode))
        baseline = evaluate_day_output(normal, default_prefs(optimizer_mode=mode))
        assert refined.objective >= baseline.objective - 1e-9  # never worse than the baseline placement
        assert refined.idle_minutes <= baseline.idle_minutes


def test_time_modes_respect_deadlines_and_fall_back_when_nothing_helps() -> None:
    deadline = DAY_START + timedelta(minutes=600)
    tasks = [make_task("Due", duration=60, deadline=deadline), make_task("Free", duration=60)]
    night = generate_day_schedule(make_day_schedule(tasks), default_prefs(optimizer_mode=OptimizerMode.NIGHT_OWL))
    assert spans(night)[tasks[0].id][1] <= 600
    single = [make_task("Only", duration=1440 - 1)]  # one placement possible in a whole day
    for mode in (OptimizerMode.EARLY_FINISH, OptimizerMode.NIGHT_OWL):
        output = generate_day_schedule(make_day_schedule(single), default_prefs(optimizer_mode=mode))
        assert len(output.placements) == 1


def test_one_minute_tasks_and_starts_stay_minute_precise() -> None:
    tasks = [make_task("Tick", duration=1), make_task("Tock", duration=7)]
    for mode in (OptimizerMode.EARLY_FINISH, OptimizerMode.NIGHT_OWL, OptimizerMode.CATCH_UP):
        output = generate_day_schedule(make_day_schedule(tasks, [make_fixed("Block", 0, 13)]),
                                       default_prefs(optimizer_mode=mode))
        assert {end - start for start, end in spans(output).values()} == {1, 7}
        if mode == OptimizerMode.EARLY_FINISH:
            assert min(start for start, _ in spans(output).values()) == 13  # not snapped to a quarter hour


def test_repacking_matches_an_exhaustive_reference_on_a_small_day() -> None:
    tasks = [make_task("A", duration=20), make_task("B", duration=30), make_task("C", duration=10)]
    fixed = [make_fixed("Busy", 40, 60)]
    total = 120
    prefs = flat_prefs(OptimizerMode.EARLY_FINISH, start_minute=0, end_minute=total)
    output = generate_day_schedule(make_day_schedule(tasks, fixed), prefs)
    got = time_bonus("early_finish", schedule_metrics(list(spans(output).values()), [(40, 60)], total),
                     mode_weight(5.0))

    best = float("-inf")
    durations = [task.estimated_duration_minutes for task in tasks]
    for starts in itertools.product(range(0, total), repeat=3):
        intervals = [(start, start + duration) for start, duration in zip(starts, durations)]
        if any(end > total for _, end in intervals):
            continue
        ordered = sorted(intervals)
        if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
            continue
        if any(not (end <= 40 or start >= 60) for start, end in intervals):
            continue
        best = max(best, time_bonus("early_finish", schedule_metrics(intervals, [(40, 60)], total), mode_weight(5.0)))
    assert abs(got - best) < 1e-9


def test_the_final_evaluator_scores_actual_neighbors() -> None:
    tasks, fixed = gappy_day()
    output = generate_day_schedule(make_day_schedule(tasks, fixed), default_prefs())
    evaluation = evaluate_day_output(output, default_prefs())
    assert evaluation.scheduled_count == len(tasks) and evaluation.fixed_minutes == 30
    assert evaluation.mode_bonus == 0 and evaluation.objective == evaluation.baseline_reward
    assert evaluation.stored_score == sum(p.score for p in output.placements)  # insertion scores, reported apart


def test_catch_up_bonus_formula_bounds_and_minimum_evidence() -> None:
    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    evidence = CatchUpEvidence(as_of=as_of, lookback_days=90, by_category={
        "sparse": CategoryEvidence(misses=4, completions=0),
        "missed": CategoryEvidence(misses=8, completions=2),
        "done": CategoryEvidence(misses=0, completions=20),
    })
    weight = mode_weight(5.0)
    assert evidence.bonus("sparse", weight) == (0.0, {"category": "sparse", "lookback_days": 90,
                                                      "as_of": "2026-03-01", "outcomes": 4,
                                                      "reason": "insufficient history"})
    bonus, explanation = evidence.bonus("missed", weight)
    assert abs(bonus - weight * (10 / 15) * (9 / 14)) < 1e-12 and explanation["misses"] == 8
    assert 0 < evidence.bonus("done", weight)[0] < bonus <= weight
    assert evidence.bonus(None, weight)[0] == 0.0  # an unknown category earns nothing


def test_catch_up_counts_one_outcome_per_occurrence_and_ignores_non_evidence() -> None:
    from app.execution.models import TaskExecution
    from app.planning.history import ExecutionHistory, ScheduleHistory
    from app.planning.models import ScheduledTask

    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    task = make_task("Gym", category="health")
    placements, executions = {}, {}

    def add(day: int, status: ExecutionStatus, *, category: str | None = "health", same_task=True):
        start = as_of - timedelta(days=day)
        placement = ScheduledTask(task_id=task.id if same_task else uuid.uuid4(), planned_date=start.date(),
                                  timezone="UTC", planned_start=start, planned_end=start + timedelta(hours=1))
        placements[placement.id] = placement
        executions[placement.id] = ExecutionHistory(TaskExecution(
            id=str(uuid.uuid4()), task_name="Gym", category=category or "", tag="", planned_duration=60, priority=5,
            status=status, created_at=start.isoformat(), updated_at=start.isoformat(), task_id=task.id,
            scheduled_task_id=placement.id), ())
        return placement

    add(1, ExecutionStatus.SKIPPED)
    add(1, ExecutionStatus.COMPLETED)  # the same occurrence (one task, no recurrence): one outcome only
    add(2, ExecutionStatus.CANCELLED, same_task=False)  # a cancelled move is not a miss
    add(3, ExecutionStatus.SCHEDULED, same_task=False)  # pending: no evidence
    add(-2, ExecutionStatus.SKIPPED, same_task=False)  # future: outside the window
    add(4, ExecutionStatus.SKIPPED, category=None, same_task=False)  # unknown snapshot
    history = ScheduleHistory(start_utc=as_of - timedelta(days=90), end_utc=as_of, placements=placements,
                              executions=executions, tasks={task.id: task})

    evidence = summarize(history, as_of)

    assert set(evidence.by_category) == {"health"}
    assert evidence.by_category["health"].outcomes == 1
    bonuses, explanations = task_bonuses(evidence, [task], mode_weight(5.0))
    assert bonuses == {} and explanations[task.id]["reason"] == "insufficient history"


def test_catch_up_puts_a_missed_category_first_when_it_counts() -> None:
    work, health = make_task("Report", category="study", priority=5), make_task("Gym", category="study", priority=5)
    tasks = [work, health]
    prefs = flat_prefs(OptimizerMode.CATCH_UP, start_minute=0, end_minute=60)  # room for one of them
    normal = generate_day_schedule(make_day_schedule(tasks), flat_prefs(OptimizerMode.PRECISE_GREEDY,
                                                                        start_minute=0, end_minute=60))
    caught_up = generate_day_schedule(make_day_schedule(tasks), prefs, task_bonuses={health.id: 5.0})
    assert [p.task_id for p in normal.placements] == [work.id]
    assert [p.task_id for p in caught_up.placements] == [health.id]


def test_the_mode_values_persist_like_the_old_ones() -> None:
    for mode in OptimizerMode:
        layer = PreferenceOverrides(optimizer_mode=mode)
        assert PreferenceOverrides.model_validate(layer.model_dump(mode="json")).optimizer_mode == mode
    assert OptimizerMode("precise_greedy") is OptimizerMode.PRECISE_GREEDY  # saved selections still read
    assert DAY == DAY_START.date()
