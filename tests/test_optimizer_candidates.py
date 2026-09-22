"""Unit tests for the shared event-search machinery in app/optimizer.py:
_free_intervals, the lattice helpers, _event_breakpoints,
_earliest_best_in_continuous_range, and _best_start_in_free_interval.

These test the candidate-generation building blocks in isolation (with
stub/hand-built scorers where useful) -- end-to-end parity against the
independent exhaustive reference lives in test_optimizer_differential.py.
"""

from __future__ import annotations

import uuid

from app.optimizer import (
    _Placed,
    _best_start_in_free_interval,
    _earliest_best_among,
    _earliest_best_in_continuous_range,
    _event_breakpoints,
    _free_intervals,
    _lattice_points,
    _next_lattice_offset,
    _short_gap_active_zone,
)
from app.reward import RewardSettings


def placed(start, end):
    return _Placed(start=start, end=end, name="X", category="other", tag="", task_id=uuid.uuid4(), score=0.0, fixed=False)


def scoring_task(*, duration=60, name="T", category="other", tag="", priority=5, preference_time=None):
    return {
        "name": name, "category": category, "tag": tag, "duration": duration,
        "priority": priority, "fixed": False, "preference_time": preference_time,
    }


# -----------------------------------------------------------------------------
# _free_intervals
# -----------------------------------------------------------------------------


def test_free_intervals_empty_day_is_one_full_interval():
    assert _free_intervals([], 1440) == [(0, 1440)]


def test_free_intervals_fully_occupied_day_is_empty():
    assert _free_intervals([placed(0, 1440)], 1440) == []


def test_free_intervals_unsorted_input_is_sorted_first():
    items = [placed(600, 700), placed(0, 100)]
    assert _free_intervals(items, 1440) == [(100, 600), (700, 1440)]


def test_free_intervals_touching_intervals_produce_no_gap():
    items = [placed(0, 100), placed(100, 200)]
    assert _free_intervals(items, 1440) == [(200, 1440)]


def test_free_intervals_overlapping_placed_items_do_not_double_subtract():
    items = [placed(0, 200), placed(100, 300)]
    assert _free_intervals(items, 1440) == [(300, 1440)]


def test_free_intervals_exact_fit_gap_is_preserved():
    items = [placed(0, 100), placed(160, 1440)]
    assert _free_intervals(items, 1440) == [(100, 160)]


# -----------------------------------------------------------------------------
# Lattice helpers
# -----------------------------------------------------------------------------


def test_next_lattice_offset_already_aligned_returns_same_value():
    assert _next_lattice_offset(60, 30, 0) == 60


def test_next_lattice_offset_snaps_up():
    assert _next_lattice_offset(17, 30, 0) == 30


def test_next_lattice_offset_anchored_to_nonzero_local_minute():
    # anchor=7 (offset 0 is local wall-clock minute 7): offset 1 is local
    # minute 8; the next multiple of 15 at or after local minute 8 is 15,
    # which is offset 8 (15 - 7).
    assert _next_lattice_offset(1, 15, 7) == 8


def test_lattice_points_empty_when_first_eligible_exceeds_hi():
    assert _lattice_points(10, 25, 30, 0) == []


def test_lattice_points_bounded_and_inclusive():
    assert _lattice_points(0, 90, 30, 0) == [0, 30, 60, 90]


def test_lattice_points_single_point_interval():
    assert _lattice_points(30, 30, 30, 0) == [30]


# -----------------------------------------------------------------------------
# _short_gap_active_zone
# -----------------------------------------------------------------------------


def test_short_gap_active_zone_before_side():
    assert _short_gap_active_zone(100, min_gap=20, duration=10, before=True) == (100, 119)


def test_short_gap_active_zone_after_side():
    # neighbor starts at 200, duration=10, min_gap=20: pre_gap = 200-10-start
    # in [0,20) -> 170 < start <= 190 -> integers [171, 190]
    assert _short_gap_active_zone(200, min_gap=20, duration=10, before=False) == (171, 190)


