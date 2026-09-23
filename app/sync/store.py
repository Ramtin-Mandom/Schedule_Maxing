"""
app/sync/store.py

SQL for the synchronization tables of schema v5 (app/execution/db.py):
sync_accounts, sync_dirty, sync_shadows, sync_outbox, sync_conflicts, and
the sync_control flag that suppresses change capture while pulled records
are applied. Business rules live in app/sync/engine.py.

Every method joins the caller's transaction (the connection's shared lock
and savepoints, app.execution.db.transaction), so the engine can make "apply
a pulled page and advance the cursor" or "acknowledge an operation, update
the shadow, clear the dirty mark" single atomic units.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.execution.db import TransactionState, locked, transaction, transaction_state_for


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def account_key(backend_url: str, user_id: str) -> str:
    return f"{backend_url.rstrip('/')}#{user_id}"


@dataclass(frozen=True)
class Account:
    account_key: str
    backend_url: str
    user_id: str
    email: str | None
    pull_cursor: int
    active: bool
    associated_at: str | None


@dataclass(frozen=True)
class Shadow:
    server_version: int
    deleted: bool
    record: dict[str, Any]


@dataclass(frozen=True)
class OutboxOp:
    op_seq: int
    op_id: str
    entity_type: str
    entity_id: str
    local_id: str
    kind: str
    action: str | None
    base_version: int | None
    payload: dict | None
    group_id: str | None
    local_rev: int
    attempts: int

    def wire(self) -> dict:
        return {
            "op_id": self.op_id, "entity_type": self.entity_type, "entity_id": self.entity_id, "kind": self.kind,
            "base_version": self.base_version, "action": self.action, "payload": self.payload, "group": self.group_id,
        }


@dataclass(frozen=True)
class Conflict:
    id: str
    account_key: str
    entity_type: str
    entity_id: str
    local_id: str
    kind: str
    op_id: str | None
    base_version: int | None
    local_record: dict | None
    remote_record: dict | None
    error: dict | None
    status: str
    resolution: dict | None
    created_at: str
    resolved_at: str | None


def _json(value) -> str | None:
    return json.dumps(value, sort_keys=True) if value is not None else None


def _load(value: str | None):
    return json.loads(value) if value is not None else None


class SyncStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._state = transaction_state_for(connection) or TransactionState()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with transaction(self._connection, self._state):
            yield

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with locked(self._connection, self._state):
            yield self._connection

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, params)

    @contextmanager
    def applying_remote(self) -> Iterator[None]:
        """Within the caller's transaction: writes are not captured (no echo) while pulled records are applied."""
        with self.transaction():
            self._execute("UPDATE sync_control SET value = 1 WHERE name = 'applying_remote'")
            try:
                yield
            finally:
                self._execute("UPDATE sync_control SET value = 0 WHERE name = 'applying_remote'")

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def upsert_account(self, backend_url: str, user_id: str, email: str | None) -> Account:
        key = account_key(backend_url, user_id)
        with self.transaction():
            self._execute(
                "INSERT INTO sync_accounts (account_key, backend_url, user_id, email, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(account_key) DO UPDATE SET email = excluded.email",
                (key, backend_url.rstrip("/"), user_id, email, _now()),
            )
        return self.account(key)

    def account(self, key: str) -> Account | None:
        with self._read():
            row = self._execute("SELECT * FROM sync_accounts WHERE account_key = ?", (key,)).fetchone()
        if row is None:
            return None
        return Account(row["account_key"], row["backend_url"], row["user_id"], row["email"], row["pull_cursor"],
                       bool(row["active"]), row["associated_at"])

    def set_active(self, key: str | None) -> None:
        """At most one account is active: records created locally while it is active are owned by it."""
        with self.transaction():
            self._execute("UPDATE sync_accounts SET active = 0 WHERE active = 1")
            if key is not None:
                self._execute("UPDATE sync_accounts SET active = 1 WHERE account_key = ?", (key,))

    def mark_associated(self, key: str) -> None:
        with self.transaction():
            self._execute("UPDATE sync_accounts SET associated_at = ? WHERE account_key = ?", (_now(), key))

    def set_cursor(self, key: str, cursor: int) -> None:
        self._execute("UPDATE sync_accounts SET pull_cursor = ? WHERE account_key = ?", (cursor, key))

    # ------------------------------------------------------------------
    # Dirty marks (written by the v5 capture triggers)
    # ------------------------------------------------------------------

    def dirty(self) -> list[tuple[str, str, int]]:
        with self._read():
            rows = self._execute("SELECT entity_type, entity_id, local_rev FROM sync_dirty ORDER BY changed_at").fetchall()
        return [(row["entity_type"], row["entity_id"], row["local_rev"]) for row in rows]

    def dirty_rev(self, entity_type: str, local_id: str) -> int | None:
        with self._read():
            row = self._execute(
                "SELECT local_rev FROM sync_dirty WHERE entity_type = ? AND entity_id = ?", (entity_type, local_id)
            ).fetchone()
        return row["local_rev"] if row is not None else None

    def clear_dirty(self, entity_type: str, local_id: str, if_rev: int | None = None) -> bool:
        """Remove the mark -- only if no newer local change happened since `if_rev` (when given)."""
        sql, params = "DELETE FROM sync_dirty WHERE entity_type = ? AND entity_id = ?", (entity_type, local_id)
        if if_rev is not None:
            sql, params = sql + " AND local_rev = ?", params + (if_rev,)
        return self._execute(sql, params).rowcount > 0

    def ensure_dirty(self, entity_type: str, local_id: str) -> None:
        self._execute(
            "INSERT INTO sync_dirty (entity_type, entity_id, local_rev, changed_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT (entity_type, entity_id) DO UPDATE SET local_rev = local_rev + 1",
            (entity_type, local_id, _now()),
        )

    # ------------------------------------------------------------------
    # Shadows
    # ------------------------------------------------------------------

    def shadow(self, key: str, entity_type: str, entity_id: str) -> Shadow | None:
        with self._read():
            row = self._execute(
                "SELECT * FROM sync_shadows WHERE account_key = ? AND entity_type = ? AND entity_id = ?",
                (key, entity_type, entity_id),
            ).fetchone()
        return Shadow(row["server_version"], bool(row["deleted"]), json.loads(row["record"])) if row else None

    def any_shadow(self, entity_type: str, entity_id: str) -> bool:
        with self._read():
            return self._execute(
                "SELECT 1 FROM sync_shadows WHERE entity_type = ? AND entity_id = ?", (entity_type, entity_id)
            ).fetchone() is not None

    def shadow_for_legacy_id(self, key: str, local_id: str) -> tuple[str, Shadow] | None:
        """The shadow of a (purged) execution whose local id was a legacy id."""
        with self._read():
            for row in self._execute(
                "SELECT * FROM sync_shadows WHERE account_key = ? AND entity_type = 'execution'", (key,)
            ):
                record = json.loads(row["record"])
                if record.get("legacy_id") == local_id or row["entity_id"] == local_id:
                    return row["entity_id"], Shadow(row["server_version"], bool(row["deleted"]), record)
        return None

    def put_shadow(self, key: str, entity_type: str, record: dict) -> None:
        self._execute(
            "INSERT INTO sync_shadows (account_key, entity_type, entity_id, server_version, deleted, record) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (account_key, entity_type, entity_id) DO UPDATE SET "
            "server_version = excluded.server_version, deleted = excluded.deleted, record = excluded.record",
            (key, entity_type, record["id"], record["version"], int(record.get("deleted_at") is not None),
             json.dumps(record, sort_keys=True)),
        )

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------

    def add_op(self, key: str, entity_type: str, entity_id: str, local_id: str, kind: str, *, action=None,
               base_version=None, payload=None, group_id=None, local_rev: int) -> str:
        op_id = str(uuid.uuid4())
        self._execute(
            "INSERT INTO sync_outbox (op_id, account_key, entity_type, entity_id, local_id, kind, action, base_version, "
            "payload, group_id, local_rev, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (op_id, key, entity_type, entity_id, local_id, kind, action, base_version, _json(payload), group_id,
             local_rev, _now()),
        )
        return op_id

    def pending_ops(self, key: str) -> list[OutboxOp]:
        with self._read():
            rows = self._execute(
                "SELECT * FROM sync_outbox WHERE account_key = ? AND state = 'pending' ORDER BY op_seq", (key,)
            ).fetchall()
        return [
            OutboxOp(row["op_seq"], row["op_id"], row["entity_type"], row["entity_id"], row["local_id"], row["kind"],
                     row["action"], row["base_version"], _load(row["payload"]), row["group_id"], row["local_rev"],
                     row["attempts"])
            for row in rows
        ]

    def has_ops(self, key: str, entity_type: str, entity_id: str) -> bool:
        with self._read():
            return self._execute(
                "SELECT 1 FROM sync_outbox WHERE account_key = ? AND entity_type = ? AND entity_id = ?",
                (key, entity_type, entity_id),
            ).fetchone() is not None

    def delete_op(self, op_id: str) -> None:
        self._execute("DELETE FROM sync_outbox WHERE op_id = ?", (op_id,))

    def delete_entity_ops(self, key: str, entity_type: str, entity_id: str) -> None:
        self._execute("DELETE FROM sync_outbox WHERE account_key = ? AND entity_type = ? AND entity_id = ?",
                      (key, entity_type, entity_id))

    def record_attempt(self, op_ids: list[str], error: str) -> None:
        with self.transaction():
            for op_id in op_ids:
                self._execute("UPDATE sync_outbox SET attempts = attempts + 1, last_error = ? WHERE op_id = ?",
                              (error, op_id))

    def block_ops(self, op_ids: list[str], error: str) -> None:
        with self.transaction():
            for op_id in op_ids:
                self._execute("UPDATE sync_outbox SET state = 'blocked', last_error = ? WHERE op_id = ?", (error, op_id))

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------

    def add_conflict(self, key: str, entity_type: str, entity_id: str, local_id: str, kind: str, *, op_id=None,
                     base_version=None, local_record=None, remote_record=None, error=None) -> str:
        conflict_id = str(uuid.uuid4())
        self._execute(
            "INSERT INTO sync_conflicts (id, account_key, entity_type, entity_id, local_id, kind, op_id, base_version, "
            "local_record, remote_record, error, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (conflict_id, key, entity_type, entity_id, local_id, kind, op_id, base_version, _json(local_record),
             _json(remote_record), _json(error), _now()),
        )
        return conflict_id

    def open_conflict(self, key: str, entity_type: str, entity_id: str) -> Conflict | None:
        with self._read():
            row = self._execute(
                "SELECT * FROM sync_conflicts WHERE account_key = ? AND entity_type = ? AND entity_id = ? "
                "AND status = 'open' ORDER BY created_at LIMIT 1",
                (key, entity_type, entity_id),
            ).fetchone()
        return self._conflict(row) if row else None

    def update_conflict_remote(self, conflict_id: str, remote_record: dict) -> None:
        self._execute("UPDATE sync_conflicts SET remote_record = ? WHERE id = ?", (_json(remote_record), conflict_id))

    def conflicts(self, key: str, status: str | None = "open") -> list[Conflict]:
        with self._read():
            if status is None:
                rows = self._execute("SELECT * FROM sync_conflicts WHERE account_key = ? ORDER BY created_at", (key,))
            else:
                rows = self._execute(
                    "SELECT * FROM sync_conflicts WHERE account_key = ? AND status = ? ORDER BY created_at", (key, status)
                )
            return [self._conflict(row) for row in rows.fetchall()]

    def conflict(self, conflict_id: str) -> Conflict | None:
        with self._read():
            row = self._execute("SELECT * FROM sync_conflicts WHERE id = ?", (conflict_id,)).fetchone()
        return self._conflict(row) if row else None

    def resolve_conflict(self, conflict_id: str, resolution: dict) -> None:
        self._execute(
            "UPDATE sync_conflicts SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ? AND status = 'open'",
            (_json(resolution), _now(), conflict_id),
        )

    @staticmethod
    def _conflict(row) -> Conflict:
        return Conflict(
            row["id"], row["account_key"], row["entity_type"], row["entity_id"], row["local_id"], row["kind"],
            row["op_id"], row["base_version"], _load(row["local_record"]), _load(row["remote_record"]),
            _load(row["error"]), row["status"], _load(row["resolution"]), row["created_at"], row["resolved_at"],
        )
