"""Fixed-block write invariants (app/planning/fixed_block_rules.py) at the local
write boundary: PlanningService creates, edits, per-date replacement, both
importers, concurrent writers on one database file, and the historical
records that are deliberately not re-judged."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.execution.db import get_connection
from app.planning.application import PlanningService, RecordBatch
from app.planning.fixed_block_rules import FixedBlockRuleViolation
from app.planning.models import FixedBlock
from app.planning.preferences import DayWindowSpec, PreferenceOverrides
from app.planning.repository import PlanningRepository

DAY = date(2026, 9, 23)
UTC = timezone.utc


def at(hour: int, minute: int = 0, *, day: date = DAY, tz=UTC, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=tz)


def block(start: datetime, end: datetime, *, label: str = "Class", day: date = DAY, tz: str = "UTC", **extra) -> FixedBlock:
    return FixedBlock(label=label, planned_date=day, timezone=tz, planned_start=start, planned_end=end, **extra)


def stored_state(connection) -> tuple:
    """Every fixed-block row and every change-capture mark: what a refused write must leave untouched."""
    blocks = [tuple(row) for row in connection.execute("SELECT * FROM fixed_blocks ORDER BY id")]
    dirty = [tuple(row) for row in connection.execute("SELECT entity_type, entity_id, local_rev FROM sync_dirty ORDER BY 1, 2")]
    return blocks, dirty


# -----------------------------------------------------------------------------
# Create
# -----------------------------------------------------------------------------


def test_an_overlapping_create_is_refused_and_changes_nothing(planning_service: PlanningService, connection) -> None:
    planning_service.create_fixed_block(block(at(9), at(11), label="Lecture"))
    before = stored_state(connection)

    with pytest.raises(FixedBlockRuleViolation) as raised:
        planning_service.create_fixed_block(block(at(10), at(12), label="Lab"))

    assert raised.value.code == "overlap" and raised.value.conflicting.label == "Lecture"
    assert stored_state(connection) == before


def test_adjacent_blocks_and_off_grid_minutes_are_valid(planning_service: PlanningService) -> None:
    planning_service.create_fixed_block(block(at(9), at(10, 13), label="A"))
    planning_service.create_fixed_block(block(at(10, 13), at(11, 47), label="B"))  # starts exactly where A ends
    planning_service.create_fixed_block(block(at(11, 47), at(12), label="C"))

    assert [b.label for b in planning_service.fixed_blocks_for_date(DAY)] == ["A", "B", "C"]


def test_whole_day_block_and_following_midnight_end_are_valid(planning_service: PlanningService) -> None:
    whole = planning_service.create_fixed_block(block(at(0), at(0, day=DAY + timedelta(days=1)), label="Away"))
    assert whole.planned_end == at(0, day=DAY + timedelta(days=1))


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        (at(9, second=30), at(10), "sub_minute_precision"),
        (at(9), at(10) + timedelta(microseconds=1), "sub_minute_precision"),
        (at(23, day=DAY - timedelta(days=1)), at(23, 30, day=DAY - timedelta(days=1)), "date_mismatch"),
        (at(23), at(1, day=DAY + timedelta(days=1)), "outside_day_window"),
    ],
)
def test_invalid_intervals_are_refused(planning_service: PlanningService, connection, start, end, code) -> None:
    before = stored_state(connection)
    with pytest.raises(FixedBlockRuleViolation) as raised:
        planning_service.create_fixed_block(block(start, end))
    assert raised.value.code == code
    assert stored_state(connection) == before


def test_the_date_is_judged_in_the_blocks_own_timezone(planning_service: PlanningService) -> None:
    tokyo = ZoneInfo("Asia/Tokyo")
    # 2026-09-23 06:00 in Tokyo is 2026-09-22 21:00 UTC: dated the 23rd, it is valid.
    saved = planning_service.create_fixed_block(block(at(6, tz=tokyo), at(7, tz=tokyo), tz="Asia/Tokyo"))
    assert saved.planned_date == DAY
    with pytest.raises(FixedBlockRuleViolation, match="starts on 2026-09-23"):
        planning_service.create_fixed_block(
            block(at(6, tz=tokyo), at(7, tz=tokyo), tz="Asia/Tokyo", day=DAY - timedelta(days=1), label="Wrong date")
        )


def test_the_effective_day_window_uses_the_date_and_user_layers(planning_service: PlanningService) -> None:
    layer = planning_service.save_date_preferences(
        DAY, PreferenceOverrides(day_window=DayWindowSpec(start_minute=8 * 60, end_minute=22 * 60))
    )
    with pytest.raises(FixedBlockRuleViolation) as raised:
        planning_service.create_fixed_block(block(at(7), at(9)))
    assert raised.value.code == "outside_day_window" and "08:00-22:00" in str(raised.value)
    planning_service.create_fixed_block(block(at(8), at(9), label="Edge"))  # touching the window's start is inside

    # Without the date layer the user layer applies; the other dates inherit it too.
    planning_service.delete_date_preferences(DAY, expected_version=layer.version)
    planning_service.save_user_preferences(PreferenceOverrides(day_window=DayWindowSpec(start_minute=6 * 60, end_minute=1440)))
    planning_service.create_fixed_block(block(at(6), at(7), label="Early"))
    with pytest.raises(FixedBlockRuleViolation):
        planning_service.create_fixed_block(block(at(5), at(6), label="Too early"))


def test_a_window_spanning_a_dst_change_is_checked_by_its_endpoints(planning_service: PlanningService) -> None:
    new_york, spring = ZoneInfo("America/New_York"), date(2026, 3, 8)  # 02:00 -> 03:00
    saved = planning_service.create_fixed_block(
        block(at(0, day=spring, tz=new_york), at(1, day=spring, tz=new_york), day=spring, tz="America/New_York")
    )
    assert saved.planned_date == spring


def test_an_ambiguous_window_endpoint_is_refused_not_guessed(planning_service: PlanningService) -> None:
    new_york, fall = ZoneInfo("America/New_York"), date(2026, 11, 1)  # 01:00-02:00 happens twice
    planning_service.save_date_preferences(fall, PreferenceOverrides(day_window=DayWindowSpec(start_minute=90, end_minute=1440)))
    with pytest.raises(FixedBlockRuleViolation) as raised:
        planning_service.create_fixed_block(
            block(at(9, day=fall, tz=new_york), at(10, day=fall, tz=new_york), day=fall, tz="America/New_York")
        )
    assert raised.value.code == "unsupported_day_window"


def test_blocks_on_neighbouring_dates_in_other_timezones_cannot_overlap(planning_service: PlanningService) -> None:
    tokyo = ZoneInfo("Asia/Tokyo")
    # 2026-09-24 00:00-02:00 Tokyo is 2026-09-23 15:00-17:00 UTC.
    planning_service.create_fixed_block(
        block(at(0, day=DAY + timedelta(days=1), tz=tokyo), at(2, day=DAY + timedelta(days=1), tz=tokyo),
              day=DAY + timedelta(days=1), tz="Asia/Tokyo", label="Tokyo call")
    )
    with pytest.raises(FixedBlockRuleViolation, match="Tokyo call"):
        planning_service.create_fixed_block(block(at(16), at(18), label="UTC gym"))


# -----------------------------------------------------------------------------
# Edit
# -----------------------------------------------------------------------------


def test_an_edit_is_judged_against_the_others_but_not_itself(planning_service: PlanningService, connection) -> None:
    first = planning_service.create_fixed_block(block(at(9), at(10), label="First"))
    second = planning_service.create_fixed_block(block(at(12), at(13), label="Second"))

    moved = planning_service.update_fixed_block(
        first.model_copy(update={"planned_start": at(9, 30), "planned_end": at(10, 30)}), expected_version=first.version
    )
    assert moved.version == first.version + 1

    before = stored_state(connection)
    with pytest.raises(FixedBlockRuleViolation):
        planning_service.update_fixed_block(
            moved.model_copy(update={"planned_end": at(12, 30)}), expected_version=moved.version
        )
    assert stored_state(connection) == before
    assert planning_service.fixed_blocks_for_date(DAY)[1] == second


def test_a_historical_block_can_be_relabelled_without_being_rewritten(
    planning_service: PlanningService, planning_repository: PlanningRepository
) -> None:
    legacy = block(at(9, second=15), at(10), label="Imported long ago")
    planning_repository.insert_fixed_block(legacy)  # stored before the rules existed

    relabelled = planning_service.update_fixed_block(legacy.model_copy(update={"label": "Seminar"}), expected_version=1)
    assert relabelled.label == "Seminar" and relabelled.planned_start == legacy.planned_start  # not rounded

    with pytest.raises(FixedBlockRuleViolation, match="whole minutes"):
        planning_service.update_fixed_block(
            relabelled.model_copy(update={"planned_end": at(10, 30)}), expected_version=relabelled.version
        )


def test_set_fixed_blocks_for_date_judges_the_final_day(planning_service: PlanningService, connection) -> None:
    a = planning_service.create_fixed_block(block(at(9), at(10), label="A"))
    b = planning_service.create_fixed_block(block(at(10), at(11), label="B"))

    swapped = planning_service.set_fixed_blocks_for_date(
        DAY,
        [a.model_copy(update={"planned_start": at(10), "planned_end": at(11)}),
         b.model_copy(update={"planned_start": at(9), "planned_end": at(10)})],
        expected_versions={a.id: a.version, b.id: b.version},
    )
    assert [x.label for x in swapped] == ["B", "A"]

    before = stored_state(connection)
    current = {x.id: x.version for x in swapped}
    with pytest.raises(FixedBlockRuleViolation):
        planning_service.set_fixed_blocks_for_date(
            DAY, [*swapped, block(at(10, 30), at(11, 30), label="C")], expected_versions=current
        )
    assert stored_state(connection) == before


# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------


def test_the_legacy_import_checks_every_block(planning_service: PlanningService, connection) -> None:
    before = stored_state(connection)
    with pytest.raises(FixedBlockRuleViolation, match="overlaps the saved fixed block"):
        planning_service.apply_import([], [block(at(9), at(10), label="One"), block(at(9, 30), at(10, 30), label="Two")])
    assert stored_state(connection) == before


def test_the_canonical_import_checks_live_blocks_but_keeps_tombstones_as_history(
    planning_service: PlanningService, connection
) -> None:
    with pytest.raises(FixedBlockRuleViolation):
        planning_service.apply_record_batch(RecordBatch(fixed_blocks=[block(at(9, second=1), at(10))]))
    assert stored_state(connection)[0] == []

    tombstone = block(at(9, second=1), at(10), deleted_at=at(12))
    result = planning_service.apply_record_batch(RecordBatch(fixed_blocks=[tombstone]))
    assert result.created["fixed_block"] == 1
    # Re-importing the identical record is a no-op, not a re-judgement.
    assert planning_service.apply_record_batch(RecordBatch(fixed_blocks=[tombstone])).unchanged["fixed_block"] == 1


# -----------------------------------------------------------------------------
# Concurrency: two writers on the same database file
# -----------------------------------------------------------------------------


def test_concurrent_overlapping_writers_cannot_both_succeed(db_path) -> None:
    get_connection(db_path).close()  # migrate once
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def writer(label: str, hour: int) -> None:
        connection = get_connection(db_path)
        try:
            service = PlanningService(PlanningRepository(connection))
            barrier.wait()
            service.create_fixed_block(block(at(hour), at(hour + 2), label=label))
            outcomes.append("saved")
        except FixedBlockRuleViolation:
            outcomes.append("refused")
        finally:
            connection.close()

    threads = [threading.Thread(target=writer, args=("Left", 9)), threading.Thread(target=writer, args=("Right", 10))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["refused", "saved"]
    connection = get_connection(db_path)
    try:
        assert len(PlanningService(PlanningRepository(connection)).fixed_blocks_for_date(DAY)) == 1
    finally:
        connection.close()
