"""
app/planning/scope.py

Request-scoped ownership for local planning and execution data (Milestone 4
preflight).

The local SQLite database can hold records of several owners at once:
ownerless records created offline (user_id NULL) and records owned by each
account that has signed in on this device (docs/sync-protocol.md). The
desktop app reads and writes *device-wide* -- every owner at once -- and
keeps doing so: a repository or service constructed without a scope is the
legacy device-wide one.

An OwnerScope restricts a repository/service to exactly one owner:

    OwnerScope.account(user_id)   records owned by that account only
    OwnerScope.ownerless()        ownerless local records only

Ownerless data is its own scope. It is never visible through, claimed by,
or written from an account scope (claiming stays the explicit
SyncService.associate_local_data step), and an account's records are never
visible through the ownerless scope. A scoped repository filters every read
by owner, refuses to write a record of another owner, and treats a record
of another owner exactly like a record that does not exist. New web
request handling must always construct a scoped service
(PlanningService.scoped / ExecutionService.scoped); the device-wide default
exists only for the legacy desktop and CLI callers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class OwnerScope:
    #: The owner every read and write is restricted to; None is the ownerless local scope.
    user_id: uuid.UUID | None

    @classmethod
    def account(cls, user_id: uuid.UUID) -> "OwnerScope":
        if user_id is None:
            raise ValueError("an account scope needs a user id; use OwnerScope.ownerless() for ownerless data")
        return cls(uuid.UUID(str(user_id)))

    @classmethod
    def ownerless(cls) -> "OwnerScope":
        return cls(None)

    @property
    def is_ownerless(self) -> bool:
        return self.user_id is None

    @property
    def sql_value(self) -> str | None:
        """The value the `user_id` column holds for this scope (compare with `IS ?`, which matches NULL too)."""
        return str(self.user_id) if self.user_id is not None else None

    def admits(self, owner: uuid.UUID | None) -> bool:
        return owner == self.user_id

    def describe(self) -> str:
        return "the ownerless local workspace" if self.user_id is None else f"account {self.user_id}"
