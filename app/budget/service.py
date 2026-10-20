"""
app/budget/service.py

BudgetService: the budgeting rules over any BudgetRepository (local SQLite,
app/budget/repository.py, or direct PostgreSQL, app/persistence/budget.py).
Every value is validated here before it is stored: exact amounts
(app/budget/money.py), dates within MIN/MAX_BUDGET_DATE, a category of the
record's kind, a known recurrence, an end date only for a recurring entry
and not before its start, and a project link only to a live project of the
same workspace. The repository's owner scope decides the workspace.

Series and occurrences:
    - Editing an entry edits the whole series: every occurrence follows it,
      except the fields an occurrence edit replaced.
    - edit_occurrence / skip_occurrence store one override per scheduled date
      (repeated edits of one occurrence update the same override);
      restore_occurrence removes it, so the occurrence follows the series
      again. Only a recurring entry has occurrence edits (a one-time entry is
      edited directly).
    - A series edit that changes its dates (start, recurrence, end) removes
      the occurrence edits whose scheduled date is no longer one of the
      series' dates; a kind change resets their categories to the series'.
      Deleting an entry deletes its occurrence edits.

Range queries expand recurring entries on the fly (nothing is stored per
future occurrence) and apply each override exactly once: a skipped
occurrence is absent, a modified one appears once with its replacement
values at its (possibly moved) date -- including when it was moved into the
range from outside it, and never at its scheduled date when moved out.
A range spans at most MAX_RANGE_DAYS.

Future items (Budgeting > Future) are not transactions: income ideas and
wishlist items never appear in occurrences(), totals(), statistics or the
forecast. convert_wish turns an open wishlist item into exactly one dated
one-time expense and records it on the item, in one unit of work; the
expense's id is derived from the item (wish_expense_id), and a converted item
refuses another conversion and further edits (delete it, or edit its expense).

The balance: every manual balance change is its own record (set_balance
never overwrites an earlier one), so the history can show each change. The
live record with the latest as_of (then the latest entered) is the current
one. It is the balance at the end of its as_of date, so
projected_balance(day) adds the net of the occurrences after as_of up to and
including `day`.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from pydantic import ValidationError

from app.budget.errors import BudgetConflictError, BudgetNotFoundError, BudgetValidationError
from app.budget.models import (
    CATEGORIES,
    IncomeIdea,
    WishlistItem,
    wish_expense_id,
    OVERRIDE_FIELDS,
    BudgetBalance,
    BudgetEntry,
    BudgetKind,
    BudgetOccurrence,
    BudgetOverride,
    BudgetOverrideState,
    BudgetRecurrence,
    BudgetTotals,
    category_problem,
)
from app.budget.money import parse_amount
from app.budget.recurrence import is_occurrence_date, occurrence_dates
from app.budget.repository import BALANCE, ENTRY, IDEA, OVERRIDE, WISH

#: The longest range a query may expand (about ten years).
MAX_RANGE_DAYS = 3660
_SCHEDULE_FIELDS = ("start_date", "recurrence", "end_date")
_ENTRY_CHANGES = ("kind", "name", "amount", "category", "start_date", "recurrence", "end_date", "project_id")
_OCCURRENCE_CHANGES = ("name", "amount", "date", "category", "project_id")
_KIND_ORDER = {BudgetKind.INCOME: 0, BudgetKind.EXPENSE: 1}
_IDEA_CHANGES = ("name", "expected_amount", "secured_amount", "description")
_WISH_CHANGES = ("name", "amount", "category", "project_id", "description")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _message(error: ValidationError) -> str:
    first = error.errors()[0] if error.errors() else {}
    return str(first.get("msg", error)).removeprefix("Value error, ")


def _build(model: type, data: dict):
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise BudgetValidationError(_message(error)) from None


def _as_date(value: object, name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, (date, str)):
        raise BudgetValidationError(f"{name} must be a calendar date.")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise BudgetValidationError(f"{name} must be a date like 2026-10-19.") from None
    return value


def _as_enum(enum: type, value: object, name: str):
    try:
        return enum(value)
    except ValueError:
        raise BudgetValidationError(
            f"{name} must be one of {', '.join(item.value for item in enum)}.") from None


def _as_uuid(value: object, name: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise BudgetValidationError(f"{name} is not an id.") from None


class BudgetService:
    def __init__(self, repository, clock: Callable[[], datetime] = _utcnow) -> None:
        self.repository = repository
        self._clock = clock

    @property
    def owner(self):
        return self.repository.owner

    def scoped(self, owner) -> "BudgetService":
        return BudgetService(self.repository.scoped(owner), self._clock)

    def _owner_id(self) -> uuid.UUID | None:
        owner = self.repository.owner
        return owner.user_id if owner is not None else None

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    def current_balance(self) -> BudgetBalance | None:
        balances = self.repository.list(BALANCE)
        return max(balances, key=lambda item: (item.as_of, item.created_at, str(item.id)), default=None)

    def set_balance(self, amount: object, as_of: object) -> BudgetBalance:
        """
        Record a manual balance change: the balance is `amount` (may be zero or
        negative) at the end of `as_of`. A new record each time, so earlier
        changes stay in the history.
        """
        amount_minor = parse_amount(amount, allow_negative=True, allow_zero=True)
        as_of = _as_date(as_of, "as_of")
        now = self._clock()
        return self.repository.insert(BALANCE, _build(BudgetBalance, {
            "user_id": self._owner_id(), "amount_minor": amount_minor, "as_of": as_of,
            "created_at": now, "updated_at": now}))

    def list_balances(self) -> list[BudgetBalance]:
        """Every manual balance change, oldest first (by as_of, then when it was entered)."""
        return sorted(self.repository.list(BALANCE), key=lambda item: (item.as_of, item.created_at, str(item.id)))

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def get_entry(self, entry_id: uuid.UUID) -> BudgetEntry:
        entry = self.repository.get(ENTRY, _as_uuid(entry_id, "entry_id"))
        if entry is None:
            raise BudgetNotFoundError(f"No budget entry {entry_id} in this workspace.")
        return entry

    def list_entries(self, kind: object | None = None) -> list[BudgetEntry]:
        entries = self.repository.list(ENTRY)
        if kind is not None:
            kind = _as_enum(BudgetKind, kind, "kind")
            entries = [entry for entry in entries if entry.kind is kind]
        return entries

    def create_entry(self, *, kind: object, name: str, amount: object, category: str, start_date: object,
                     recurrence: object = BudgetRecurrence.ONE_TIME, end_date: object | None = None,
                     project_id: object | None = None) -> BudgetEntry:
        now = self._clock()
        data = {
            "user_id": self._owner_id(), "kind": _as_enum(BudgetKind, kind, "kind"), "name": name,
            "amount_minor": parse_amount(amount), "category": category,
            "start_date": _as_date(start_date, "start_date"),
            "recurrence": _as_enum(BudgetRecurrence, recurrence, "recurrence"),
            "end_date": _as_date(end_date, "end_date") if end_date is not None else None,
            "project_id": _as_uuid(project_id, "project_id"), "created_at": now, "updated_at": now,
        }
        entry = _build(BudgetEntry, data)
        with self.repository.transaction():
            self._check_project(entry.project_id)
            return self.repository.insert(ENTRY, entry)

    def update_entry(self, entry_id: uuid.UUID, *, expected_version: int | None = None, **changes) -> BudgetEntry:
        """
        Edit the whole series. `changes` uses the create_entry names (kind,
        name, amount, category, start_date, recurrence, end_date, project_id);
        end_date=None / project_id=None clear them.
        """
        unknown = set(changes) - set(_ENTRY_CHANGES)
        if unknown:
            raise BudgetValidationError(f"Unknown entry field(s): {', '.join(sorted(unknown))}.")
        with self.repository.transaction():
            current = self._current(ENTRY, entry_id, expected_version)
            data = current.model_dump()
            for name, value in changes.items():
                if name == "amount":
                    data["amount_minor"] = parse_amount(value)
                elif name == "kind":
                    data["kind"] = _as_enum(BudgetKind, value, "kind")
                elif name == "recurrence":
                    data["recurrence"] = _as_enum(BudgetRecurrence, value, "recurrence")
                elif name in ("start_date", "end_date"):
                    data[name] = _as_date(value, name) if value is not None else None
                elif name == "project_id":
                    data[name] = _as_uuid(value, name)
                else:
                    data[name] = value
            if data["recurrence"] in (BudgetRecurrence.ONE_TIME, BudgetRecurrence.ONE_TIME.value) \
                    and "end_date" not in changes:
                data["end_date"] = None  # becoming one-time ends the series where it starts
            now = self._clock()
            entry = _build(BudgetEntry, {**data, "updated_at": now, "version": current.version + 1})
            if entry.model_dump(exclude={"updated_at", "version"}) == current.model_dump(
                    exclude={"updated_at", "version"}):
                return current
            if entry.project_id != current.project_id:
                self._check_project(entry.project_id)
            stored = self.repository.update(ENTRY, entry, expected_version=current.version)
            self._follow_series(current, stored, now)
            return stored

    def delete_entry(self, entry_id: uuid.UUID, *, expected_version: int | None = None) -> None:
        """Delete the entry (all its occurrences) and its occurrence edits."""
        with self.repository.transaction():
            current = self._current(ENTRY, entry_id, expected_version)
            now = self._clock()
            for override in self._overrides_of(current.id):
                self.repository.soft_delete(OVERRIDE, override.id, deleted_at=now, expected_version=override.version)
            self.repository.soft_delete(ENTRY, current.id, deleted_at=now, expected_version=current.version)

    # ------------------------------------------------------------------
    # Occurrences
    # ------------------------------------------------------------------

    def list_overrides(self, entry_id: uuid.UUID) -> list[BudgetOverride]:
        return self._overrides_of(self.get_entry(entry_id).id)

    def edit_occurrence(self, entry_id: uuid.UUID, occurrence_date: object, **changes) -> BudgetOverride | None:
        """
        Change one occurrence (identified by its scheduled date): name,
        amount, date (move it), category, project_id. A field passed as None
        follows the series again; fields not passed keep any earlier edit.
        An edit that leaves nothing replaced is removed (None is returned):
        the occurrence follows the series.
        """
        unknown = set(changes) - set(_OCCURRENCE_CHANGES)
        if unknown:
            raise BudgetValidationError(f"Unknown occurrence field(s): {', '.join(sorted(unknown))}.")
        with self.repository.transaction():
            entry, slot, existing = self._occurrence(entry_id, occurrence_date)
            values = ({name: getattr(existing, name) for name in OVERRIDE_FIELDS}
                      if existing is not None and existing.state is BudgetOverrideState.MODIFIED
                      else dict.fromkeys(OVERRIDE_FIELDS))
            for name, value in changes.items():
                if value is None:
                    values["amount_minor" if name == "amount" else name] = None
                elif name == "amount":
                    values["amount_minor"] = parse_amount(value)
                elif name == "date":
                    values["date"] = _as_date(value, "date")
                elif name == "project_id":
                    values["project_id"] = _as_uuid(value, name)
                else:
                    values[name] = value
            if values["category"] is not None and (problem := category_problem(entry.kind, values["category"])):
                raise BudgetValidationError(problem)
            if values["date"] == slot:
                values["date"] = None
            if values["project_id"] is not None and (existing is None or existing.project_id != values["project_id"]):
                self._check_project(values["project_id"])
            if all(value is None for value in values.values()):
                if existing is not None:
                    self.repository.soft_delete(OVERRIDE, existing.id, deleted_at=self._clock(),
                                                expected_version=existing.version)
                return None
            return self._save_override(entry, slot, existing, BudgetOverrideState.MODIFIED, values)

    def skip_occurrence(self, entry_id: uuid.UUID, occurrence_date: object) -> BudgetOverride:
        """The occurrence on this scheduled date does not happen (restore_occurrence undoes it)."""
        with self.repository.transaction():
            entry, slot, existing = self._occurrence(entry_id, occurrence_date)
            return self._save_override(entry, slot, existing, BudgetOverrideState.SKIPPED,
                                       dict.fromkeys(OVERRIDE_FIELDS))

    def restore_occurrence(self, entry_id: uuid.UUID, occurrence_date: object) -> bool:
        """Remove the occurrence's edit, so it follows the series again. False when it had none."""
        with self.repository.transaction():
            _, _, existing = self._occurrence(entry_id, occurrence_date)
            if existing is None:
                return False
            self.repository.soft_delete(OVERRIDE, existing.id, deleted_at=self._clock(),
                                        expected_version=existing.version)
            return True

    # ------------------------------------------------------------------
    # Future: income ideas and wishlist items (never transactions)
    # ------------------------------------------------------------------

    def list_ideas(self) -> list[IncomeIdea]:
        return sorted(self.repository.list(IDEA), key=lambda item: (item.name.casefold(), str(item.id)))

    def create_idea(self, *, name: str, expected_amount: object, secured_amount: object = 0,
                    description: str | None = None) -> IncomeIdea:
        now = self._clock()
        idea = _build(IncomeIdea, {
            "user_id": self._owner_id(), "name": name, "expected_amount_minor": parse_amount(expected_amount),
            "secured_amount_minor": parse_amount(secured_amount, allow_zero=True), "description": description,
            "created_at": now, "updated_at": now})
        return self.repository.insert(IDEA, idea)

    def update_idea(self, idea_id: uuid.UUID, *, expected_version: int | None = None, **changes) -> IncomeIdea:
        """Change name, expected_amount, secured_amount (received or secured so far) or description."""
        unknown = set(changes) - set(_IDEA_CHANGES)
        if unknown:
            raise BudgetValidationError(f"Unknown idea field(s): {', '.join(sorted(unknown))}.")
        with self.repository.transaction():
            current = self._current(IDEA, idea_id, expected_version)
            data = current.model_dump()
            for name, value in changes.items():
                if name == "expected_amount":
                    data["expected_amount_minor"] = parse_amount(value)
                elif name == "secured_amount":
                    data["secured_amount_minor"] = parse_amount(value, allow_zero=True)
                else:
                    data[name] = value
            idea = _build(IncomeIdea, {**data, "updated_at": self._clock(), "version": current.version + 1})
            if idea.model_dump(exclude={"updated_at", "version"}) == current.model_dump(
                    exclude={"updated_at", "version"}):
                return current
            return self.repository.update(IDEA, idea, expected_version=current.version)

    def delete_idea(self, idea_id: uuid.UUID, *, expected_version: int | None = None) -> None:
        with self.repository.transaction():
            current = self._current(IDEA, idea_id, expected_version)
            self.repository.soft_delete(IDEA, current.id, deleted_at=self._clock(), expected_version=current.version)

    def list_wishes(self, *, include_converted: bool = True) -> list[WishlistItem]:
        wishes = sorted(self.repository.list(WISH), key=lambda item: (item.name.casefold(), str(item.id)))
        return wishes if include_converted else [item for item in wishes if not item.converted]

    def create_wish(self, *, name: str, amount: object, category: str, project_id: object | None = None,
                    description: str | None = None) -> WishlistItem:
        now = self._clock()
        wish = _build(WishlistItem, {
            "user_id": self._owner_id(), "name": name, "amount_minor": parse_amount(amount), "category": category,
            "project_id": _as_uuid(project_id, "project_id"), "description": description,
            "created_at": now, "updated_at": now})
        with self.repository.transaction():
            self._check_project(wish.project_id)
            return self.repository.insert(WISH, wish)

    def update_wish(self, wish_id: uuid.UUID, *, expected_version: int | None = None, **changes) -> WishlistItem:
        """Change name, amount, category, project_id or description of an item not yet converted."""
        unknown = set(changes) - set(_WISH_CHANGES)
        if unknown:
            raise BudgetValidationError(f"Unknown wishlist field(s): {', '.join(sorted(unknown))}.")
        with self.repository.transaction():
            current = self._current(WISH, wish_id, expected_version)
            if current.converted:
                raise BudgetConflictError("This wishlist item was already turned into an expense; edit the expense.")
            data = current.model_dump()
            for name, value in changes.items():
                if name == "amount":
                    data["amount_minor"] = parse_amount(value)
                elif name == "project_id":
                    data["project_id"] = _as_uuid(value, name)
                else:
                    data[name] = value
            wish = _build(WishlistItem, {**data, "updated_at": self._clock(), "version": current.version + 1})
            if wish.model_dump(exclude={"updated_at", "version"}) == current.model_dump(
                    exclude={"updated_at", "version"}):
                return current
            if wish.project_id != current.project_id:
                self._check_project(wish.project_id)
            return self.repository.update(WISH, wish, expected_version=current.version)

    def delete_wish(self, wish_id: uuid.UUID, *, expected_version: int | None = None) -> None:
        """Delete the item (an expense it was turned into stays: it is an ordinary expense now)."""
        with self.repository.transaction():
            current = self._current(WISH, wish_id, expected_version)
            self.repository.soft_delete(WISH, current.id, deleted_at=self._clock(), expected_version=current.version)

    def convert_wish(self, wish_id: uuid.UUID, on_date: object, *,
                     expected_version: int | None = None) -> tuple[WishlistItem, BudgetEntry]:
        """
        Turn an open wishlist item into one one-time expense dated `on_date`
        (its name, amount, category and project) and mark the item converted --
        both or neither. A converted item is refused (BudgetConflictError).
        """
        on_date = _as_date(on_date, "date")
        with self.repository.transaction():
            wish = self._current(WISH, wish_id, expected_version)
            if wish.converted:
                raise BudgetConflictError(f"\u201c{wish.name}\u201d was already turned into an expense on "
                                          f"{wish.converted_on.isoformat()}.")
            self._check_project(wish.project_id)
            now = self._clock()
            entry = self.repository.insert(ENTRY, _build(BudgetEntry, {
                "id": wish_expense_id(wish.id), "user_id": wish.user_id, "kind": BudgetKind.EXPENSE,
                "name": wish.name, "amount_minor": wish.amount_minor, "category": wish.category,
                "start_date": on_date, "recurrence": BudgetRecurrence.ONE_TIME, "project_id": wish.project_id,
                "created_at": now, "updated_at": now}))
            converted = self.repository.update(WISH, wish.model_copy(update={
                "converted_entry_id": entry.id, "converted_on": on_date, "updated_at": now,
                "version": wish.version + 1}), expected_version=wish.version)
            return converted, entry

    # ------------------------------------------------------------------
    # Range queries
    # ------------------------------------------------------------------

    def occurrences(self, start: object, end: object, *, kind: object | None = None,
                    project_id: object | None = None) -> list[BudgetOccurrence]:
        """Every occurrence whose (effective) date lies in [start, end], ordered by date."""
        start, end = self._range(start, end)
        kind = _as_enum(BudgetKind, kind, "kind") if kind is not None else None
        project_id = _as_uuid(project_id, "project_id")
        edits: dict[uuid.UUID, dict[date, BudgetOverride]] = defaultdict(dict)
        for override in self.repository.list(OVERRIDE):
            known = edits[override.entry_id].get(override.occurrence_date)
            if known is None or (override.updated_at, str(override.id)) > (known.updated_at, str(known.id)):
                edits[override.entry_id][override.occurrence_date] = override
        found: list[BudgetOccurrence] = []
        for entry in self.repository.list(ENTRY):
            if kind is not None and entry.kind is not kind:
                continue
            entry_edits = edits.get(entry.id, {}) if entry.is_recurring else {}
            for slot in occurrence_dates(entry, start, end):
                override = entry_edits.get(slot)
                if override is None:
                    found.append(_expanded(entry, slot, None))
                elif override.state is BudgetOverrideState.MODIFIED:
                    occurrence = _expanded(entry, slot, override)
                    if start <= occurrence.date <= end:
                        found.append(occurrence)
            for slot, override in entry_edits.items():  # moved into the range from a date outside it
                if (override.state is BudgetOverrideState.MODIFIED and override.date is not None
                        and start <= override.date <= end and not start <= slot <= end
                        and is_occurrence_date(entry, slot)):
                    found.append(_expanded(entry, slot, override))
        if project_id is not None:
            found = [item for item in found if item.project_id == project_id]
        return sorted(found, key=lambda item: (item.date, _KIND_ORDER[item.kind], item.name.lower(),
                                               str(item.entry_id), item.occurrence_date))

    def totals(self, start: object, end: object, **filters) -> BudgetTotals:
        start, end = self._range(start, end)
        occurrences = self.occurrences(start, end, **filters)
        return BudgetTotals(
            start_date=start, end_date=end, occurrence_count=len(occurrences),
            income_minor=sum(item.amount_minor for item in occurrences if item.kind is BudgetKind.INCOME),
            expense_minor=sum(item.amount_minor for item in occurrences if item.kind is BudgetKind.EXPENSE),
        )

    def projected_balance(self, on_date: object) -> int | None:
        """The current balance plus the net of the occurrences after its as_of date up to `on_date`; None
        without a balance."""
        on_date = _as_date(on_date, "on_date")
        balance = self.current_balance()
        if balance is None:
            return None
        if on_date < balance.as_of:
            raise BudgetValidationError(f"A projection starts at the balance's date ({balance.as_of}).")
        if on_date == balance.as_of:
            return balance.amount_minor
        return balance.amount_minor + self.totals(balance.as_of + timedelta(days=1), on_date).net_minor

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _range(start: object, end: object) -> tuple[date, date]:
        start, end = _as_date(start, "start"), _as_date(end, "end")
        if end < start:
            raise BudgetValidationError("The range ends before it starts.")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise BudgetValidationError(f"A range spans at most {MAX_RANGE_DAYS} days.")
        return start, end

    def _check_project(self, project_id: uuid.UUID | None) -> None:
        if project_id is not None and not self.repository.project_exists(project_id):
            raise BudgetValidationError("project_id does not name a project of this workspace.")

    def _current(self, entity_type: str, record_id: object, expected_version: int | None):
        record = self.repository.get(entity_type, _as_uuid(record_id, "id"))
        if record is None:
            raise BudgetNotFoundError(f"No such budget record {record_id} in this workspace.")
        if expected_version is not None and record.version != expected_version:
            raise BudgetConflictError("The budget record changed since it was read; reload it and try again.")
        return record

    def _overrides_of(self, entry_id: uuid.UUID) -> list[BudgetOverride]:
        return sorted((item for item in self.repository.list(OVERRIDE) if item.entry_id == entry_id),
                      key=lambda item: item.occurrence_date)

    def _occurrence(self, entry_id: object, occurrence_date: object):
        entry = self.get_entry(entry_id)
        slot = _as_date(occurrence_date, "occurrence_date")
        if not entry.is_recurring:
            raise BudgetValidationError("A one-time entry has a single occurrence: edit the entry itself.")
        if not is_occurrence_date(entry, slot):
            raise BudgetValidationError(f"{slot} is not a scheduled date of this entry.")
        existing = next((item for item in self._overrides_of(entry.id) if item.occurrence_date == slot), None)
        return entry, slot, existing

    def _save_override(self, entry: BudgetEntry, slot: date, existing: BudgetOverride | None,
                       state: BudgetOverrideState, values: dict) -> BudgetOverride:
        now = self._clock()
        data = {"entry_id": entry.id, "occurrence_date": slot, "state": state, **values, "updated_at": now}
        if existing is None:
            return self.repository.insert(OVERRIDE, _build(BudgetOverride, {
                **data, "user_id": entry.user_id, "created_at": now}))
        changed = _build(BudgetOverride, {**existing.model_dump(), **data, "version": existing.version + 1})
        if changed.model_dump(exclude={"updated_at", "version"}) == existing.model_dump(
                exclude={"updated_at", "version"}):
            return existing
        return self.repository.update(OVERRIDE, changed, expected_version=existing.version)

    def _follow_series(self, before: BudgetEntry, after: BudgetEntry, now: datetime) -> None:
        """After a series edit: drop edits of dates the series no longer has; reset categories of another kind."""
        rescheduled = any(getattr(before, name) != getattr(after, name) for name in _SCHEDULE_FIELDS)
        if not rescheduled and before.kind is after.kind:
            return
        for override in self._overrides_of(after.id):
            if rescheduled and not (after.is_recurring and is_occurrence_date(after, override.occurrence_date)):
                self.repository.soft_delete(OVERRIDE, override.id, deleted_at=now, expected_version=override.version)
            elif override.category is not None and override.category not in CATEGORIES[after.kind]:
                self.repository.update(OVERRIDE, override.model_copy(update={
                    "category": None, "updated_at": now, "version": override.version + 1}),
                    expected_version=override.version)


def _expanded(entry: BudgetEntry, slot: date, override: BudgetOverride | None) -> BudgetOccurrence:
    def value(name: str):
        replaced = getattr(override, name) if override is not None else None
        return replaced if replaced is not None else getattr(entry, name)

    return BudgetOccurrence(
        entry_id=entry.id, occurrence_date=slot,
        date=override.date if override is not None and override.date is not None else slot,
        kind=entry.kind, name=value("name"), amount_minor=value("amount_minor"), category=value("category"),
        project_id=value("project_id"), recurrence=entry.recurrence,
        override_id=override.id if override is not None else None,
    )
