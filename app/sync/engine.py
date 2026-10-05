"""
app/sync/engine.py

The synchronization rules (docs/sync-protocol.md), independent of threads
and of the network: SyncService (app/sync/service.py) calls these steps and
performs the network calls *between* them, never inside a SQLite
transaction.

Local revision vs. server version:
    - sync_dirty.local_rev counts local changes of a record since the server
      last acknowledged it (bumped by the v5 capture triggers in the same
      transaction as the change). It is never sent.
    - sync_shadows.server_version is the version of the last server state
      this device acknowledged (from a push result or a pull). Every update,
      delete or action is sent with base_version = that shadow version --
      never with a local counter -- so any number of offline edits are sent
      as one change against the correct precondition.

Push (prepare, then push_batch):
    prepare() turns each dirty record of the account into operations -- at
    most one pending chain per record (a record with unanswered operations
    or an open conflict waits):
        no shadow, live            -> create (its current content)
        no shadow, deleted/missing -> nothing (created and deleted offline)
        shadow, live               -> update (base = shadow version); for an
                                      execution: its new lifecycle actions and
                                      feedback as one atomic group
        shadow, deleted/missing    -> delete (base = shadow version); a
                                      placement's removal reason travels with it
        no shadow, placement tombstone that a later placement superseded
                                   -> a history create: the removed plan with
                                      its reason and successor (sent after the
                                      successor), so a plan made and moved or
                                      regenerated before the first sync keeps
                                      its lineage on the server
        shadow live, local placement tombstone RESCHEDULED
                                   -> one "reschedule" action (base = shadow
                                      version) that the server applies as one
                                      unit: the move, the replacement and the
                                      cancellation of the never-started
                                      execution. Until it is acknowledged, the
                                      replacement and that execution are part
                                      of it and send nothing of their own;
                                      afterwards the results' related records
                                      are their shadows
        shadow is a tombstone, local is live -> a conflict (never a revival)
    Operations are ordered so references resolve: creates/updates by
    ENTITY_ORDER (tasks after the tasks they depend on), then deletes in
    reverse (dependents first). Each operation is written to sync_outbox with
    a stable op_id and the local_rev it was built from.
    push_batch() sends pending operations (whole groups only) and records the
    answers in one transaction: an applied operation updates the shadow,
    leaves the outbox, and clears the dirty mark only if local_rev is
    unchanged -- an edit made while the request was in flight stays pending
    and is sent next time with the new shadow version as its base. A lost
    response leaves the operations in the outbox, and they are resent with
    the same op_ids (the server answers from its record). Conflicts and
    rejections become sync_conflicts rows and stop that record only.

Pull (apply_pull_page): a page of the server's change feed and the new
cursor are applied in one transaction, with change capture suppressed (no
echo). A change whose version is not newer than the shadow is skipped, so a
replayed page (e.g. after a crash before commit) is harmless. A change to a
record with a pending local change, or a collision with a different local
record for the same unique scope, is stored as a conflict instead of
overwriting local work.

Conflicts are resolved explicitly (resolve):
    accept_remote  the server state replaces the local record (or the local
                   change is discarded if the server never had the record)
    keep_local     the remote version becomes the new base and the local
                   change is sent again -- it can conflict again. Not allowed
                   when the server record is a tombstone (no silent revival)
                   or a different record owns the same unique scope.
Every decision is kept on the conflict row (resolution, resolved_at).
Accepting the server's state for a placement this device had rescheduled
also undoes the rest of that local move: its replacement (which the server
never accepted) is discarded, and the execution the move cancelled goes
back to its server state (or, never synchronized, to `scheduled`).

Milestone 6 (docs/sync-protocol.md, "Conflicts"):
    series_changed   an occurrence expanded here from an older (or since
                     deleted) series. One that only followed its series (no
                     exception state) follows the server's series at once,
                     without a conflict -- re-derived and sent again, or
                     retired -- and the members of its unit that failed only
                     with it are sent again. Otherwise (a local exception, or
                     a pending local change of the series) it is a conflict:
                     keep_local is refused (it would get
                     around the series edit); accept_remote takes the
                     server's series (unless it has its own pending change)
                     and re-derives the occurrence from it -- sent again --
                     or, when its date is no longer one of the series' or
                     the series is gone, retires it here (superseded; its
                     never-synchronized placements without work go with it).
    execution        accept_remote never deletes work sessions only this
                     device recorded: they are kept as a separate historical
                     execution (sent as a create) before the server's
                     version replaces the local one. keep_local is refused
                     when the local sessions do not continue the server's.
    refused request  a push the server refuses as a whole (malformed,
                     4xx) is narrowed to the units at fault
                     (SyncService), which become push_rejected conflicts --
                     actionable, never retried automatically.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.execution.db import SYNC_TABLES
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.planning.models import OccurrenceState, PlacementRemovalReason
from app.planning.recurrence import RecurrenceError, SeriesRule
from app.planning.repository import PlanningRepository
from app.planning.series import SERIES_CONTENT_FIELDS
from app.sync.mapping import (
    ENTITY_ORDER,
    PLACEMENT_HISTORY_FIELDS,
    TASK_TYPE_FIELDS,
    OCCURRENCE_BOOKKEEPING,
    DivergedHistory,
    LocalRecord,
    LocalRecords,
    execution_changes,
)
from app.sync.store import Account, Conflict, SyncStore
from app.sync.transport import PullPage


#: Ownerless live layers/records whose scope a live record of the account already holds (parameter: user id).
_PREFERENCE_COLLISIONS = (
    "SELECT mine.id FROM preference_overrides AS mine JOIN preference_overrides AS theirs "
    "ON theirs.scope = mine.scope AND theirs.scope_date IS mine.scope_date "
    "WHERE mine.user_id IS NULL AND theirs.user_id = ? AND mine.deleted_at IS NULL AND theirs.deleted_at IS NULL"
)
_GENERATION_COLLISIONS = (
    "SELECT mine.id, mine.planned_date FROM schedule_generations AS mine JOIN schedule_generations AS theirs "
    "ON theirs.planned_date = mine.planned_date WHERE mine.user_id IS NULL AND theirs.user_id = ? "
    "AND mine.deleted_at IS NULL AND theirs.deleted_at IS NULL"
)


#: A compound series change larger than this is sent as independent operations (one push carries at most 200).
_MAX_GROUP_OPERATIONS = 150


@dataclass
class _SeriesGroup:
    """The pending task operations of one recurring lineage (see SyncEngine._series_groups)."""

    root: str
    upserts: list = field(default_factory=list)
    reschedules: list = field(default_factory=list)
    deletes: list = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.upserts) + len(self.reschedules) + len(self.deletes)


class AssociationError(Exception):
    """The previewed association cannot be applied (it changed since the preview, or it has problems)."""

    def __init__(self, message: str, *, code: str, preview: "AssociationPreview | None" = None) -> None:
        super().__init__(message)
        self.code = code
        self.preview = preview


@dataclass(frozen=True)
class AssociationPreview:
    """What associate_local_data would claim for the account; nothing has been written."""

    account_key: str
    #: Ownerless records per entity type that would become the account's (tombstones included).
    counts: dict[str, int]
    #: The same, live records only.
    live_counts: dict[str, int]
    #: Records that cannot be claimed as they are (e.g. a preference layer for a scope the account already has).
    problems: list[dict]
    #: Identifies exactly these records at these versions; pass it back to confirm.
    token: str

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class ConflictResolutionError(Exception):
    """The requested resolution is not possible for this conflict."""


@dataclass
class PushOutcome:
    sent: int = 0
    applied: int = 0
    conflicts: int = 0
    #: Occurrences this device only expanded that followed a changed series at once (series_changed).
    followed: int = 0


@dataclass
class PullOutcome:
    applied: int = 0
    skipped: int = 0
    conflicts: list[str] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _moment(value) -> datetime:
    """An ISO 8601 instant (text or datetime) for comparison."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class SyncEngine:
    def __init__(self, connection, clock: Callable[[], datetime] = _utcnow) -> None:
        self.store = SyncStore(connection)
        self.records = LocalRecords(PlanningRepository(connection), ExecutionRepository(connection))
        self._connection = connection
        self._clock = clock
        #: Records the last prepare() held back for a server without recurrence support.
        self.held = 0

    # ------------------------------------------------------------------
    # Ownerless local data
    # ------------------------------------------------------------------

    def association_preview(self, account: Account) -> AssociationPreview:
        """
        What the claim step would do, read in one snapshot: how many ownerless
        records of each type it would assign to `account`, and the ones that
        cannot be claimed as they are -- an ownerless preference layer or
        schedule record for a scope (the user layer, a date) the account
        already has a live record for would break that scope's uniqueness.
        Records owned by any account are never part of it.
        """
        counts: dict[str, int] = {}
        live: dict[str, int] = {}
        identity: list = []
        problems: list[dict] = []
        with self.store.transaction():
            for entity_type, table in SYNC_TABLES:
                rows = self._connection.execute(
                    f"SELECT id, version, deleted_at FROM {table} WHERE user_id IS NULL ORDER BY id"
                ).fetchall()
                identity.extend((entity_type, row["id"], row["version"]) for row in rows)
                if entity_type == "task_type":
                    continue  # claimed with its tasks, but not a record the user counts (a label of theirs)
                counts[entity_type] = len(rows)
                live[entity_type] = sum(1 for row in rows if row["deleted_at"] is None)
            for row in self._connection.execute(_PREFERENCE_COLLISIONS, (account.user_id,)).fetchall():
                problems.append({"entity_type": "preference", "id": row["id"], "code": "scope_taken",
                                 "message": "The account already has preferences for this scope (the user layer or "
                                            "this date); delete one of the two layers first."})
            for row in self._connection.execute(_GENERATION_COLLISIONS, (account.user_id,)).fetchall():
                problems.append({"entity_type": "schedule_generation", "id": row["id"], "code": "scope_taken",
                                 "message": f"The account already has a saved schedule record for {row[1]}; "
                                            "reset that date in one of the two workspaces first."})
        token = hashlib.sha256(json.dumps([account.account_key, identity], separators=(",", ":")).encode()).hexdigest()
        return AssociationPreview(account.account_key, counts, live, problems, token)

    def associate_local_data(self, account: Account, *, confirmation: str | None = None) -> dict[str, int]:
        """
        The explicit claim step: every ownerless local record becomes the
        account's (one logical mutation each: version + 1), and the account
        becomes active so later local records are owned by it too. Records of
        other accounts are untouched. The claimed records are then pushed by
        the next sync. Nothing calls this implicitly (not even signing in).

        With `confirmation` (an association_preview token) it applies only if
        the ownerless records are still exactly the previewed ones, and never
        when the preview has problems (AssociationError; nothing changes).
        """
        now = self._clock().isoformat()
        counts: dict[str, int] = {}
        with self.store.transaction():
            if confirmation is not None:
                preview = self.association_preview(account)
                if preview.token != confirmation:
                    raise AssociationError("The local records changed since the preview; review it again.",
                                           code="preview_changed", preview=preview)
                if preview.problems:
                    raise AssociationError("Some local records cannot be associated as they are.",
                                           code="association_blocked", preview=preview)
            for entity_type, table in SYNC_TABLES:
                cursor = self._connection.execute(
                    f"UPDATE {table} SET user_id = ?, version = version + 1, updated_at = ? WHERE user_id IS NULL",
                    (account.user_id, now),
                )
                if entity_type != "task_type":  # see association_preview
                    counts[entity_type] = cursor.rowcount
            self.store.mark_associated(account.account_key)
            self.store.set_active(account.account_key)
        return counts

    # ------------------------------------------------------------------
    # Push
    # ------------------------------------------------------------------

    def _waiting(self, key: str, entity_type: str, wire_id: str) -> bool:
        return self.store.has_ops(key, entity_type, wire_id) or self.store.open_conflict(key, entity_type, wire_id) is not None

    def prepare(self, account: Account, *, recurrence: bool = True, manual_placements: bool = True,
                scheduling_modes: bool = True, task_types: bool = True) -> int:
        """
        Materialize operations for the account's dirty records (see the module
        docstring). Returns how many. recurrence=False (a server without the
        "recurrence_occurrences" feature, app/sync/service.py) holds back every
        record carrying recurrence data -- series anchors, occurrences and what
        refers to them -- instead of letting an older server drop those fields;
        they stay dirty and are counted in self.held. manual_placements=False
        (a server without "manual_placements") leaves placement origin/manual
        intent and cancel reasons out of the payloads. task_types=False (a server
        without "task_types") holds task-type records back and leaves a task's
        type and a placement's planning snapshot out of the payloads: they stay
        recorded here and are uploaded once the server supports them
        (_requeue_history_fields).
        """
        key = account.account_key
        self.held = 0
        self.records.manual_placements = manual_placements
        self.records.task_types = task_types
        with self.store.transaction():
            if task_types:
                self._requeue_history_fields(account)
            upserts: list[tuple[LocalRecord, object]] = []
            reschedules: list[tuple[LocalRecord, object, int]] = []
            histories: list[LocalRecord] = []
            reserved: list[LocalRecord] = []
            # (type, wire id, local id, rev, shadow, payload)
            deletes: list[tuple[str, str, str, int, object, dict | None]] = []
            for entity_type, local_id, rev in self.store.dirty():
                local = self.records.read(entity_type, local_id)
                if local is None:  # physically removed locally (a history reset)
                    found = self.store.shadow_for_legacy_id(key, local_id) if entity_type == "execution" else None
                    if found is None and entity_type != "execution":
                        shadow = self.store.shadow(key, entity_type, local_id)
                        found = (local_id, shadow) if shadow else None
                    if found is None:
                        if not self.store.any_shadow(entity_type, local_id):  # never synced by any account
                            self.store.clear_dirty(entity_type, local_id, if_rev=rev)
                    elif found[1].deleted:
                        self.store.clear_dirty(entity_type, local_id, if_rev=rev)
                    elif not self._waiting(key, entity_type, found[0]):
                        deletes.append((entity_type, found[0], local_id, rev, found[1], None))
                    continue
                if local.owner != account.user_id or self._waiting(key, entity_type, local.wire_id):
                    continue
                if not recurrence and self._needs_recurrence_support(local):
                    self.held += 1
                    continue
                if not scheduling_modes and self._uses_new_mode(local):
                    self.held += 1  # an older server refuses the value: wait for it to be upgraded
                    continue
                if not task_types and entity_type == "task_type":
                    self.held += 1  # an older server does not know the record type: it stays queued here
                    continue
                if self._part_of_pending_reschedule(key, local):
                    continue
                shadow = self.store.shadow(key, entity_type, local.wire_id)
                if local.deleted:
                    if shadow is not None and not shadow.deleted:
                        if self._is_rescheduled(local):
                            reschedules.append((local, shadow, rev))
                        else:
                            payload = (self.records.placement_removal(local) if entity_type == "placement"
                                       else self.records.task_removal(local) if entity_type == "task" else None)
                            deletes.append((entity_type, local.wire_id, local_id, rev, shadow, payload))
                    elif shadow is None and self._is_lineage_history(local):
                        histories.append(local)
                    elif shadow is None and entity_type == "task" and local.model.is_occurrence:
                        reserved.append(local)  # a slot suppressed before it ever reached the server
                    else:
                        self.store.clear_dirty(entity_type, local_id, if_rev=rev)
                elif shadow is not None and shadow.deleted:
                    self.store.add_conflict(
                        key, entity_type, local.wire_id, local_id, "push_conflict", base_version=shadow.server_version,
                        local_record=local.payload, remote_record=shadow.record,
                        error={"code": "deleted", "message": "The record was deleted on the server."},
                    )
                else:
                    upserts.append((local, shadow))

            groups = self._series_groups(upserts, reschedules, deletes)
            grouped_upserts = {id(item) for group in groups for item in group.upserts}
            grouped_reschedules = {id(item) for group in groups for item in group.reschedules}
            grouped_deletes = {id(item) for group in groups for item in group.deletes}
            upserts = [item for item in upserts if id(item) not in grouped_upserts]
            reschedules = [item for item in reschedules if id(item) not in grouped_reschedules]
            deletes = [item for item in deletes if id(item) not in grouped_deletes]

            count = 0
            rank = {entity_type: index for index, entity_type in enumerate(ENTITY_ORDER)}
            ordered = self._ordered_upserts(upserts)
            before_tasks = [item for item in ordered if rank[item[0].entity_type] < rank["task"]]
            tasks = [item for item in ordered if item[0].entity_type == "task"]
            after_tasks = [item for item in ordered if rank["task"] < rank[item[0].entity_type] <= rank["placement"]]
            for local, shadow in before_tasks:
                count += self._materialize(key, local, shadow)
            # Tasks and compound series changes (each one atomic group), references first.
            for unit in self._ordered_task_units(tasks, groups):
                if isinstance(unit, _SeriesGroup):
                    count += self._materialize_group(key, unit)
                else:
                    count += self._materialize(key, *unit)
            for local in sorted(reserved, key=lambda item: item.wire_id):
                self.store.add_op(key, "task", local.wire_id, local.local_id, "create",
                                  payload=self.records.reserved_slot_payload(local),
                                  local_rev=self.store.dirty_rev("task", local.local_id) or 1)
                count += 1
            # Moves go after the placement creates/updates (their tasks exist) and before executions.
            for local, shadow in after_tasks:
                count += self._materialize(key, local, shadow)
            for local, shadow, rev in sorted(reschedules, key=lambda item: item[0].wire_id):
                count += self._materialize_reschedule(key, local, shadow, rev)
            for local in self._ordered_histories(histories):  # each after the placement that superseded it
                payload = {**local.payload, **self.records.placement_removal(local)}
                self.store.add_op(key, "placement", local.wire_id, local.local_id, "create", payload=payload,
                                  local_rev=self.store.dirty_rev("placement", local.local_id) or 1)
                count += 1
            for local, shadow in (item for item in ordered if rank[item[0].entity_type] > rank["placement"]):
                count += self._materialize(key, local, shadow)
            for entity_type, wire_id, local_id, rev, shadow, payload in self._ordered_deletes(deletes):
                self.store.add_op(key, entity_type, wire_id, local_id, "delete", base_version=shadow.server_version,
                                  payload=payload, local_rev=rev)
                count += 1
            return count

    # ------------------------------------------------------------------
    # Recurring series (docs/recurrence.md)
    # ------------------------------------------------------------------

    @staticmethod
    def _uses_new_mode(local: LocalRecord) -> bool:
        """Whether a preference layer or schedule record names a mode added with docs/scheduling-modes.md."""
        if local.entity_type == "preference":
            mode = local.payload.get("overrides", {}).get("optimizer_mode") if local.payload else None
        elif local.entity_type == "schedule_generation":
            mode = local.payload.get("engine_mode") if local.payload else None
        else:
            return False
        return mode in ("early_finish", "night_owl", "catch_up")

    def _needs_recurrence_support(self, local: LocalRecord) -> bool:
        """Whether this record (or the task it belongs to) carries recurrence data an older server would drop."""
        if local.entity_type == "task":
            task = local.model
            return bool(task.series_id or task.series_predecessor_id
                        or (task.recurrence is not None and task.recurrence.configured))
        task_id = None
        if local.entity_type == "placement":
            task_id = local.model.task_id
        elif local.entity_type == "execution":
            task_id = local.model.task_id
        if task_id is None:
            return False
        task = self.records.planning.get_task(task_id, include_deleted=True)
        return task is not None and task.is_occurrence

    def _series_root(self, task, cache: dict) -> str | None:
        """The first segment of the lineage a series or occurrence belongs to (None for other tasks)."""
        if task.id in cache:
            return cache[task.id]
        current = task
        if task.is_occurrence:
            current = self.records.planning.get_task(task.series_id, include_deleted=True)
        root = None
        steps = 0
        while current is not None and current.is_series and steps < 64:
            root = str(current.id)
            if current.series_predecessor_id is None:
                break
            current = self.records.planning.get_task(current.series_predecessor_id, include_deleted=True)
            steps += 1
        cache[task.id] = root
        return root

    def _series_groups(self, upserts, reschedules, deletes) -> list["_SeriesGroup"]:
        """
        The compound changes of recurring series: every pending task operation
        of one lineage (its segments and their occurrences) -- updates,
        creates, the moves of its occurrences and the deletes of superseded
        occurrences with their placements -- when there is more than one. Each
        becomes one atomic group, so a split ("this and every later
        occurrence") never half-applies on the server. A lineage with more
        operations than fit one push is sent ungrouped.
        """
        cache: dict = {}
        families: dict[str, _SeriesGroup] = {}

        def family(task) -> "_SeriesGroup | None":
            root = self._series_root(task, cache) if task is not None else None
            return None if root is None else families.setdefault(root, _SeriesGroup(root))

        for item in upserts:
            if item[0].entity_type == "task" and (group := family(item[0].model)) is not None:
                group.upserts.append(item)
        for item in reschedules:
            task = self.records.planning.get_task(item[0].model.task_id, include_deleted=True)
            if task is not None and task.is_occurrence and (group := family(task)) is not None:
                group.reschedules.append(item)
        deleted_tasks: dict[str, _SeriesGroup] = {}
        for item in deletes:
            if item[0] != "task":
                continue
            task = self.records.planning.get_task(uuid.UUID(item[2]), include_deleted=True)
            if task is not None and (group := family(task)) is not None:
                group.deletes.append(item)
                deleted_tasks[str(task.id)] = group
        for item in deletes:
            if item[0] != "placement":
                continue
            placement = self.records.planning.get_placements([uuid.UUID(item[2])], include_deleted=True).get(
                uuid.UUID(item[2]))
            if placement is not None and str(placement.task_id) in deleted_tasks:
                deleted_tasks[str(placement.task_id)].deletes.append(item)
        return [group for group in families.values() if 1 < group.size <= _MAX_GROUP_OPERATIONS]

    def _ordered_task_units(self, tasks, groups) -> list:
        """Single task upserts and series groups, each after the tasks (or groups) it references."""
        units: dict[str, object] = {}
        unit_of: dict[str, str] = {}
        for item in tasks:
            units[item[0].wire_id] = item
            unit_of[item[0].wire_id] = item[0].wire_id
        for group in groups:
            units[f"group:{group.root}"] = group
            for local, _ in group.upserts:
                unit_of[local.wire_id] = f"group:{group.root}"

        def references(unit) -> set[str]:
            locals_ = [unit[0]] if isinstance(unit, tuple) else [local for local, _ in unit.upserts]
            found = set()
            for local in locals_:
                refs = list(local.payload.get("dependency_ids") or [])
                refs += [local.payload.get("series_id"), local.payload.get("series_predecessor_id")]
                found.update(unit_of[ref] for ref in refs if ref and ref in unit_of)
            return found

        ordered: list = []
        done: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in done or name in visiting:
                return
            visiting.add(name)
            for reference in sorted(references(units[name]) - {name}):
                visit(reference)
            visiting.discard(name)
            done.add(name)
            ordered.append(units[name])

        for name in sorted(units):
            visit(name)
        return ordered

    def _materialize_group(self, key: str, group: "_SeriesGroup") -> int:
        group_id = str(uuid.uuid4())
        count = 0
        for local, shadow in self._ordered_upserts(group.upserts):
            count += self._materialize(key, local, shadow, group_id=group_id)
        for local, shadow, rev in sorted(group.reschedules, key=lambda item: item[0].wire_id):
            payload = self.records.reschedule_payload(local)
            if payload is None:
                self.store.add_op(key, "placement", local.wire_id, local.local_id, "delete",
                                  base_version=shadow.server_version, local_rev=rev, group_id=group_id)
            else:
                self.store.add_op(key, "placement", local.wire_id, local.local_id, "action", action="reschedule",
                                  base_version=shadow.server_version, payload=payload, local_rev=rev,
                                  group_id=group_id)
            count += 1
        for entity_type, wire_id, local_id, rev, shadow, payload in self._ordered_deletes(group.deletes):
            self.store.add_op(key, entity_type, wire_id, local_id, "delete", base_version=shadow.server_version,
                              payload=payload, local_rev=rev, group_id=group_id)
            count += 1
        return count

    def _converges(self, local: LocalRecord, record: dict) -> bool:
        """
        A pulled occurrence that equals this device's own pending copy of the
        same slot (both devices expanded it): nothing to merge or conflict on.
        """
        if local.entity_type != "task" or not local.model.is_occurrence:
            return False
        if local.deleted != (record.get("deleted_at") is not None):
            return False
        return all(local.payload.get(name) == record.get(name) for name in local.payload
                   if name not in OCCURRENCE_BOOKKEEPING)

    @staticmethod
    def _is_lineage_history(local: LocalRecord) -> bool:
        """A placement tombstone that a later placement superseded (moved or re-placed): part of a lineage."""
        return (local.entity_type == "placement" and local.deleted and local.model.superseded_by_id is not None
                and local.model.removal_reason in (PlacementRemovalReason.RESCHEDULED,
                                                   PlacementRemovalReason.REGENERATED))

    @staticmethod
    def _ordered_histories(histories: list[LocalRecord]) -> list[LocalRecord]:
        """Successors first: a history create names its successor, which the server must already have."""
        by_id = {local.model.id: local for local in histories}

        def depth(local: LocalRecord) -> int:
            steps, current, seen = 0, local, set()
            while current.model.superseded_by_id in by_id and current.model.id not in seen:
                seen.add(current.model.id)
                current = by_id[current.model.superseded_by_id]
                steps += 1
            return steps

        return sorted(histories, key=lambda local: (depth(local), local.wire_id))

    @staticmethod
    def _is_rescheduled(local: LocalRecord) -> bool:
        """A placement tombstone left by an explicit local reschedule."""
        return (local.entity_type == "placement" and local.deleted
                and local.model.removal_reason == PlacementRemovalReason.RESCHEDULED
                and local.model.superseded_by_id is not None)

    def _pending_move(self, key: str, placement: LocalRecord | None) -> bool:
        """A local reschedule of `placement` that the server has not applied yet (its shadow is still live)."""
        if placement is None or not self._is_rescheduled(placement):
            return False
        shadow = self.store.shadow(key, "placement", placement.wire_id)
        return shadow is not None and not shadow.deleted

    def _awaiting_move(self, key: str, placement_id) -> bool:
        """
        Whether a local move that produced this placement has not reached the
        server yet: a predecessor's move is pending, or the predecessor is
        itself a replacement the server has not seen (a chain of offline moves).
        """
        predecessors = self.records.planning.placements_superseded_by([placement_id]).get(placement_id, [])
        for predecessor in predecessors:
            local = self.records.read("placement", str(predecessor.id))
            if local is None or not self._is_rescheduled(local):
                continue
            if self._pending_move(key, local):
                return True
            if self.store.shadow(key, "placement", local.wire_id) is None and self._awaiting_move(key, predecessor.id):
                return True
        return False

    def _part_of_pending_reschedule(self, key: str, local: LocalRecord) -> bool:
        """
        The replacement of a pending move (or of a chain of them), or the
        execution of a placement that is being moved or is such a replacement:
        the reschedule actions carry (or cause) their change, so they send
        nothing of their own until those are acknowledged or resolved.
        """
        if local.entity_type == "placement":
            return self._awaiting_move(key, local.model.id)
        if local.entity_type == "execution" and local.model.scheduled_task_id is not None:
            placement = self.records.read("placement", str(local.model.scheduled_task_id))
            return placement is not None and (
                self._pending_move(key, placement) or self._awaiting_move(key, placement.model.id))
        return False

    def _materialize_reschedule(self, key: str, local: LocalRecord, shadow, rev: int) -> int:
        payload = self.records.reschedule_payload(local)
        if payload is None:  # the replacement is gone locally: only the removal can be sent (reason unknown)
            self.store.add_op(key, "placement", local.wire_id, local.local_id, "delete",
                              base_version=shadow.server_version, local_rev=rev)
            return 1
        self.store.add_op(key, "placement", local.wire_id, local.local_id, "action", action="reschedule",
                          base_version=shadow.server_version, payload=payload, local_rev=rev)
        return 1

    def _ordered_upserts(self, upserts):
        rank = {entity_type: index for index, entity_type in enumerate(ENTITY_ORDER)}
        tasks = {local.wire_id: (local, shadow) for local, shadow in upserts if local.entity_type == "task"}
        ordered_tasks: list = []
        visiting: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting or task_id not in tasks or tasks[task_id] in ordered_tasks:
                return
            visiting.add(task_id)
            payload = tasks[task_id][0].payload
            # Dependencies, an occurrence's series and a segment's predecessor exist on the server first.
            for reference in [*payload["dependency_ids"], payload.get("series_id"), payload.get("series_predecessor_id")]:
                if reference:
                    visit(reference)
            ordered_tasks.append(tasks[task_id])

        for task_id in sorted(tasks):
            visit(task_id)
        others = sorted((item for item in upserts if item[0].entity_type != "task"),
                        key=lambda item: rank[item[0].entity_type])
        return [item for item in others if rank[item[0].entity_type] < rank["task"]] + ordered_tasks + [
            item for item in others if rank[item[0].entity_type] > rank["task"]
        ]

    def _ordered_deletes(self, deletes):
        rank = {entity_type: index for index, entity_type in enumerate(ENTITY_ORDER)}
        # Dependents before what they depend on: executions first ... projects last; tasks that depend on
        # other deleted tasks first.
        task_deps = {}
        for entity_type, wire_id, _, _, shadow, _ in deletes:
            if entity_type == "task":
                task_deps[wire_id] = set(shadow.record.get("dependency_ids") or [])

        def depth(task_id: str, seen=frozenset()) -> int:
            dependents = [other for other, deps in task_deps.items() if task_id in deps and other not in seen]
            return 1 + max((depth(other, seen | {task_id}) for other in dependents), default=0)

        return sorted(deletes, key=lambda item: (-rank[item[0]], -depth(item[1]) if item[0] == "task" else 0))

    def _requeue_history_fields(self, account: Account) -> None:
        """
        Once per account, when its server first supports "task_types": mark the
        tasks and placements whose acknowledged server copy lacks a type or a
        planning-snapshot field this device recorded, so they are uploaded --
        fields an older server could not take are never lost. Only a field the
        server has no value for is ever sent this way (a snapshot is immutable).
        """
        flag = f"history_fields_requeued:{account.account_key}"
        if self.store.setting(flag) is not None:
            return
        rows = self._connection.execute(
            "SELECT entity_type, entity_id, record FROM sync_shadows WHERE account_key = ? AND deleted = 0 "
            "AND entity_type IN ('task', 'placement')", (account.account_key,),
        ).fetchall()
        for row in rows:
            names = TASK_TYPE_FIELDS if row["entity_type"] == "task" else PLACEMENT_HISTORY_FIELDS
            remote = json.loads(row["record"])
            if all(remote.get(name) is not None for name in names):
                continue
            local = self.records.read(row["entity_type"], row["entity_id"])
            if local is None or local.deleted or local.owner != account.user_id:
                continue
            if any(local.payload.get(name) is not None and remote.get(name) is None for name in names):
                self.store.ensure_dirty(row["entity_type"], row["entity_id"])
        self.store.set_setting(flag, self._clock().isoformat())

    @staticmethod
    def _update_payload(local: LocalRecord, shadow) -> dict:
        """
        A placement update never contradicts a snapshot value the server already
        holds (it refuses a changed snapshot): those fields repeat the server's.
        """
        if local.entity_type != "placement":
            return local.payload
        held = {name: shadow.record[name] for name in ("task_category", *PLACEMENT_HISTORY_FIELDS)
                if name in local.payload and shadow.record.get(name) is not None}
        return {**local.payload, **held}

    def _materialize(self, key: str, local: LocalRecord, shadow, *, group_id: str | None = None) -> int:
        rev = self.store.dirty_rev(local.entity_type, local.local_id) or 1
        add = self.store.add_op
        if shadow is None:
            payload = dict(local.payload)
            if local.entity_type == "execution":
                payload["historical_reference"] = not self.records.links_resolve(local)
            add(key, local.entity_type, local.wire_id, local.local_id, "create", payload=payload, local_rev=rev,
                group_id=group_id)
            return 1
        if local.entity_type != "execution":
            add(key, local.entity_type, local.wire_id, local.local_id, "update", base_version=shadow.server_version,
                payload=self._update_payload(local, shadow), local_rev=rev, group_id=group_id)
            return 1
        try:
            changes = execution_changes(shadow.record, local)
        except DivergedHistory as error:
            self.store.add_conflict(
                key, "execution", local.wire_id, local.local_id, "push_rejected", base_version=shadow.server_version,
                local_record=local.payload, remote_record=shadow.record,
                error={"code": "diverged_history", "message": str(error)},
            )
            return 0
        if not changes:
            self.store.clear_dirty("execution", local.local_id, if_rev=rev)
            return 0
        group = str(uuid.uuid4()) if len(changes) > 1 else None
        for offset, (kind, action, payload) in enumerate(changes):
            add(key, "execution", local.wire_id, local.local_id, kind, action=action,
                base_version=shadow.server_version + offset, payload=payload, group_id=group, local_rev=rev)
        return len(changes)

    def next_batch(self, account: Account, limit: int) -> list:
        """The next pending operations to send: at most `limit`, never splitting a group."""
        batch = []
        for op in self.store.pending_ops(account.account_key):
            if len(batch) >= limit and (op.group_id is None or op.group_id != batch[-1].group_id):
                break
            batch.append(op)
        return batch

    def acknowledge(self, account: Account, batch: list, results: list[dict]) -> PushOutcome:
        """Record the server's answers to `batch` in one transaction (see the module docstring)."""
        key = account.account_key
        outcome = PushOutcome(sent=len(batch))
        by_op = {op.op_id: op for op in batch}
        failed_entities: dict[tuple[str, str], list[tuple[object, dict]]] = {}
        with self.store.transaction():
            for result in results:
                op = by_op.get(result["op_id"])
                if op is None:
                    continue
                if result["status"] == "applied":
                    self.store.put_shadow(key, op.entity_type, result["record"])
                    # The other records a reschedule changed; they stay dirty and are compared next round.
                    for related in result.get("related") or []:
                        self.store.put_shadow(key, related["entity_type"], related["record"])
                    self.store.delete_op(op.op_id)
                    # A task type travels with its task: the user's counts (pushed, pulled, pending) are of the
                    # records they made, so the type record is synchronized but not counted.
                    outcome.applied += op.entity_type != "task_type"
                    if not self.store.has_ops(key, op.entity_type, op.entity_id):
                        # Only if nothing changed locally while the request was in flight.
                        self.store.clear_dirty(op.entity_type, op.local_id, if_rev=op.local_rev)
                else:
                    failed_entities.setdefault((op.entity_type, op.entity_id), []).append((op, result))
            # Units refused because the series of an occurrence they create changed: series id -> group ids.
            changed: dict[str, set] = {}
            for entity, failures in list(failed_entities.items()):
                culprit = next((op for op, r in failures if r["error"].get("code") == "series_changed"), None)
                if culprit is not None and culprit.group_id is not None:
                    changed.setdefault((culprit.payload or {}).get("series_id"), set()).add(culprit.group_id)
                if self._follow_changed_series(account, failures):
                    del failed_entities[entity]
                    outcome.followed += 1
            for entity, failures in list(failed_entities.items()):
                # A create of another occurrence of that same series, refused only because its unit was: sent
                # again on its own next round (it then succeeds, follows the series, or conflicts itself). Members
                # of any other compound change are never re-sent without the rest of their unit.
                if all(r["error"].get("code") == "group_failed" and op.kind == "create"
                       and (op.payload or {}).get("series_id") is not None
                       and op.group_id in changed.get((op.payload or {}).get("series_id"), ())
                       for op, r in failures):
                    self.store.delete_entity_ops(key, *entity)
                    self.store.ensure_dirty(entity[0], failures[0][0].local_id)
                    del failed_entities[entity]
            outcome.conflicts += self._record_failures(key, failed_entities)
        return outcome

    def _follow_changed_series(self, account: Account, failures: list) -> bool:
        """
        An occurrence this device only expanded (no exception state of its
        own) that the server refused as series_changed is not a user's
        intent: it follows the server's series at once (re-derived and sent
        again, or retired when its date is gone) -- unless this device has
        its own pending change of that series, which then conflicts
        normally. Anything else stays a conflict for the user to decide.
        """
        op, result = next(((o, r) for o, r in failures if r["error"].get("code") == "series_changed"), (None, None))
        if op is None or op.entity_type != "task":
            return False
        key, series = account.account_key, result["error"].get("current")
        local = self.records.read("task", op.local_id)
        if local is None or local.deleted or local.model.occurrence_state is not None or series is None:
            return False
        series_local = self.records.read("task", self.records.local_id_for("task", series))
        if series_local is not None and (self.store.dirty_rev("task", series_local.local_id) is not None
                                         or self.store.has_ops(key, "task", series_local.wire_id)):
            return False
        with self.store.applying_remote():
            self.records.store("task", series, account.user_id,
                               (series_local.model.version + 1) if series_local else 1)
            self.store.put_shadow(key, "task", series)
            resend = self._follow_series(account, local)
        self.store.delete_entity_ops(key, "task", op.entity_id)
        if resend:
            self.store.ensure_dirty("task", op.local_id)
        else:
            self.store.clear_dirty("task", op.local_id)
        return True

    def refuse(self, account: Account, batch: list, message: str) -> PushOutcome:
        """
        The server refused `batch` as a whole (a 4xx that is not an
        authentication failure) and it cannot be narrowed further: each of its
        records becomes a push_rejected conflict (code request_refused) and
        leaves the outbox, so the user sees it and decides -- it is never
        retried automatically, and nothing else waits behind it.
        """
        failures: dict[tuple[str, str], list[tuple[object, dict]]] = {}
        for op in batch:
            failures.setdefault((op.entity_type, op.entity_id), []).append(
                (op, {"status": "rejected", "error": {"code": "request_refused", "message": message}}))
        with self.store.transaction():
            conflicts = self._record_failures(account.account_key, failures)
        return PushOutcome(sent=len(batch), conflicts=conflicts)

    def _record_failures(self, key: str, failed_entities: dict) -> int:
        for (entity_type, entity_id), failures in failed_entities.items():
            op, result = next(((o, r) for o, r in failures if r["error"].get("code") != "group_failed"), failures[0])
            local = self.records.read(entity_type, op.local_id)
            self.store.add_conflict(
                key, entity_type, entity_id, op.local_id,
                "push_conflict" if result["status"] == "conflict" else "push_rejected",
                op_id=op.op_id, base_version=op.base_version, local_record=local.payload if local else None,
                remote_record=result["error"].get("current"), error=result["error"],
            )
            self.store.delete_entity_ops(key, entity_type, entity_id)
        return len(failed_entities)

    # ------------------------------------------------------------------
    # Pull
    # ------------------------------------------------------------------

    def apply_pull_page(self, account: Account, page: PullPage) -> PullOutcome:
        """Apply one page of server changes and advance the cursor, atomically, without echo."""
        outcome = PullOutcome()
        with self.store.transaction(), self.store.applying_remote():
            for change in page.changes:
                self._apply_change(account, change, outcome)
            self.store.set_cursor(account.account_key, page.cursor)
        return outcome

    def _apply_change(self, account: Account, change: dict, outcome: PullOutcome) -> None:
        key, entity_type, record = account.account_key, change["entity_type"], change["record"]
        wire_id = str(change["entity_id"])
        shadow = self.store.shadow(key, entity_type, wire_id)
        if shadow is not None and change["version"] <= shadow.server_version:
            outcome.skipped += 1  # our own acknowledged change, or an older state
            return
        local_id = self.records.local_id_for(entity_type, record)
        open_conflict = self.store.open_conflict(key, entity_type, wire_id)
        if open_conflict is not None:
            self.store.update_conflict_remote(open_conflict.id, record)  # resolve against the newest server state
            return
        local = self.records.read(entity_type, local_id)
        if local is not None and local.owner in (None, account.user_id) and self._part_of_pending_reschedule(key, local):
            # Its local change belongs to a move that is still being decided: the server state becomes the
            # base the move is resolved against (applied: the move's result; refused: what is restored).
            self.store.put_shadow(key, entity_type, record)
            outcome.skipped += 1
            return
        pending = self.store.dirty_rev(entity_type, local_id) is not None or self.store.has_ops(key, entity_type, wire_id)
        if local is not None and local.owner not in (None, account.user_id):
            self._pull_conflict(key, entity_type, wire_id, local_id, shadow, local, record, "owned_by_another_account")
            outcome.conflicts.append(wire_id)
            return
        if local is not None and pending and self._converges(local, record):
            # Both devices materialized the same occurrence: adopt the server's version as the base, keep ours.
            self.store.put_shadow(key, entity_type, record)
            self.store.clear_dirty(entity_type, local_id)
            self.store.delete_entity_ops(key, entity_type, wire_id)
            outcome.skipped += 1
            return
        if local is not None and pending:
            self._pull_conflict(key, entity_type, wire_id, local_id, shadow, local, record, "concurrent_change")
            outcome.conflicts.append(wire_id)
            return
        collision = self._collision(entity_type, record, local_id)
        if collision is not None:
            self._pull_conflict(key, entity_type, wire_id, collision.local_id, shadow, collision, record,
                                "same_scope_elsewhere")
            outcome.conflicts.append(wire_id)
            return
        if local is None and record["deleted_at"] is not None:
            if not self._keeps_lineage(entity_type, record):
                self.store.put_shadow(key, entity_type, record)  # never seen here; nothing to delete
                return
        version = (local.model.version + 1) if local is not None else 1
        self.records.store(entity_type, record, account.user_id, version)
        self.store.put_shadow(key, entity_type, record)
        outcome.applied += entity_type != "task_type"

    def _keeps_lineage(self, entity_type: str, record: dict) -> bool:
        """
        A removed placement never seen here is still stored when it is part of a
        lineage (a later placement superseded it) and its task is here: the
        original plan of a moved or regenerated occurrence stays readable. A
        removed occurrence of a series this device has is stored too: its slot
        stays reserved here, so a local expansion never mints it again
        (docs/recurrence.md).
        """
        if entity_type == "task" and record.get("series_id"):
            return self.records.planning.get_task(uuid.UUID(record["series_id"]), include_deleted=True) is not None
        if entity_type != "placement" or not record.get("superseded_by_id"):
            return False
        return self.records.planning.get_task(uuid.UUID(record["task_id"]), include_deleted=True) is not None

    def _pull_conflict(self, key, entity_type, wire_id, local_id, shadow, local, record, code) -> None:
        self.store.add_conflict(
            key, entity_type, wire_id, local_id, "pull_conflict",
            base_version=shadow.server_version if shadow else None, local_record=local.payload, remote_record=record,
            error={"code": code, "message": "The server changed a record that also has local changes."},
        )
        self.store.delete_entity_ops(key, entity_type, wire_id)

    def _collision(self, entity_type: str, record: dict, local_id: str) -> LocalRecord | None:
        """A different live local record that owns the same unique scope as the pulled one."""
        if record["deleted_at"] is not None:
            return None
        if entity_type == "preference":
            sql = ("SELECT id FROM preference_overrides WHERE scope = ? AND scope_date IS ? AND deleted_at IS NULL "
                   "AND id <> ?")
            params = (record["scope"], record["date"], local_id)
        elif entity_type == "schedule_generation":
            sql = "SELECT id FROM schedule_generations WHERE planned_date = ? AND deleted_at IS NULL AND id <> ?"
            params = (record["planned_date"], local_id)
        elif entity_type == "execution" and record["scheduled_task_id"]:
            sql = "SELECT id FROM executions WHERE scheduled_task_id = ? AND deleted_at IS NULL AND id <> ?"
            params = (record["scheduled_task_id"], local_id)
        else:
            return None
        row = self._connection.execute(sql, params).fetchone()
        return self.records.read(entity_type, row[0]) if row is not None else None

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------

    def conflicts(self, account: Account, status: str | None = "open") -> list[Conflict]:
        return self.store.conflicts(account.account_key, status)

    def allowed_resolutions(self, conflict: Conflict) -> dict[str, str | None]:
        """
        {choice: None if allowed, else why not} for an open conflict -- the
        same rules resolve() enforces, so a UI offers only what will work.
        There is no merge.
        """
        remote = conflict.remote_record
        collision = remote is not None and (
            remote["id"] != conflict.entity_id
            or self.records.local_id_for(conflict.entity_type, remote) != conflict.local_id
        )
        keep_local = None
        code = (conflict.error or {}).get("code")
        if code == "series_changed":
            keep_local = ("The recurring series changed on the server after this occurrence was created here; "
                          "keeping the local copy would get around that change. Accept the server's series: the "
                          "occurrence then follows it.")
        elif remote is not None and remote.get("deleted_at") is not None:
            keep_local = ("The record was deleted on the server; keeping the local version would bring it back. "
                          "Accept the deletion instead.")
        elif collision:
            keep_local = "Another record owns this scope on the server."
        elif conflict.entity_type == "execution" and remote is not None and self._diverged(conflict, remote):
            keep_local = ("The work sessions recorded here do not continue the server's, so they cannot be sent as "
                          "changes. Accept the server's version: the sessions only this device has are kept as a "
                          "separate record.")
        return {"accept_remote": None, "keep_local": keep_local}

    def _diverged(self, conflict: Conflict, remote: dict) -> bool:
        local = self.records.read("execution", conflict.local_id)
        if local is None:
            return False
        try:
            execution_changes(remote, local)
        except DivergedHistory:
            return True
        return False

    def resolve(self, account: Account, conflict_id: str, choice: str) -> Conflict:
        conflict = self.store.conflict(conflict_id)
        if conflict is None or conflict.account_key != account.account_key:
            raise ConflictResolutionError("No such conflict for this account.")
        if conflict.status != "open":
            raise ConflictResolutionError("The conflict is already resolved.")
        if choice not in ("accept_remote", "keep_local"):
            raise ConflictResolutionError("Choose accept_remote or keep_local.")
        refused = self.allowed_resolutions(conflict).get(choice)
        if refused is not None:
            raise ConflictResolutionError(refused)
        if (conflict.error or {}).get("code") == "series_changed":
            return self._resolve_series_change(account, conflict)
        remote = conflict.remote_record
        # A collision: the server's record for this scope is a *different* record than the local one.
        collision = remote is not None and (
            remote["id"] != conflict.entity_id
            or self.records.local_id_for(conflict.entity_type, remote) != conflict.local_id
        )
        key, entity_type = account.account_key, conflict.entity_type
        with self.store.transaction():
            if choice == "keep_local":
                if remote is not None and remote.get("deleted_at") is not None:
                    raise ConflictResolutionError(
                        "The record was deleted on the server; keeping the local version would silently bring it "
                        "back. Accept the deletion instead."
                    )
                if collision:
                    raise ConflictResolutionError("Another record owns this scope on the server; accept it instead.")
                if remote is not None:
                    self.store.put_shadow(key, entity_type, remote)  # the new precondition
                self.store.ensure_dirty(entity_type, conflict.local_id)
            else:
                before = self.records.read(entity_type, conflict.local_id) if entity_type == "placement" else None
                kept_history = None
                with self.store.applying_remote():
                    if entity_type == "execution":
                        kept_history = self._keep_unacknowledged_history(
                            account, self.records.read("execution", conflict.local_id), remote)
                    self._accept_remote(account, conflict, remote, collision)
                    if before is not None and self._is_rescheduled(before):
                        self._undo_local_move(account, before)
                self.store.clear_dirty(entity_type, conflict.local_id)
                if kept_history is not None:
                    self.store.ensure_dirty("execution", kept_history)  # sent as a create of its own
            self.store.delete_entity_ops(key, entity_type, conflict.entity_id)
            resolution = {
                "choice": choice, "decided_at": self._clock().isoformat(),
                "base_version": conflict.base_version, "remote_version": remote.get("version") if remote else None,
            }
            if choice == "accept_remote" and kept_history is not None:
                resolution["kept_history_execution_id"] = kept_history
            self.store.resolve_conflict(conflict_id, resolution)
        return self.store.conflict(conflict_id)

    def _keep_unacknowledged_history(self, account: Account, local: LocalRecord | None, remote: dict | None) -> str | None:
        """
        Before the server's version of an execution replaces the local one:
        the work sessions only this device recorded are kept as a separate
        historical execution (same snapshot, no placement link, those
        sessions; derived metrics unknown), so accepting the server never
        deletes actual work. Returns its id, or None when there is nothing
        only this device has.
        """
        if local is None or local.deleted or remote is None:
            return None
        known = {_moment(work["started_at"]) for work in remote.get("sessions") or []}
        extra = [(started, ended) for started, ended in (local.sessions or []) if _moment(started) not in known]
        if not extra:
            return None
        execution = local.model
        if extra[-1][1] is None:
            status = ExecutionStatus.IN_PROGRESS
        elif execution.status in (ExecutionStatus.COMPLETED, ExecutionStatus.SKIPPED, ExecutionStatus.CANCELLED):
            status = execution.status
        else:
            status = ExecutionStatus.PAUSED
        task_id = execution.task_id
        if task_id is not None and self.records.planning.get_task(uuid.UUID(str(task_id))) is None:
            task_id = None  # its task is gone here: the copy is history only
        now = self._clock().isoformat()
        copy = execution.model_copy(update={
            "id": str(uuid.uuid4()), "scheduled_task_id": None, "task_id": task_id, "status": status,
            "cancel_reason": execution.cancel_reason if status == ExecutionStatus.CANCELLED else None,
            "actual_first_start_at": _moment(extra[0][0]), "actual_final_end_at": _moment(extra[-1][1]) if status in (
                ExecutionStatus.COMPLETED, ExecutionStatus.SKIPPED, ExecutionStatus.CANCELLED) else None,
            "actual_active_duration_minutes": None, "duration_variance_minutes": None, "start_delay_minutes": None,
            "created_at": now, "updated_at": now, "version": 1, "deleted_at": None,
        })
        self.records.executions.store_synced(copy, extra)
        return copy.id

    def _resolve_series_change(self, account: Account, conflict: Conflict) -> Conflict:
        """accept_remote for series_changed (see the module docstring); keep_local was refused before."""
        key, remote = account.account_key, conflict.remote_record
        resend = False
        with self.store.transaction():
            local = self.records.read("task", conflict.local_id)
            with self.store.applying_remote():
                if remote is not None:
                    series_local = self.records.read("task", self.records.local_id_for("task", remote))
                    if series_local is None or not (self.store.dirty_rev("task", series_local.local_id) is not None
                                                    or self.store.has_ops(key, "task", series_local.wire_id)):
                        # The server's series, unless it has a pending change of its own (its own conflict decides).
                        self.records.store("task", remote, account.user_id,
                                           (series_local.model.version + 1) if series_local else 1)
                        self.store.put_shadow(key, "task", remote)
                if local is not None and not local.deleted:
                    resend = self._follow_series(account, local)
            self.store.delete_entity_ops(key, "task", conflict.entity_id)
            if resend:
                self.store.ensure_dirty("task", conflict.local_id)
            else:
                self.store.clear_dirty("task", conflict.local_id)
            self.store.resolve_conflict(conflict.id, {
                "choice": "accept_remote", "decided_at": self._clock().isoformat(),
                "base_version": conflict.base_version, "remote_version": remote.get("version") if remote else None,
                "occurrence": "follows_series" if resend else "retired",
            })
        return self.store.conflict(conflict.id)

    def _follow_series(self, account: Account, local: LocalRecord) -> bool:
        """
        Re-derive an occurrence created here from its series as stored now:
        True when it follows the series again (to be sent anew); False when it
        was retired here -- its date is no longer one of the series' or the
        series is gone -- together with its never-synchronized placements that
        have no recorded work (anything with work stays, visible).
        """
        occurrence = local.model
        series = self.records.planning.get_task(occurrence.series_id, include_deleted=True)
        now = self._clock()
        valid = False
        if series is not None and series.deleted_at is None and series.recurrence is not None:
            try:
                valid = SeriesRule.of(series.recurrence).is_slot(occurrence.occurrence_slot)
            except RecurrenceError:
                valid = False
        if valid:
            update = {"updated_at": now, "version": occurrence.version + 1, "series_version": series.version}
            if occurrence.occurrence_state is None:  # an exception (modified) keeps its own content
                update.update({name: getattr(series, name) for name in SERIES_CONTENT_FIELDS})
            self.records.planning.store_synced("task", occurrence.model_copy(update=update))
            return True
        self.records.planning.store_synced("task", occurrence.model_copy(update={
            "deleted_at": now, "updated_at": now, "version": occurrence.version + 1,
            "occurrence_state": OccurrenceState.SUPERSEDED,
        }))
        key = account.account_key
        for placement in self.records.planning.active_placements_for_tasks([occurrence.id]).get(occurrence.id, []):
            if self.store.shadow(key, "placement", str(placement.id)) is not None:
                continue
            execution = self.records.executions.find_by_scheduled_task_id(str(placement.id))
            if execution is not None and self.records.executions.list_sessions(execution.id):
                continue
            self._discard(account, self.records.read("placement", str(placement.id)))
            self.store.clear_dirty("placement", str(placement.id))
            self.store.delete_entity_ops(key, "placement", str(placement.id))
        return False

    def _accept_remote(self, account: Account, conflict: Conflict, remote: dict | None, collision: bool) -> None:
        local = self.records.read(conflict.entity_type, conflict.local_id)
        if remote is None or collision:
            # The server does not have this local record (or another record owns its scope): discard it here.
            shadow = self.store.shadow(account.account_key, conflict.entity_type, conflict.entity_id)
            if shadow is not None and not collision:
                self.records.store(conflict.entity_type, shadow.record, account.user_id,
                                   (local.model.version + 1) if local else 1)
            elif local is not None and not local.deleted:
                self._discard(account, local)
        if remote is not None:
            target = self.records.read(conflict.entity_type, self.records.local_id_for(conflict.entity_type, remote))
            if not (target is None and remote.get("deleted_at") is not None):
                self.records.store(conflict.entity_type, remote, account.user_id,
                                   (target.model.version + 1) if target else 1)
            self.store.put_shadow(account.account_key, conflict.entity_type, remote)

    def _undo_local_move(self, account: Account, moved: LocalRecord) -> None:
        """
        The server state won over this device's reschedule of `moved`: discard
        its replacement if the server never had it, and give back the
        execution the move cancelled -- its last acknowledged server state,
        or, never synchronized, `scheduled` again (the move only ever
        cancels a never-started execution). Capture is suppressed.
        """
        key = account.account_key
        replacement = self.records.read("placement", str(moved.model.superseded_by_id))
        if replacement is not None and self.store.shadow(key, "placement", replacement.wire_id) is None:
            if not replacement.deleted:
                self._discard(account, replacement)
            self.store.clear_dirty("placement", replacement.local_id)
        execution = self.records.executions.find_by_scheduled_task_id(moved.local_id)
        if execution is None or execution.status != ExecutionStatus.CANCELLED:
            return
        local = self.records.read("execution", execution.id)
        shadow = self.store.shadow(key, "execution", local.wire_id)
        if shadow is not None:
            self.records.store("execution", shadow.record, account.user_id, execution.version + 1)
            self.store.clear_dirty("execution", execution.id)
        else:
            restored = execution.model_copy(update={
                "status": ExecutionStatus.SCHEDULED, "actual_final_end_at": None, "version": execution.version + 1,
            })
            self.records.executions.store_synced(restored, local.sessions or [])

    def _discard(self, account: Account, local: LocalRecord) -> None:
        """Tombstone a local record the server never accepted (no push follows: capture is suppressed)."""
        now = self._clock()
        if local.entity_type == "execution":
            execution = local.model.model_copy(update={"deleted_at": now.isoformat(), "version": local.model.version + 1})
            self.records.executions.store_synced(execution, local.sessions or [])
            return
        model = local.model.model_copy(update={"deleted_at": now, "updated_at": now, "version": local.model.version + 1})
        self.records.planning.store_synced(local.entity_type, model)
