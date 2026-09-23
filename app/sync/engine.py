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
        shadow, deleted/missing    -> delete (base = shadow version)
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
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.execution.db import SYNC_TABLES
from app.execution.repository import ExecutionRepository
from app.planning.repository import PlanningRepository
from app.sync.mapping import ENTITY_ORDER, DivergedHistory, LocalRecord, LocalRecords, execution_changes
from app.sync.store import Account, Conflict, SyncStore
from app.sync.transport import PullPage


class ConflictResolutionError(Exception):
    """The requested resolution is not possible for this conflict."""


@dataclass
class PushOutcome:
    sent: int = 0
    applied: int = 0
    conflicts: int = 0


@dataclass
class PullOutcome:
    applied: int = 0
    skipped: int = 0
    conflicts: list[str] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SyncEngine:
    def __init__(self, connection, clock: Callable[[], datetime] = _utcnow) -> None:
        self.store = SyncStore(connection)
        self.records = LocalRecords(PlanningRepository(connection), ExecutionRepository(connection))
        self._connection = connection
        self._clock = clock

    # ------------------------------------------------------------------
    # Ownerless local data
    # ------------------------------------------------------------------

    def associate_local_data(self, account: Account) -> dict[str, int]:
        """
        The explicit claim step: every ownerless local record becomes the
        account's (one logical mutation each: version + 1), and the account
        becomes active so later local records are owned by it too. Records of
        other accounts are untouched. The claimed records are then pushed by
        the next sync. Nothing calls this implicitly (not even signing in).
        """
        now = self._clock().isoformat()
        counts: dict[str, int] = {}
        with self.store.transaction():
            for entity_type, table in SYNC_TABLES:
                cursor = self._connection.execute(
                    f"UPDATE {table} SET user_id = ?, version = version + 1, updated_at = ? WHERE user_id IS NULL",
                    (account.user_id, now),
                )
                counts[entity_type] = cursor.rowcount
            self.store.mark_associated(account.account_key)
            self.store.set_active(account.account_key)
        return counts

    # ------------------------------------------------------------------
    # Push
    # ------------------------------------------------------------------

    def _waiting(self, key: str, entity_type: str, wire_id: str) -> bool:
        return self.store.has_ops(key, entity_type, wire_id) or self.store.open_conflict(key, entity_type, wire_id) is not None

    def prepare(self, account: Account) -> int:
        """Materialize operations for the account's dirty records (see the module docstring). Returns how many."""
        key = account.account_key
        with self.store.transaction():
            upserts: list[tuple[LocalRecord, object]] = []
            deletes: list[tuple[str, str, str, int, object]] = []  # (type, wire id, local id, rev, shadow)
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
                        deletes.append((entity_type, found[0], local_id, rev, found[1]))
                    continue
                if local.owner != account.user_id or self._waiting(key, entity_type, local.wire_id):
                    continue
                shadow = self.store.shadow(key, entity_type, local.wire_id)
                if local.deleted:
                    if shadow is not None and not shadow.deleted:
                        deletes.append((entity_type, local.wire_id, local_id, rev, shadow))
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

            count = 0
            for local, shadow in self._ordered_upserts(upserts):
                count += self._materialize(key, local, shadow)
            for entity_type, wire_id, local_id, rev, shadow in self._ordered_deletes(deletes):
                self.store.add_op(key, entity_type, wire_id, local_id, "delete", base_version=shadow.server_version,
                                  local_rev=rev)
                count += 1
            return count

    def _ordered_upserts(self, upserts):
        rank = {entity_type: index for index, entity_type in enumerate(ENTITY_ORDER)}
        tasks = {local.wire_id: (local, shadow) for local, shadow in upserts if local.entity_type == "task"}
        ordered_tasks: list = []
        visiting: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting or task_id not in tasks or tasks[task_id] in ordered_tasks:
                return
            visiting.add(task_id)
            for dependency in tasks[task_id][0].payload["dependency_ids"]:
                visit(dependency)
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
        for entity_type, wire_id, _, _, shadow in deletes:
            if entity_type == "task":
                task_deps[wire_id] = set(shadow.record.get("dependency_ids") or [])

        def depth(task_id: str, seen=frozenset()) -> int:
            dependents = [other for other, deps in task_deps.items() if task_id in deps and other not in seen]
            return 1 + max((depth(other, seen | {task_id}) for other in dependents), default=0)

        return sorted(deletes, key=lambda item: (-rank[item[0]], -depth(item[1]) if item[0] == "task" else 0))

    def _materialize(self, key: str, local: LocalRecord, shadow) -> int:
        rev = self.store.dirty_rev(local.entity_type, local.local_id) or 1
        add = self.store.add_op
        if shadow is None:
            payload = dict(local.payload)
            if local.entity_type == "execution":
                payload["historical_reference"] = not self.records.links_resolve(local)
            add(key, local.entity_type, local.wire_id, local.local_id, "create", payload=payload, local_rev=rev)
            return 1
        if local.entity_type != "execution":
            add(key, local.entity_type, local.wire_id, local.local_id, "update", base_version=shadow.server_version,
                payload=local.payload, local_rev=rev)
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
                    self.store.delete_op(op.op_id)
                    outcome.applied += 1
                    if not self.store.has_ops(key, op.entity_type, op.entity_id):
                        # Only if nothing changed locally while the request was in flight.
                        self.store.clear_dirty(op.entity_type, op.local_id, if_rev=op.local_rev)
                else:
                    failed_entities.setdefault((op.entity_type, op.entity_id), []).append((op, result))
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
                outcome.conflicts += 1
        return outcome

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
        pending = self.store.dirty_rev(entity_type, local_id) is not None or self.store.has_ops(key, entity_type, wire_id)
        if local is not None and local.owner not in (None, account.user_id):
            self._pull_conflict(key, entity_type, wire_id, local_id, shadow, local, record, "owned_by_another_account")
            outcome.conflicts.append(wire_id)
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
            self.store.put_shadow(key, entity_type, record)  # never seen here; nothing to delete
            return
        version = (local.model.version + 1) if local is not None else 1
        self.records.store(entity_type, record, account.user_id, version)
        self.store.put_shadow(key, entity_type, record)
        outcome.applied += 1

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
            sql = "SELECT id FROM executions WHERE scheduled_task_id = ? AND id <> ?"
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

    def resolve(self, account: Account, conflict_id: str, choice: str) -> Conflict:
        conflict = self.store.conflict(conflict_id)
        if conflict is None or conflict.account_key != account.account_key:
            raise ConflictResolutionError("No such conflict for this account.")
        if conflict.status != "open":
            raise ConflictResolutionError("The conflict is already resolved.")
        if choice not in ("accept_remote", "keep_local"):
            raise ConflictResolutionError("Choose accept_remote or keep_local.")
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
                with self.store.applying_remote():
                    self._accept_remote(account, conflict, remote, collision)
                self.store.clear_dirty(entity_type, conflict.local_id)
            self.store.delete_entity_ops(key, entity_type, conflict.entity_id)
            self.store.resolve_conflict(conflict_id, {
                "choice": choice, "decided_at": self._clock().isoformat(),
                "base_version": conflict.base_version, "remote_version": remote.get("version") if remote else None,
            })
        return self.store.conflict(conflict_id)

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

    def _discard(self, account: Account, local: LocalRecord) -> None:
        """Tombstone a local record the server never accepted (no push follows: capture is suppressed)."""
        now = self._clock()
        if local.entity_type == "execution":
            execution = local.model.model_copy(update={"deleted_at": now.isoformat(), "version": local.model.version + 1})
            self.records.executions.store_synced(execution, local.sessions or [])
            return
        model = local.model.model_copy(update={"deleted_at": now, "updated_at": now, "version": local.model.version + 1})
        self.records.planning.store_synced(local.entity_type, model)
