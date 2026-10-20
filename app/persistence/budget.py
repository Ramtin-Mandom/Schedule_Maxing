"""
app/persistence/budget.py

DirectBudgetRepository: the BudgetRepository interface (app/budget/repository.py)
over the server's PostgreSQL schema, for BudgetService in direct mode. Every
write goes through the backend's one mutation path (backend/mutations.py:
Mutator with the BUDGET_* specs of backend/resources.py) -- the server's
validation, versions and change log, so synchronizing clients of the same
account see direct writes in their feed.

    - A call outside a transaction is its own unit of work (a read is
      rolled back and closed; a write is one committed mutation).
    - transaction() opens one Session and one mutation (the user's
      change-log lock) for the whole block; calls inside it on the same
      thread use them, and a nested transaction() joins the enclosing one.
      Nothing commits before the outermost block ends; any failure rolls it
      all back.

Versions are the server's: a model returned here carries the version the
server stored. The repository is bound to the signed-in account's scope.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import select

from app.budget.errors import BudgetConflictError, BudgetNotFoundError, BudgetValidationError
from app.budget.models import BudgetBalance, BudgetEntry, BudgetOverride, IncomeIdea, WishlistItem
from app.budget.repository import BALANCE, ENTRY, IDEA, OVERRIDE, WISH
from app.persistence import errors
from app.planning.errors import ScopeError
from app.planning.scope import OwnerScope
from backend import models
from backend.errors import ApiError
from backend.mutations import mutation
from backend.resources import BUDGET_BALANCES, BUDGET_ENTRIES, BUDGET_IDEAS, BUDGET_OVERRIDES, BUDGET_WISHES, live

_SPECS = {BALANCE: BUDGET_BALANCES, ENTRY: BUDGET_ENTRIES, OVERRIDE: BUDGET_OVERRIDES, IDEA: BUDGET_IDEAS,
          WISH: BUDGET_WISHES}
_MODELS = {BALANCE: BudgetBalance, ENTRY: BudgetEntry, OVERRIDE: BudgetOverride, IDEA: IncomeIdea,
           WISH: WishlistItem}


def budget_error(error: ApiError) -> Exception:
    """A budget domain error for an ApiError raised by the mutation path (messages are the server's own)."""
    if error.status == 401:
        return errors.NotSignedInError()
    if error.status == 404:
        return BudgetNotFoundError(error.message)
    if error.status == 409:
        return BudgetConflictError(error.message)
    return BudgetValidationError(error.message)


class DirectBudgetRepository:
    def __init__(self, account) -> None:
        self._account = account
        self._local = threading.local()

    @property
    def owner(self) -> OwnerScope:
        return self._account.scope

    def scoped(self, owner: OwnerScope) -> "DirectBudgetRepository":
        if owner != self.owner:
            raise ScopeError("a direct budget repository is bound to the signed-in account and cannot change scope.")
        return self

    def _active(self):
        return getattr(self._local, "mutator", None)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._active() is not None:
            self._account.require_active()
            yield  # joins the enclosing unit of work
            return
        with self._account.operation(budget_error) as session:
            with mutation(session, self._account.user_id, self._account.clock) as mutator:
                self._local.mutator = mutator
                try:
                    yield
                finally:
                    self._local.mutator = None

    def _read(self, read):
        active = self._active()
        if active is not None:
            return read(active.session)
        with self._account.operation(budget_error) as session:
            return read(session)

    def _write(self, write):
        with self.transaction():
            return write(self._active())

    def _model(self, entity_type: str, record: dict):
        return _MODELS[entity_type].model_validate({**record, "user_id": self._account.user_id})

    def _content(self, entity_type: str, model) -> dict:
        fields = set(_SPECS[entity_type].create_schema.model_fields) - {"id"}
        return model.model_dump(mode="json", include=fields)

    # -- the BudgetRepository interface ------------------------------------

    def get(self, entity_type: str, record_id: uuid.UUID, *, include_deleted: bool = False):
        spec = _SPECS[entity_type]

        def read(session):
            row = session.get(spec.model, (self._account.user_id, record_id))
            if row is None or (row.deleted_at is not None and not include_deleted):
                return None
            return self._model(entity_type, spec.serialize(session, self._account.user_id, row))

        return self._read(read)

    def list(self, entity_type: str, *, include_deleted: bool = False) -> list:
        spec = _SPECS[entity_type]

        def read(session):
            query = select(spec.model).where(spec.model.user_id == self._account.user_id)
            if not include_deleted:
                query = query.where(spec.model.deleted_at.is_(None))
            rows = session.scalars(query.order_by(spec.model.created_at, spec.model.id)).all()
            return [self._model(entity_type, spec.serialize(session, self._account.user_id, row)) for row in rows]

        return self._read(read)

    def project_exists(self, project_id: uuid.UUID) -> bool:
        return self._read(lambda session: live(session, models.Project, self._account.user_id, project_id) is not None)

    def insert(self, entity_type: str, model):
        if not self.owner.admits(model.user_id):
            raise ScopeError("A budget record of another owner cannot be written in this account.")
        spec = _SPECS[entity_type]
        payload = spec.create_schema.model_validate({**self._content(entity_type, model), "id": str(model.id)})
        return self._model(entity_type, self._write(lambda mutator: mutator.create(spec, payload)))

    def update(self, entity_type: str, model, *, expected_version: int):
        if not self.owner.admits(model.user_id):
            raise ScopeError("A budget record of another owner cannot be written in this account.")
        spec = _SPECS[entity_type]
        payload = spec.update_schema.model_validate({**self._content(entity_type, model),
                                                     "base_version": expected_version})
        return self._model(entity_type, self._write(lambda mutator: mutator.update(spec, model.id, payload)))

    def soft_delete(self, entity_type: str, record_id: uuid.UUID, *, deleted_at: datetime, expected_version: int):
        spec = _SPECS[entity_type]  # the server's clock stamps the tombstone (deleted_at is the local intent)
        return self._model(entity_type, self._write(lambda mutator: mutator.delete(spec, record_id, expected_version)))