def test_short_gap_active_zone_none_when_no_edge():
    assert _short_gap_active_zone(None, min_gap=20, duration=10, before=True) is None


def test_short_gap_active_zone_none_when_min_gap_nonpositive():
    assert _short_gap_active_zone(100, min_gap=0, duration=10, before=True) is None


# -----------------------------------------------------------------------------
# _event_breakpoints
# -----------------------------------------------------------------------------


def _settings(**overrides):
    return RewardSettings(**overrides)


def test_event_breakpoints_includes_interval_endpoints():
    cuts, zones = _event_breakpoints(
        duration=30, clipped_start=0, clipped_latest=1000, previous_item=None, next_item=None,
        preferred_window=None, settings=_settings(), adhd_mode=False, day_start=None, day_end=None,
    )
    assert cuts[0] == 0
    assert cuts[-1] == 1000
    assert zones == []


def test_event_breakpoints_preferred_window_boundaries_present():
    cuts, _ = _event_breakpoints(
        duration=60, clipped_start=0, clipped_latest=1000,
        previous_item=None, next_item=None,
        preferred_window={"start_time": 100, "end_time": 200},
        settings=_settings(max_time_distance_minutes=240), adhd_mode=False, day_start=None, day_end=None,
    )
    # preferred_start=100, preferred_end-duration=140, center=(100+200-60)/2=120
    assert 100 in cuts
    assert 140 in cuts
    assert 120 in cuts


def test_event_breakpoints_fragmentation_step_isolated_on_both_sides():
    prev = placed(0, 100)
    settings = _settings(min_gap_between_tasks_minutes=30)
    cuts, _ = _event_breakpoints(
        duration=10, clipped_start=0, clipped_latest=500, previous_item=prev, next_item=None,
        preferred_window=None, settings=settings, adhd_mode=False, day_start=None, day_end=None,
    )
    # gap==0 boundary is start_time==100 (flush); gap==min_gap boundary is 130.
    # Both the exact points and their immediate integer neighbors must be
    # present so the step is isolated into its own tiny region.
    for point in (99, 100, 101, 129, 130, 131):
        assert point in cuts


def test_event_breakpoints_no_active_zone_when_adhd_mode_off():
    prev = placed(0, 100)
    settings = _settings(short_gap_bonus_weight=5.0, min_gap_between_tasks_minutes=30)
    _, zones = _event_breakpoints(
        duration=10, clipped_start=100, clipped_latest=500, previous_item=prev, next_item=None,
        preferred_window=None, settings=settings, adhd_mode=False, day_start=None, day_end=None,
    )
    assert zones == []


def test_event_breakpoints_active_zone_bounded_by_min_gap():
    prev = placed(0, 100)
    settings = _settings(short_gap_bonus_weight=5.0, min_gap_between_tasks_minutes=20, short_gap_bonus_max_minutes=10)
    _, zones = _event_breakpoints(
        duration=10, clipped_start=100, clipped_latest=500, previous_item=prev, next_item=None,
        preferred_window=None, settings=settings, adhd_mode=True, day_start=None, day_end=None,
    )
    assert zones == [(100, 119)]


def test_event_breakpoints_active_zone_uses_day_boundary_when_no_neighbor():
    settings = _settings(short_gap_bonus_weight=5.0, min_gap_between_tasks_minutes=20, short_gap_bonus_max_minutes=10)
    _, zones = _event_breakpoints(
        duration=10, clipped_start=0, clipped_latest=500, previous_item=None, next_item=None,
        preferred_window=None, settings=settings, adhd_mode=True, day_start=0, day_end=1440,
    )
    # before-side zone anchored at day_start=0; the after-side zone (derived
    # from day_end=1440) falls entirely outside [clipped_start, clipped_latest]
    # = [0, 500] and is dropped rather than clipped into a false-positive zone.
    assert zones == [(0, 19)]


def test_event_breakpoints_inactive_zone_when_duration_exceeds_threshold():
    prev = placed(0, 100)
    settings = _settings(short_gap_bonus_weight=5.0, min_gap_between_tasks_minutes=20, short_gap_bonus_max_minutes=5)
    _, zones = _event_breakpoints(
        duration=10, clipped_start=100, clipped_latest=500, previous_item=prev, next_item=None,
        preferred_window=None, settings=settings, adhd_mode=True, day_start=None, day_end=None,
    )
    assert zones == []


