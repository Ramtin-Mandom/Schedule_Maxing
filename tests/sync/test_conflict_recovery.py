"""Synchronization conflicts and interruption recovery for recurrence, execution and manual intent
(docs/sync-protocol.md, "Conflicts", "Retention and cursors", "Failures"): an occurrence expanded offline
follows a concurrent series edit instead of getting around it; a local exception survives a series edit;
an exception under a series deleted elsewhere is a durable conflict whose only safe choice retires it, never
resurrecting anything; accepting the server's execution keeps the work only this device recorded; a request
the server refuses is narrowed to its unit and becomes actionable; replays add no versions or change-log
entries; a crash before acknowledgement or before the cursor commit replays safely; a release and a remote
constraint edit combine; an expired session keeps the queue and the conflicts; and the conflict view
explains it all in words."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.execution.models import ExecutionStatus
from app.planning import series as series_ops
from app.planning import workflow
from app.planning.application import RangeScope
from app.planning.models import FixedBlock, OccurrenceState, RecurrenceSpec
from app.planning.series import EditScope
from app.sync.engine import ConflictResolutionError, SyncEngine
from app.sync.transport import AuthenticationError, ProtocolError
from app.ui.account_controller import conflict_view
from backend import models
from backend.database import session_factory
from tests.sync.conftest import MON, FlakyTransport, InProcessTransport

DAY = timedelta(days=1)


@pytest.fixture
def pair(alice_server, make_device):
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    return alice_server, a, b


def walk_series(device):
    return device.add_task("Walk", recurrence=RecurrenceSpec(frequency="daily", start_date=MON, timezone="UTC"))


def server_occurrences(server, series_id) -> dict:
    tasks = server.get("alice@example.com", "/tasks", include_deleted=True, limit=500)["items"]
    return {task["occurrence_slot"]: task for task in tasks if task["series_id"] == str(series_id)}


def change_log_size(server) -> int:
    with session_factory(server.engine)() as session:
        return session.scalar(select(func.count()).select_from(models.ChangeLogEntry))


def synced_series(server, a, b):
    walk = walk_series(a)
    series_ops.expand_occurrences(a.planning, MON, MON + DAY)
    assert a.sync_now().status == "ok"
    assert b.sync_now().status == "ok"
    return walk


# -----------------------------------------------------------------------------
# Series edits, exceptions and offline expansion
# -----------------------------------------------------------------------------


def test_an_offline_expansion_follows_a_concurrent_series_edit(pair) -> None:
    server, a, b = pair
    walk = synced_series(server, a, b)
    series_ops.expand_occurrences(b.planning, MON + 2 * DAY, MON + 3 * DAY)  # offline, from the old series
    stored = a.planning.get_task(walk.id)
    series_ops.edit_series(a.planning, stored.model_copy(update={"name": "Stroll"}), expected_version=stored.version,
                           scope=EditScope.SERIES)
    assert a.sync_now().status == "ok"

    report = b.sync_now()

    assert report.status == "ok" and report.conflicts == 0 and b.sync.list_conflicts() == []
    on_server = server_occurrences(server, walk.id)
    assert {slot: task["name"] for slot, task in on_server.items()} == {
        (MON + offset * DAY).isoformat(): "Stroll" for offset in range(4)}  # never a stale "Walk" copy
    assert {t.name for t in b.planning.occurrences_of_series([walk.id])[walk.id]} == {"Stroll"}
    assert b.sync_now().pushed == 0 and not b.dirty()


def test_a_local_exception_survives_a_series_edit_as_its_own_content(pair) -> None:
    server, a, b = pair
    walk = synced_series(server, a, b)
    [created] = series_ops.expand_occurrences(b.planning, MON + 2 * DAY, MON + 2 * DAY).created
    series_ops.edit_occurrence(b.planning, created.model_copy(update={"priority": 9}), expected_version=created.version)
    stored = a.planning.get_task(walk.id)
    series_ops.edit_series(a.planning, stored.model_copy(update={"name": "Stroll"}), expected_version=stored.version,
                           scope=EditScope.SERIES)
    a.sync_now()

    assert b.sync_now().status == "ok" and b.sync.list_conflicts() == []

    exception = server_occurrences(server, walk.id)[(MON + 2 * DAY).isoformat()]
    assert (exception["priority"], exception["occurrence_state"]) == (9, "modified")  # the user's own edit


def test_a_late_device_never_resurrects_a_deleted_series(pair) -> None:
    server, a, b = pair
    walk = synced_series(server, a, b)
    created = series_ops.expand_occurrences(b.planning, MON + 2 * DAY, MON + 3 * DAY).created  # offline
    exception = created[0]
    series_ops.edit_occurrence(b.planning, exception.model_copy(update={"priority": 9}),
                               expected_version=exception.version)
    stored = a.planning.get_task(walk.id)
    series_ops.delete_series(a.planning, walk.id, expected_version=stored.version, scope=EditScope.SERIES)
    assert a.sync_now().status == "ok"

    assert b.sync_now().status == "ok"

    live = {slot for slot, task in server_occurrences(server, walk.id).items() if task["deleted_at"] is None}
    assert live == set()  # nothing B expanded or edited brought the series back
    [conflict] = b.sync.list_conflicts()  # only the user's own exception asks for a decision
    assert conflict.entity_id == str(exception.id) and conflict.error["code"] == "series_changed"
    choices = b.sync.conflict_actions(conflict)
    assert choices["accept_remote"] is None and "series" in choices["keep_local"]
    with pytest.raises(ConflictResolutionError):
        b.sync.resolve_conflict(conflict.id, "keep_local")
    view = conflict_view(conflict, choices)
    assert any("occurrence of a recurring series" in line for line in view.context)
    assert any("series changed on the server" in line for line in view.context)

    resolved = b.sync.resolve_conflict(conflict.id, "accept_remote")

    assert resolved.resolution["occurrence"] == "retired"
    retired = b.planning.get_tasks_including_deleted([exception.id])[exception.id]
    assert retired.deleted_at is not None and retired.occurrence_state == OccurrenceState.SUPERSEDED
    assert b.sync_now().status == "ok" and b.sync.list_conflicts() == []
    assert series_ops.expand_occurrences(b.planning, MON, MON + 3 * DAY).created == []  # stays suppressed
    assert {slot for slot, task in server_occurrences(server, walk.id).items() if task["deleted_at"] is None} == set()


# -----------------------------------------------------------------------------
# Execution history
# -----------------------------------------------------------------------------


def test_accepting_the_servers_execution_keeps_the_work_only_this_device_recorded(pair) -> None:
    server, a, b = pair
    essay = a.add_task("Essay", required_date=MON)
    workflow.generate(a.planning, range_start=MON, range_end=MON, timezone_name="UTC", scope=RangeScope.ELIGIBLE)
    [placement] = a.planning.placements_for_date(MON)
    execution = a.executions.get_or_create_canonical_execution(essay, placement)
    a.sync_now()
    b.sync_now()
    a.executions.start(execution.id)
    a.executions.complete(execution.id)
    a.sync_now()
    b.executions.start(execution.id)  # offline, at another time: a session the server never had
    b.executions.pause(execution.id)
    mine = b.executions.list_sessions(execution.id)

    b.sync_now()
    [conflict] = b.sync.list_conflicts()
    choices = b.sync.conflict_actions(conflict)
    assert "sessions" in choices["keep_local"]  # B's sessions cannot be expressed against A's
    assert any("only on this device" in line for line in conflict_view(conflict, choices).context)

    resolved = b.sync.resolve_conflict(conflict.id, "accept_remote")

    kept_id = resolved.resolution["kept_history_execution_id"]
    assert b.executions.get_execution(execution.id).status == ExecutionStatus.COMPLETED  # the server's version
    kept = b.executions.get_execution(kept_id)
    assert (kept.scheduled_task_id, kept.status) == (None, ExecutionStatus.PAUSED)
    assert [(s.started_at, s.ended_at) for s in b.executions.list_sessions(kept_id)] == [
        (s.started_at, s.ended_at) for s in mine]  # nothing recorded here was deleted
    assert b.sync_now().status == "ok" and b.sync.list_conflicts() == []
    uploaded = server.get("alice@example.com", f"/executions/{b.executions.wire_id(kept_id)}")
    assert uploaded["historical_reference"] is False or uploaded["scheduled_task_id"] is None
    assert len(uploaded["sessions"]) == len(mine)
    assert b.sync_now().pushed == 0  # no duplicate on a second round


# -----------------------------------------------------------------------------
# Manual intent
# -----------------------------------------------------------------------------


def test_a_release_and_a_remote_constraint_edit_combine(pair) -> None:
    server, a, b = pair
    essay = a.add_task("Essay", required_date=MON)
    workflow.generate(a.planning, range_start=MON, range_end=MON, timezone_name="UTC", scope=RangeScope.ELIGIBLE)
    [placed] = a.planning.placements_for_date(MON)
    start = datetime(MON.year, MON.month, MON.day, 15, tzinfo=timezone.utc)
    moved = workflow.reschedule_placement(a.planning, placed.id, expected_version=placed.version, planned_date=MON,
                                          timezone_name="UTC", planned_start=start,
                                          planned_end=start + timedelta(hours=1)).replacement
    a.sync_now()
    b.sync_now()
    b.planning.release_manual_placement(moved.id, expected_version=b.planning.get_placement(moved.id).version)
    a.planning.create_fixed_block(FixedBlock(label="Class", category="event", planned_date=MON, timezone="UTC",
                                             planned_start=start, planned_end=start + timedelta(hours=1)))
    assert a.sync_now().status == "ok" and b.sync_now().status == "ok" and b.sync.list_conflicts() == []
    assert a.sync_now().status == "ok"

    # A, which had the manual placement blocked by its own new class, can now regenerate around it.
    workflow.generate(a.planning, range_start=MON, range_end=MON, timezone_name="UTC", scope=RangeScope.ELIGIBLE)
    [replacement] = [p for p in a.planning.placements_for_date(MON) if p.task_id == essay.id]
    assert replacement.id != moved.id
    assert replacement.planned_end <= start or replacement.planned_start >= start + timedelta(hours=1)


# -----------------------------------------------------------------------------
# Refused requests, replays and crashes
# -----------------------------------------------------------------------------


class RefusingTransport(InProcessTransport):
    """A server that refuses, as a whole, any request carrying a task named BAD."""

    def push(self, token: str, operations: list[dict]) -> list[dict]:
        if any((op.get("payload") or {}).get("name") == "BAD" for op in operations):
            raise ProtocolError("The backend refused the request (HTTP 422 validation_error).")
        return super().push(token, operations)


def test_a_refused_request_is_narrowed_to_its_unit_and_becomes_actionable(alice_server, make_device) -> None:
    device = make_device("refused", RefusingTransport(alice_server.client))
    device.sign_in("alice@example.com")
    good, bad, other = device.add_task("Good"), device.add_task("BAD"), device.add_task("Also good")

    report = device.sync_now()

    assert report.status == "ok" and report.conflicts == 1
    names = {task["name"] for task in alice_server.get("alice@example.com", "/tasks", limit=50)["items"]}
    assert names == {"Good", "Also good"}  # nothing else was held back
    [conflict] = device.sync.list_conflicts()
    assert (conflict.entity_id, conflict.kind, conflict.error["code"]) == (str(bad.id), "push_rejected",
                                                                           "request_refused")
    assert any("not retried automatically" in line for line in conflict_view(conflict, {}).context)
    assert device.sync_now().pushed == 0  # never retried on its own
    device.reopen()
    assert len(device.sync.list_conflicts()) == 1  # durable
    device.sync.resolve_conflict(conflict.id, "accept_remote")
    assert device.planning.get_tasks_including_deleted([bad.id])[bad.id].deleted_at is not None
    assert {good.id, other.id} <= {task.id for task in device.planning.list_tasks()}


def test_a_replayed_series_unit_adds_no_versions_or_change_log_entries(alice_server, make_device) -> None:
    transport = FlakyTransport(alice_server.client)
    device = make_device("replay", transport)
    device.sign_in("alice@example.com")
    walk = walk_series(device)
    series_ops.expand_occurrences(device.planning, MON, MON + 2 * DAY)
    transport.lose_push_response = 1  # the server commits; the answer is lost

    assert device.sync_now().status == "offline"
    logged, stored = change_log_size(alice_server), server_occurrences(alice_server, walk.id)

    assert device.sync_now().status == "ok"  # the same op_ids again

    assert change_log_size(alice_server) == logged
    assert server_occurrences(alice_server, walk.id) == stored  # same versions, no duplicates
    assert device.sync_now().pushed == 0 and not device.dirty()


def test_a_crash_before_acknowledgement_replays_without_duplicates(alice_server, make_device, monkeypatch) -> None:
    device = make_device("crash", InProcessTransport(alice_server.client))
    device.sign_in("alice@example.com")
    walk = walk_series(device)
    series_ops.expand_occurrences(device.planning, MON, MON + DAY)
    real = SyncEngine.acknowledge

    def crash(*_args, **_kwargs):
        raise RuntimeError("power lost before the answer was recorded (injected)")

    monkeypatch.setattr(SyncEngine, "acknowledge", crash)
    with pytest.raises(RuntimeError):
        device.sync_now()
    monkeypatch.setattr(SyncEngine, "acknowledge", real)
    logged = change_log_size(alice_server)
    device.reopen()

    assert device.sync_now().status == "ok"

    assert change_log_size(alice_server) == logged  # answered from the recorded results
    assert len(server_occurrences(alice_server, walk.id)) == 2 and not device.dirty()


def test_a_crash_before_the_cursor_commit_replays_the_page(pair, monkeypatch) -> None:
    server, a, b = pair
    walk = walk_series(a)
    series_ops.expand_occurrences(a.planning, MON, MON + 2 * DAY)
    a.sync_now()
    cursor = b.sync._engine.store.account(b.sync._account_key).pull_cursor

    def crash(*_args, **_kwargs):
        raise RuntimeError("power lost while applying a page (injected)")

    monkeypatch.setattr(type(b.sync._engine.store), "set_cursor", crash)
    with pytest.raises(RuntimeError):
        b.sync_now()
    monkeypatch.undo()
    b.reopen()
    assert b.sync._engine.store.account(b.sync._account_key).pull_cursor == cursor
    assert walk.id not in b.planning.get_tasks_including_deleted([walk.id])  # the page's records rolled back with it

    assert b.sync_now().status == "ok"

    assert len(b.planning.occurrences_of_series([walk.id])[walk.id]) == 3  # once each
    assert b.sync.list_conflicts() == [] and not b.dirty()


# -----------------------------------------------------------------------------
# Authentication expiry
# -----------------------------------------------------------------------------


def test_an_expired_session_keeps_the_queue_and_the_conflicts(pair) -> None:
    server, a, b = pair
    shared = a.add_task("Shared")
    a.sync_now()
    b.sync_now()
    a.planning.update_task(shared.model_copy(update={"priority": 2}), expected_version=shared.version)
    a.sync_now()
    stored = b.planning.get_task(shared.id)
    b.planning.update_task(stored.model_copy(update={"priority": 3}), expected_version=stored.version)
    b.sync_now()
    assert len(b.sync.list_conflicts()) == 1
    b.add_task("Queued")
    real = b.transport.push

    def expired(token, operations):
        b.transport.push = real
        raise AuthenticationError("token expired (injected)")

    b.transport.push = expired
    assert b.sync_now().status == "auth_required"
    assert b.sync_now().status != "ok"  # nothing is sent without a session
    key = b.sync._account_key
    b.reopen(keep_session=False)
    assert len(b.sync._engine.store.conflicts(key)) == 1 and b.dirty()  # both survive a restart

    b.sign_in("alice@example.com", associate=False)
    assert b.sync_now().status == "ok"
    names = {task["name"] for task in server.get("alice@example.com", "/tasks", limit=50)["items"]}
    assert "Queued" in names and len(b.sync.list_conflicts()) == 1
