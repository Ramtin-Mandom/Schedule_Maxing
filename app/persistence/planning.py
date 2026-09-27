"""
app/persistence/planning.py

DirectPlanningRepository: the planning repository interface PlanningService
and app/planning/workflow.py use, served by the backend's
ServerPlanningRepository -- but with an operation-scoped Session instead of
a request's.

    - A call outside a transaction runs in its own Session (read: rolled
      back and closed after the call; write: one committed mutation).
    - transaction() opens one Session and one ServerPlanningRepository
      transaction (the user's change-log lock, versions, change log) for
      the whole block; calls inside it -- on the same thread -- use that
      Session, and a nested transaction() is a savepoint. Nothing commits
      before the outermost block ends; any failure rolls it all back.
    - The active Session is thread-local: two background workers never
      share one, and none stays open between user actions.

So PlanningService's long work (the optimizer in workflow.generate) runs
outside any transaction, between the read snapshot and the guarded,
re-checked save in a short write transaction -- as on SQLite.

The repository is bound to its account session's owner (scoped() to any
other owner is a ScopeError) and stops working after sign-out.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from app.planning.errors import ScopeError
from app.planning.scope import OwnerScope
from backend.planning_repository import ServerPlanningRepository


class DirectPlanningRepository:
    def __init__(self, account) -> None:
        self._account = account
        self._local = threading.local()

    @property
    def owner(self) -> OwnerScope:
        return self._account.scope

    def scoped(self, owner: OwnerScope) -> "DirectPlanningRepository":
        if owner != self.owner:
            raise ScopeError("a direct planning repository is bound to the signed-in account and cannot change scope.")
        return self

    def _active(self) -> ServerPlanningRepository | None:
        return getattr(self._local, "repository", None)

    def _repository(self, session) -> ServerPlanningRepository:
        return ServerPlanningRepository(session, self._account.user_id, self._account.clock)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        active = self._active()
        if active is not None:
            with active.transaction():  # a savepoint of the enclosing unit of work
                yield
            return
        with self._account.operation() as session:
            repository = self._repository(session)
            self._local.repository = repository
            try:
                with repository.transaction():
                    yield
            finally:
                self._local.repository = None

    def __getattr__(self, name: str):
        if name.startswith("_") or not callable(getattr(ServerPlanningRepository, name, None)):
            raise AttributeError(name)

        def call(*args, **kwargs):
            active = self._active()
            if active is not None:
                self._account.require_active()
                return getattr(active, name)(*args, **kwargs)
            with self._account.operation() as session:
                return getattr(self._repository(session), name)(*args, **kwargs)

        call.__name__ = name
        return call
