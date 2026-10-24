"""Differential tests: the production event search must exactly match its
independent exhaustive reference -- both per task and across a full greedy
run -- for the canonical engine.

Per-task tests use the *_exhaustive helpers directly (test/benchmark-only,
never called by production -- see the "production never calls the
exhaustive reference" test below). Full-greedy tests monkeypatch the
production call site to the exhaustive helper and compare the resulting
schedule to the normal (event-search) run.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

import app.optimizer as optimizer_module
from app.optimizer import (
    MandatoryTaskSchedulingError,
    _best_candidate_for_canonical_task,
    _best_candidate_for_canonical_task_exhaustive,
    _Placed,
    generate_day_schedule,
)
from app.planning.models import DaySchedule, FixedBlock, LocalTimeWindow, Task, TaskRegistry
from app.planning.preferences import (
    DayWindowSpec,
    OptimizerMode,
    PreferenceOverrides,
    RewardPreferencesOverride,
    resolve_day_preferences,
)

DAY = date(2024, 6, 3)
TZ = "UTC"
DAY_START = datetime(2024, 6, 3, 0, 0, tzinfo=timezone.utc)


# -----------------------------------------------------------------------------
# Shared builders (mirrors tests/test_day_engine.py's conventions)
# -----------------------------------------------------------------------------


def make_task(
    name="Task", *, duration=60, category="study", tags=None, required=False,
    deadline=None, preferred_time_window=None, required_date=None, task_id=None,
):
    kwargs = dict(
        name=name, category=category, tags=tags or [], estimated_duration_minutes=duration,
        required=required, deadline=deadline,
        preferred_time_window=preferred_time_window, required_date=required_date,
    )
    if task_id is not None:
        kwargs["id"] = task_id
    return Task(**kwargs)


def make_fixed(label, start_min, end_min):
    return FixedBlock(
        label=label, planned_date=DAY, timezone=TZ,
        planned_start=DAY_START + timedelta(minutes=start_min),
        planned_end=DAY_START + timedelta(minutes=end_min),
    )


def make_day_schedule(tasks, fixed_blocks=None):
    registry = TaskRegistry()
    for task in tasks:
        registry.add(task)
    return DaySchedule(
        date=DAY, timezone=TZ, fixed_blocks=fixed_blocks or [], task_ids=[task.id for task in tasks], tasks=registry
    )


def prefs(mode=OptimizerMode.PRECISE_GREEDY, day_window=None, **reward_kwargs):
    layer = PreferenceOverrides(
        optimizer_mode=mode,
        day_window=day_window,
        reward=RewardPreferencesOverride(**reward_kwargs) if reward_kwargs else RewardPreferencesOverride(),
    )
    return resolve_day_preferences(date=DAY, timezone=TZ, date_overrides=layer)


def placed(start, end, category="other", tag=""):
    return _Placed(start=start, end=end, category=category, tag=tag, task_id=uuid.uuid4(), score=0.0, fixed=False)


# -----------------------------------------------------------------------------
# Production never calls the exhaustive reference
# -----------------------------------------------------------------------------


def test_generate_day_schedule_never_calls_the_canonical_exhaustive_reference(monkeypatch):
    calls = []
    original = optimizer_module._best_candidate_for_canonical_task_exhaustive

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer_module, "_best_candidate_for_canonical_task_exhaustive", spy)

    tasks = [
        make_task(f"T{i}", duration=30, preferred_time_window=LocalTimeWindow(start_minute=i * 30, end_minute=i * 30 + 60))
        for i in range(5)
    ]
    schedule = make_day_schedule(tasks)
    generate_day_schedule(schedule, prefs())

    assert calls == []


# -----------------------------------------------------------------------------
# Per-task differential: canonical, seeded scenarios
# -----------------------------------------------------------------------------


CANONICAL_DURATIONS = [1, 3, 13, 29, 30, 31, 37, 90]


@pytest.mark.parametrize("duration", CANONICAL_DURATIONS)
@pytest.mark.parametrize("mode", [OptimizerMode.PRECISE_GREEDY, OptimizerMode.ADHD_FRIENDLY])
def test_canonical_per_task_parity_across_durations_and_modes(duration, mode):
    day_preferences = prefs(mode=mode, weight_fragmentation_penalty=-4.0, weight_tag_relation=2.0)
    task = make_task(
        "Study", duration=duration, category="study", tags=["math"],
        preferred_time_window=LocalTimeWindow(start_minute=100, end_minute=400),
    )
    settings = optimizer_module.day_preferences_to_reward_settings(day_preferences)
    placed_items = [placed(0, 60, category="study", tag="math"), placed(500, 560, category="work", tag="")]

    kwargs = dict(
        task=task, placed=placed_items, day_total_minutes=1440, mode=mode,
        day_window_start_minute=day_preferences.day_window.start_minute, settings=settings,
        day_preferences=day_preferences, deadline_offset=None,
    )
    event = _best_candidate_for_canonical_task(**kwargs)
    exhaustive = _best_candidate_for_canonical_task_exhaustive(**kwargs)

    assert (event is None) == (exhaustive is None)
    if event is not None:
        assert event[0] == exhaustive[0]
        assert event[1] == exhaustive[1]
        assert event[2] == pytest.approx(exhaustive[2])


def test_canonical_per_task_parity_non_midnight_day_and_short_gap_bonus():
    day_preferences = prefs(
        mode=OptimizerMode.ADHD_FRIENDLY,
        day_window=DayWindowSpec(start_minute=480, end_minute=1080),
        short_gap_bonus_weight=5.0, short_gap_bonus_max_minutes=20, short_gap_bonus_cap=8.0,
        min_gap_between_tasks_minutes=15,
    )
    task = make_task("Short", duration=10)
    settings = optimizer_module.day_preferences_to_reward_settings(day_preferences)
    placed_items = [placed(0, 100), placed(150, 600)]

    kwargs = dict(
        task=task, placed=placed_items, day_total_minutes=600, mode=OptimizerMode.ADHD_FRIENDLY,
        day_window_start_minute=480, settings=settings, day_preferences=day_preferences, deadline_offset=None,
    )
    event = _best_candidate_for_canonical_task(**kwargs)
    exhaustive = _best_candidate_for_canonical_task_exhaustive(**kwargs)

    assert event is not None and exhaustive is not None
    assert event == exhaustive


def test_canonical_per_task_parity_signed_weights_and_near_cancelling_slopes():
    """Opposing-slope regime: time-preference decay pushes one direction
    while the short-gap bonus (adhd) pushes the other -- the scenario Task 6
    explicitly calls out as a rounding hazard."""
    day_preferences = prefs(
        mode=OptimizerMode.ADHD_FRIENDLY,
        weight_time_bonus=-3.0, weight_fragmentation_penalty=2.0, weight_tag_relation=-1.0,
        short_gap_bonus_weight=3.0, short_gap_bonus_max_minutes=15, short_gap_bonus_cap=5.0,
        min_gap_between_tasks_minutes=25, max_time_distance_minutes=50,
    )
    task = make_task("T", duration=10, preferred_time_window=LocalTimeWindow(start_minute=200, end_minute=210))
    settings = optimizer_module.day_preferences_to_reward_settings(day_preferences)
    placed_items = [placed(0, 100)]

    kwargs = dict(
        task=task, placed=placed_items, day_total_minutes=1440, mode=OptimizerMode.ADHD_FRIENDLY,
        day_window_start_minute=0, settings=settings, day_preferences=day_preferences, deadline_offset=None,
    )
    event = _best_candidate_for_canonical_task(**kwargs)
    exhaustive = _best_candidate_for_canonical_task_exhaustive(**kwargs)

    assert event == exhaustive


def test_canonical_per_task_parity_no_feasible_candidates():
    day_preferences = prefs()
    task = make_task("T", duration=60)
    settings = optimizer_module.day_preferences_to_reward_settings(day_preferences)

    kwargs = dict(
        task=task, placed=[placed(0, 1440)], day_total_minutes=1440,
        mode=OptimizerMode.PRECISE_GREEDY, day_window_start_minute=0, settings=settings,
        day_preferences=day_preferences, deadline_offset=None,
    )
    assert _best_candidate_for_canonical_task(**kwargs) is None
    assert _best_candidate_for_canonical_task_exhaustive(**kwargs) is None


# -----------------------------------------------------------------------------
# Full-greedy differential: canonical
# -----------------------------------------------------------------------------


def _canonical_exhaustive_run(schedule, day_preferences, **kwargs):
    """Runs generate_day_schedule with the per-task search forced to the
    exhaustive reference (module-level monkeypatch, restored after)."""
    original = optimizer_module._best_candidate_for_canonical_task
    optimizer_module._best_candidate_for_canonical_task = optimizer_module._best_candidate_for_canonical_task_exhaustive
    try:
        return generate_day_schedule(schedule, day_preferences, **kwargs)
    finally:
        optimizer_module._best_candidate_for_canonical_task = original


def _assert_canonical_results_match(event_result, exhaustive_result):
    assert event_result.total_score == pytest.approx(exhaustive_result.total_score)
    event_placements = {p.task_id: (p.planned_start, p.planned_end, round(p.score, 2)) for p in event_result.placements}
    exhaustive_placements = {p.task_id: (p.planned_start, p.planned_end, round(p.score, 2)) for p in exhaustive_result.placements}
    assert event_placements == exhaustive_placements
    assert {e.task_id for e in event_result.unscheduled} == {e.task_id for e in exhaustive_result.unscheduled}


@pytest.mark.parametrize("mode", [OptimizerMode.PRECISE_GREEDY, OptimizerMode.ADHD_FRIENDLY])
def test_full_greedy_parity_mixed_required_optional(mode):
    day_preferences = prefs(mode=mode, weight_tag_relation=2.0, weight_fragmentation_penalty=-4.0)
    prep = make_task("Prep", duration=30, tags=["math"])
    dependent = make_task("Dependent", duration=45, required=True, tags=["math"])
    optional_a = make_task(
        "OptA", duration=60, preferred_time_window=LocalTimeWindow(start_minute=300, end_minute=400)
    )
    optional_b = make_task("OptB", duration=13, category="work")
    fixed = make_fixed("Lunch", 700, 760)

    schedule = make_day_schedule([prep, dependent, optional_a, optional_b], fixed_blocks=[fixed])

    event_result = generate_day_schedule(schedule, day_preferences)
    exhaustive_result = _canonical_exhaustive_run(schedule, day_preferences)

    _assert_canonical_results_match(event_result, exhaustive_result)


def test_full_greedy_parity_duplicate_names_distinct_ids():
    day_preferences = prefs()
    a = make_task("Same Name", duration=60, task_id=uuid.uuid4())
    b = make_task("Same Name", duration=60, task_id=uuid.uuid4())
    schedule = make_day_schedule([a, b])

    event_result = generate_day_schedule(schedule, day_preferences)
    exhaustive_result = _canonical_exhaustive_run(schedule, day_preferences)

    _assert_canonical_results_match(event_result, exhaustive_result)


def test_full_greedy_parity_near_full_day_and_fragmentation():
    day_preferences = prefs(weight_fragmentation_penalty=-6.0, min_gap_between_tasks_minutes=20)
    fixed_blocks = [make_fixed("Sleep", 0, 480), make_fixed("Lunch", 720, 760), make_fixed("Dinner", 1080, 1140)]
    tasks = [
        make_task(f"T{i}", duration=17 + i * 3, category=["study", "work", "chores"][i % 3])
        for i in range(10)
    ]
    schedule = make_day_schedule(tasks, fixed_blocks=fixed_blocks)

    event_result = generate_day_schedule(schedule, day_preferences)
    exhaustive_result = _canonical_exhaustive_run(schedule, day_preferences)

    _assert_canonical_results_match(event_result, exhaustive_result)


def test_full_greedy_parity_mandatory_failure_raises_identically():
    day_preferences = prefs()
    required_a = make_task("A", duration=800, required=True)
    required_b = make_task("B", duration=800, required=True)
    schedule = make_day_schedule([required_a, required_b])

    with pytest.raises(MandatoryTaskSchedulingError) as event_exc:
        generate_day_schedule(schedule, day_preferences)
    with pytest.raises(MandatoryTaskSchedulingError) as exhaustive_exc:
        _canonical_exhaustive_run(schedule, day_preferences)

    assert {f.task_id for f in event_exc.value.failures} == {f.task_id for f in exhaustive_exc.value.failures}


def test_full_greedy_parity_previous_result_id_reuse():
    day_preferences = prefs()
    task = make_task("Stable", duration=60)
    schedule = make_day_schedule([task])

    first = generate_day_schedule(schedule, day_preferences)
    event_second = generate_day_schedule(schedule, day_preferences, previous_result=first)
    exhaustive_second = _canonical_exhaustive_run(schedule, day_preferences, previous_result=first)

    assert event_second.placements[0].id == exhaustive_second.placements[0].id == first.placements[0].id


def test_full_greedy_parity_non_midnight_quarter_hour_alignment():
    day_preferences = prefs(
        mode=OptimizerMode.ADHD_FRIENDLY, day_window=DayWindowSpec(start_minute=463, end_minute=1200),
    )
    tasks = [make_task(f"T{i}", duration=40 + i * 5) for i in range(4)]
    schedule = make_day_schedule(tasks)

    event_result = generate_day_schedule(schedule, day_preferences)
    exhaustive_result = _canonical_exhaustive_run(schedule, day_preferences)

    _assert_canonical_results_match(event_result, exhaustive_result)


# -----------------------------------------------------------------------------
# Independent validity checks (not calling the same helper being tested)
# -----------------------------------------------------------------------------


def test_canonical_result_independent_validity_checks():
    day_preferences = prefs(weight_tag_relation=2.0)
    prep = make_task("Prep", duration=30)
    dependent = make_task("Dependent", duration=30, required=True)
    fixed = make_fixed("Busy", 200, 260)
    schedule = make_day_schedule([prep, dependent], fixed_blocks=[fixed])

    result = generate_day_schedule(schedule, day_preferences)

    intervals = [(b.planned_start, b.planned_end) for b in result.fixed_blocks]
    intervals += [(p.planned_start, p.planned_end) for p in result.placements]
    intervals.sort()
    for (_, end), (next_start, _) in zip(intervals, intervals[1:]):
        assert end <= next_start  # no overlap

    day_start_utc = DAY_START
    day_end_utc = DAY_START + timedelta(minutes=1440)
    for start, end in intervals:
        assert day_start_utc <= start < end <= day_end_utc  # day bounds

    by_task = {p.task_id: p for p in result.placements}
    assert dependent.id in by_task and prep.id in by_task

    assert result.total_score == pytest.approx(round(sum(p.score for p in result.placements), 2))


# -----------------------------------------------------------------------------
# Scorer-evaluation reduction
# -----------------------------------------------------------------------------


def test_event_search_substantially_reduces_scorer_calls_on_a_large_sparse_interval(monkeypatch):
    """A large, mostly-empty day with a single unconstrained task: the
    exhaustive reference scores every one of ~1000 feasible minutes, while
    the event search should need only a handful of real scorer calls."""
    day_preferences = prefs()
    task = make_task("Solo", duration=60)
    settings = optimizer_module.day_preferences_to_reward_settings(day_preferences)

    counts = {"n": 0}
    real_score = optimizer_module.calculate_task_score

    def counting_score(*args, **kwargs):
        counts["n"] += 1
        return real_score(*args, **kwargs)

    kwargs = dict(
        task=task, placed=[], day_total_minutes=1440, mode=OptimizerMode.PRECISE_GREEDY,
        day_window_start_minute=0, settings=settings, day_preferences=day_preferences, deadline_offset=None,
    )

    monkeypatch.setattr(optimizer_module, "calculate_task_score", counting_score)
    _best_candidate_for_canonical_task(**kwargs)
    event_calls = counts["n"]

    counts["n"] = 0
    _best_candidate_for_canonical_task_exhaustive(**kwargs)
    exhaustive_calls = counts["n"]

    assert exhaustive_calls >= 1000  # 1440-60+1 feasible minutes
    assert event_calls < exhaustive_calls // 10  # order-of-magnitude reduction
