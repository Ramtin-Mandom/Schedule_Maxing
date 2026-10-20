"""
app/budget/repository.py

BudgetRepository: the budget records in the local SQLite database (tables
budget_balances, budget_entries, budget_overrides; schema v17 in
app/execution/db.py). Parameterized SQL only; no business rules -- those are
BudgetService's (app/budget/service.py).

The interface is the same for every storage (BudgetService only uses
these methods; app/persistence/budget.py implements them over PostgreSQL),
generic over the three entity types of ENTITY_TYPES:

    get(type, id, include_deleted=False)      one record or None
    list(type, include_deleted=False)         every record of the workspace
    insert(type, model) -> stored model
    update(type, model, expected_version) -> stored model   (BudgetConflictError when stale)
    soft_delete(type, id, deleted_at, expected_version) -> stored tombstone
    project_exists(project_id)                a live project of the workspace
    transaction()                             one atomic unit of work

Ownership follows app/planning/scope.py: a scoped repository reads and
writes exactly one owner's records and treats another owner's record as
missing; the device-wide repository (owner None) is only for app/sync.
Deletes are soft (tombstones), and change capture triggers (v17) mark every
write for synchronization in the same transaction.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from enum import Enum

from app.budget.errors import BudgetConflictError, BudgetValidationError
from app.budget.models import BudgetBalance, BudgetEntry, BudgetOverride, IncomeIdea, WishlistItem
from app.execution.db import TransactionState, locked, transaction, transaction_state_for
from app.planning.errors import ScopeError
from app.planning.scope import OwnerScope

BALANCE, ENTRY, OVERRIDE = "budget_balance", "budget_entry", "budget_override"
IDEA, WISH = "budget_idea", "budget_wish"
#: The budget entity types (their wire names), in reference order: an override and a converted wish name an entry.
ENTITY_TYPES = (BALANCE, ENTRY, OVERRIDE, IDEA, WISH)

_META = ("created_at", "updated_at", "version", "deleted_at")
#: entity type -> (table, model, columns)
_TABLES: dict[str, tuple[str, type, tuple[str, ...]]] = {
    BALANCE: ("budget_balances", BudgetBalance, ("id", "user_id", "amount_minor", "as_of", *_META)),
    ENTRY: ("budget_entries", BudgetEntry, (
        "id", "user_id", "kind", "name", "amount_minor", "category", "start_date", "recurrence", "end_date",
        "project_id", *_META)),
    OVERRIDE: ("budget_overrides", BudgetOverride, (
        "id", "user_id", "entry_id", "occurrence_date", "state", "name", "amount_minor", "date", "category",
        "project_id", *_META)),
    IDEA: ("budget_ideas", IncomeIdea, (
        "id", "user_id", "name", "expected_amount_minor", "secured_amount_minor", "description", *_META)),
    WISH: ("budget_wishes", WishlistItem, (
        "id", "user_id", "name", "amount_minor", "category", "project_id", "description", "converted_entry_id",
        "converted_on", *_META)),
}
_LABELS = {BALANCE: "budget balance", ENTRY: "budget entry", OVERRIDE: "occurrence edit", IDEA: "income idea",
           WISH: "wishlist item"}


def _value(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _row(model, columns: tuple[str, ...]) -> tuple:
    return tuple(_value(getattr(model, column)) for column in columns)


class BudgetRepository:
    def __init__(self, connection: sqlite3.Connection, *, owner: OwnerScope | None = None) -> None:
        self._connection = connection
        self._state = transaction_state_for(connection) or TransactionState()
        self._owner = owner

    @property
    def owner(self) -> OwnerScope | None:
        return self._owner

    def scoped(self, owner: OwnerScope) -> "BudgetRepository":
        """A repository on the same connection (and lock/transaction state), restricted to `owner`."""
        scoped = BudgetRepository.__new__(BudgetRepository)
        scoped._connection, scoped._state, scoped._owner = self._connection, self._state, owner
        return scoped

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with transaction(self._connection, self._state):
            yield

    # -- reads -----------------------------------------------------------

    def _owner_sql(self) -> tuple[str, tuple]:
        if self._owner is None:
            return "", ()
        return " AND user_id IS ?", (self._owner.sql_value,)

    def get(self, entity_type: str, record_id: uuid.UUID, *, include_deleted: bool = False):
        table, model, _ = _TABLES[entity_type]
        owner_sql, owner_params = self._owner_sql()
        live = "" if include_deleted else " AND deleted_at IS NULL"
        with locked(self._connection, self._state):
            row = self._connection.execute(
                f"SELECT * FROM {table} WHERE id = ?{live}{owner_sql}", (str(record_id), *owner_params)).fetchone()
        return model.model_validate(dict(row)) if row is not None else None

    def list(self, entity_type: str, *, include_deleted: bool = False) -> list:
        table, model, _ = _TABLES[entity_type]
        owner_sql, owner_params = self._owner_sql()
        live = "1 = 1" if include_deleted else "deleted_at IS NULL"
        with locked(self._connection, self._state):
            rows = self._connection.execute(
                f"SELECT * FROM {table} WHERE {live}{owner_sql} ORDER BY created_at, id", owner_params).fetchall()
        return [model.model_validate(dict(row)) for row in rows]

    def project_exists(self, project_id: uuid.UUID) -> bool:
        owner_sql, owner_params = self._owner_sql()
        with locked(self._connection, self._state):
            row = self._connection.execute(
                f"SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL{owner_sql}",
                (str(project_id), *owner_params)).fetchone()
        return row is not None

    # -- writes ----------------------------------------------------------

    def _require_owner(self, entity_type: str, model) -> None:
        if self._owner is not None and not self._owner.admits(model.user_id):
            raise ScopeError(f"{_LABELS[entity_type]} {model.id} belongs to another owner; it cannot be written in "
                             f"{self._owner.describe()}.")

    def insert(self, entity_type: str, model):
        table, _, columns = _TABLES[entity_type]
        self._require_owner(entity_type, model)
        with self.transaction():
            try:
                self._connection.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    _row(model, columns))
            except sqlite3.IntegrityError as error:
                if f"{table}.id" in str(error):
                    raise BudgetConflictError(f"This {_LABELS[entity_type]} already exists.") from error
                if "budget_overrides.entry_id" in str(error):
                    raise BudgetConflictError("This occurrence already has an edit.") from error
                raise BudgetValidationError(f"The {_LABELS[entity_type]} is not valid: {error}") from error
            stored = self.get(entity_type, model.id, include_deleted=True)
            if stored is None:
                # In the ownerless scope while an account is active, the owner trigger re-stamped the row for
                # that account: the ownerless workspace never creates account records implicitly.
                raise ScopeError(f"The {_LABELS[entity_type]} could not be created as an ownerless record: an "
                                 "account is active on this device, so new records belong to it.")
        return stored

    def update(self, entity_type: str, model, *, expected_version: int):
        table, _, columns = _TABLES[entity_type]
        self._require_owner(entity_type, model)
        assignments = ", ".join(f"{column} = ?" for column in columns if column != "id")
        values = [value for column, value in zip(columns, _row(model, columns)) if column != "id"]
        owner_sql, owner_params = self._owner_sql()
        with self.transaction():
            try:
                cursor = self._connection.execute(
                    f"UPDATE {table} SET {assignments} WHERE id = ? AND version = ? AND deleted_at IS NULL{owner_sql}",
                    (*values, str(model.id), expected_version, *owner_params))
            except sqlite3.IntegrityError as error:
                raise BudgetValidationError(f"The {_LABELS[entity_type]} is not valid: {error}") from error
            if cursor.rowcount != 1:
                raise BudgetConflictError(f"The {_LABELS[entity_type]} changed or was deleted since it was read.")
            return self.get(entity_type, model.id)

    def soft_delete(self, entity_type: str, record_id: uuid.UUID, *, deleted_at: datetime, expected_version: int):
        table, _, _ = _TABLES[entity_type]
        stamp = deleted_at.astimezone(timezone.utc).isoformat()
        owner_sql, owner_params = self._owner_sql()
        with self.transaction():
            cursor = self._connection.execute(
                f"UPDATE {table} SET deleted_at = ?, updated_at = ?, version = version + 1 "
                f"WHERE id = ? AND version = ? AND deleted_at IS NULL{owner_sql}",
                (stamp, stamp, str(record_id), expected_version, *owner_params))
            if cursor.rowcount != 1:
                raise BudgetConflictError(f"The {_LABELS[entity_type]} changed or was deleted since it was read.")
            return self.get(entity_type, record_id, include_deleted=True)

    def store_synced(self, entity_type: str, model) -> None:
        """
        Insert or overwrite one record exactly as given (tombstones included).
        Only app/sync uses this, to apply a record the server already accepted
        (see PlanningRepository.store_synced).
        """
        table, _, columns = _TABLES[entity_type]
        assignments = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "id")
        with self.transaction():
            self._connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(id) DO UPDATE SET {assignments}", _row(model, columns))