def test_event_breakpoints_no_preferred_window_no_time_breakpoints_beyond_endpoints():
    cuts, _ = _event_breakpoints(
        duration=30, clipped_start=50, clipped_latest=90, previous_item=None, next_item=None,
        preferred_window=None, settings=_settings(), adhd_mode=False, day_start=None, day_end=None,
    )
    assert set(cuts) == {50, 90}


# -----------------------------------------------------------------------------
# _earliest_best_in_continuous_range / _earliest_best_among
# -----------------------------------------------------------------------------


def test_earliest_best_in_continuous_range_ascending_finds_plateau_start():
    # score(x) = round(x * 0.001, 2): a very small slope over a long range,
    # so many consecutive integers round to the same value -- the earliest
    # minute achieving the range's max (at hi) must be found exactly.
    def scorer(x):
        return round(x * 0.001, 2)

    start, score = _earliest_best_in_continuous_range(scorer, 0, 1000)
    assert score == scorer(1000)
    assert scorer(start) == score
    assert scorer(start - 1) < score if start > 0 else True


def test_earliest_best_in_continuous_range_descending_returns_lo_immediately():
    calls = []

    def scorer(x):
        calls.append(x)
        return -x

    start, score = _earliest_best_in_continuous_range(scorer, 10, 1000)
    assert start == 10
    assert score == -10
    assert calls == [10, 1000]  # only the two endpoints -- no search needed


def test_earliest_best_in_continuous_range_constant_returns_lo():
    start, score = _earliest_best_in_continuous_range(lambda x: 5.0, 0, 500)
    assert start == 0
    assert score == 5.0


def test_earliest_best_in_continuous_range_single_point():
    start, score = _earliest_best_in_continuous_range(lambda x: 3.0, 7, 7)
    assert (start, score) == (7, 3.0)


def test_earliest_best_among_picks_earliest_tied_max():
    scores = {10: 1.0, 20: 2.0, 30: 2.0, 40: 1.5}
    start, score = _earliest_best_among(lambda x: scores[x], [10, 20, 30, 40])
    assert (start, score) == (20, 2.0)


# -----------------------------------------------------------------------------
# _best_start_in_free_interval
# -----------------------------------------------------------------------------


def test_best_start_in_free_interval_no_feasible_candidates_returns_none():
    result = _best_start_in_free_interval(
        interval_start=0, interval_end=10, duration=60, first_start=0, latest_start=1000,
        previous_item=None, next_item=None, scoring_task=scoring_task(duration=60),
        settings=_settings(), adhd_mode=False, day_start=0, day_end=1000, lattice_step=None, lattice_anchor=0,
    )
    assert result is None


def test_best_start_in_free_interval_lattice_mode_grid_projected():
    result = _best_start_in_free_interval(
        interval_start=0, interval_end=200, duration=60, first_start=17, latest_start=140,
        previous_item=None, next_item=None, scoring_task=scoring_task(duration=60),
        settings=_settings(), adhd_mode=False, day_start=None, day_end=None,
        lattice_step=30, lattice_anchor=0,
    )
    assert result is not None
    start, end, score = result
    assert start % 30 == 0
    assert start >= 17


def test_best_start_in_free_interval_lattice_anchored_to_nonzero_local_minute():
    # anchor=7 (local minute 7 at offset 0), step=15: the grid in offset
    # space is 8, 23, 38, ... (local minutes 15, 30, 45, ...).
    result = _best_start_in_free_interval(
        interval_start=0, interval_end=100, duration=5, first_start=0, latest_start=95,
        previous_item=None, next_item=None, scoring_task=scoring_task(duration=5),
        settings=_settings(), adhd_mode=False, day_start=None, day_end=None,
        lattice_step=15, lattice_anchor=7,
    )
    assert result is not None
    start, _, _ = result
    assert (7 + start) % 15 == 0
