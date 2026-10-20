"""
app/budget/models.py

The budget records. Each is owner-scoped and sync-ready like the planning
records (docs/sync-contract.md): a UUID, an owner (user_id; None is the
ownerless local workspace), UTC audit timestamps, a version and a
`deleted_at` tombstone. Amounts are integer minor units (app/budget/money.py).

    BudgetBalance   a balance the user entered, true on its `as_of` date. The
                    current balance is the live one with the latest as_of.
    BudgetEntry     a named income or expense: an amount, a category, a date
                    (the first occurrence), a recurrence and an optional end
                    date, and an optional project link.
    BudgetOverride  an edit of one occurrence of a recurring entry, keyed by
                    the occurrence's scheduled date (`occurrence_date`, its
                    identity, which never changes): "skipped" (it does not
                    happen) or "modified" (each non-null field replaces the
                    entry's value for that occurrence; null fields follow
                    the entry). At most one live override per occurrence.
    IncomeIdea      a possible future income (Future, left): a name, an
                    expected amount, a description, and its progress -- the
                    amount already received or firmly secured towards the
                    expected amount (secured_amount_minor; progress =
                    secured / expected, reached at 100%). Never a transaction:
                    it affects no balance, total, statistic or forecast.
    WishlistItem    something the user would like to buy (Future, right): a
                    name, an amount, an expense category, an optional project
                    and description, and no date -- so it affects nothing
                    until it is turned into an expense. Converting records the
                    created expense (converted_entry_id) and its date
                    (converted_on); a converted item cannot be converted again.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from app.budget.money import MAX_AMOUNT_MINOR

#: `date` under another name, for models that have a field called "date".
Day = date

#: The earliest and latest date a budget record may use.
MIN_BUDGET_DATE = date(1900, 1, 1)
MAX_BUDGET_DATE = date(2999, 12, 31)
MAX_NAME_LENGTH = 200


class BudgetKind(str, Enum):
    INCOME = "income"
    EXPENSE = "expense"


#: The categories of each kind; "Other" is always available.
INCOME_CATEGORIES = ("Work 1", "Work 2", "Work 3", "Other")
EXPENSE_CATEGORIES = (
    "Housing", "Utilities", "Groceries", "Dining", "Transport", "Health", "Insurance", "Debt", "Subscriptions",
    "Entertainment", "Shopping", "Education", "Savings", "Gifts", "Travel", "Other",
)
CATEGORIES = {BudgetKind.INCOME: INCOME_CATEGORIES, BudgetKind.EXPENSE: EXPENSE_CATEGORIES}


class BudgetRecurrence(str, Enum):
    ONE_TIME = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


class BudgetOverrideState(str, Enum):
    MODIFIED = "modified"
    SKIPPED = "skipped"


#: The fields of an occurrence a "modified" override can replace.
OVERRIDE_FIELDS = ("name", "amount_minor", "date", "category", "project_id")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _check_date(value: date | None) -> date | None:
    if value is not None and not MIN_BUDGET_DATE <= value <= MAX_BUDGET_DATE:
        raise ValueError(f"dates must lie between {MIN_BUDGET_DATE} and {MAX_BUDGET_DATE}")
    return value


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("a name cannot be empty")
    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"a name has at most {MAX_NAME_LENGTH} characters")
    return value


def category_problem(kind: BudgetKind, category: str) -> str | None:
    """Why `category` is not one of `kind`'s categories (None when it is)."""
    if category in CATEGORIES[kind]:
        return None
    return f"{category!r} is not an {kind.value} category; use one of {', '.join(CATEGORIES[kind])}"


class _Record(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: uuid.UUID | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = Field(default=1, gt=0)
    deleted_at: datetime | None = None

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _timestamps_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class BudgetBalance(_Record):
    #: The balance in minor units (negative = overdrawn).
    amount_minor: int = Field(ge=-MAX_AMOUNT_MINOR, le=MAX_AMOUNT_MINOR, strict=True)
    as_of: date

    @field_validator("as_of")
    @classmethod
    def _valid_as_of(cls, value: date) -> date:
        return _check_date(value)


class BudgetEntry(_Record):
    kind: BudgetKind
    name: str
    #: Always positive; the kind says whether it adds to or subtracts from the balance.
    amount_minor: int = Field(ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    category: str
    #: The date of a one-time entry, or the first occurrence (the anchor) of a recurring one.
    start_date: date
    recurrence: BudgetRecurrence = BudgetRecurrence.ONE_TIME
    #: The last date a recurring entry may occur on (inclusive); None = no end. Always None for a one-time entry.
    end_date: date | None = None
    project_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("start_date", "end_date")
    @classmethod
    def _valid_dates(cls, value: date | None) -> date | None:
        return _check_date(value)

    @model_validator(mode="after")
    def _consistent(self) -> "BudgetEntry":
        problem = category_problem(self.kind, self.category)
        if problem:
            raise ValueError(problem)
        if self.end_date is not None:
            if self.recurrence is BudgetRecurrence.ONE_TIME:
                raise ValueError("a one-time entry has no end date")
            if self.end_date < self.start_date:
                raise ValueError("the end date cannot be before the start date")
        return self

    @property
    def is_recurring(self) -> bool:
        return self.recurrence is not BudgetRecurrence.ONE_TIME

    def signed(self, amount_minor: int) -> int:
        """`amount_minor` with this entry's sign: positive for income, negative for an expense."""
        return amount_minor if self.kind is BudgetKind.INCOME else -amount_minor


class BudgetOverride(_Record):
    entry_id: uuid.UUID
    #: The occurrence's scheduled date: its identity within the entry (never changes).
    occurrence_date: Day
    state: BudgetOverrideState = BudgetOverrideState.MODIFIED
    name: str | None = None
    amount_minor: int | None = Field(default=None, ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    #: The date the occurrence was moved to (None = its scheduled date).
    date: Day | None = None
    category: str | None = None
    project_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str | None) -> str | None:
        return _clean_name(value)

    @field_validator("occurrence_date", "date")
    @classmethod
    def _valid_dates(cls, value: Day | None) -> Day | None:
        return _check_date(value)

    @model_validator(mode="after")
    def _consistent(self) -> "BudgetOverride":
        if self.state is BudgetOverrideState.SKIPPED and any(getattr(self, name) is not None
                                                             for name in OVERRIDE_FIELDS):
            raise ValueError("a skipped occurrence has no replacement values")
        if self.category is not None and not self.category.strip():
            raise ValueError("a category cannot be empty")
        return self


MAX_DESCRIPTION_LENGTH = 4000


def _clean_description(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"a description has at most {MAX_DESCRIPTION_LENGTH} characters")
    return value or None


class IncomeIdea(_Record):
    name: str
    expected_amount_minor: int = Field(ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    #: Received or firmly secured so far (may exceed the expectation).
    secured_amount_minor: int = Field(default=0, ge=0, le=MAX_AMOUNT_MINOR, strict=True)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("description")
    @classmethod
    def _valid_description(cls, value: str | None) -> str | None:
        return _clean_description(value)

    @property
    def progress_percent(self) -> int:
        """Secured / expected as a whole percentage, rounded down (100 or more once the expectation is met)."""
        return self.secured_amount_minor * 100 // self.expected_amount_minor

    @property
    def remaining_minor(self) -> int:
        return max(0, self.expected_amount_minor - self.secured_amount_minor)

    @property
    def reached(self) -> bool:
        return self.secured_amount_minor >= self.expected_amount_minor


class WishlistItem(_Record):
    name: str
    amount_minor: int = Field(ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    category: str
    project_id: uuid.UUID | None = None
    description: str | None = None
    #: The expense this item was turned into, and that expense's date (both set together, once).
    converted_entry_id: uuid.UUID | None = None
    converted_on: Day | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("description")
    @classmethod
    def _valid_description(cls, value: str | None) -> str | None:
        return _clean_description(value)

    @field_validator("converted_on")
    @classmethod
    def _valid_converted_on(cls, value: Day | None) -> Day | None:
        return _check_date(value)

    @model_validator(mode="after")
    def _consistent(self) -> "WishlistItem":
        problem = category_problem(BudgetKind.EXPENSE, self.category)
        if problem:
            raise ValueError(problem)
        if (self.converted_entry_id is None) != (self.converted_on is None):
            raise ValueError("a converted item records both its expense and that expense's date")
        return self

    @property
    def converted(self) -> bool:
        return self.converted_entry_id is not None


#: The expense a wishlist item turns into has an id derived from the item, so two devices converting the same
#: item create the same expense record (a conflict to resolve, never a second expense).
WISH_EXPENSE_NAMESPACE = uuid.UUID("5d1f4a9e-7c3b-4e2a-9f60-2b8e1c7d4a35")


def wish_expense_id(wish_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(WISH_EXPENSE_NAMESPACE, str(wish_id))


class BudgetOccurrence(BaseModel):
    """One occurrence of an entry within a queried range, with any override applied (never stored)."""

    entry_id: uuid.UUID
    #: The scheduled date (the occurrence's identity); `date` is where it actually falls.
    occurrence_date: Day
    date: Day
    kind: BudgetKind
    name: str
    amount_minor: int
    category: str
    project_id: uuid.UUID | None
    recurrence: BudgetRecurrence
    #: The live override applied to this occurrence, if any.
    override_id: uuid.UUID | None = None

    @property
    def signed_amount_minor(self) -> int:
        return self.amount_minor if self.kind is BudgetKind.INCOME else -self.amount_minor


class BudgetTotals(BaseModel):
    """Totals of the occurrences in a range, in minor units."""

    start_date: Day
    end_date: Day
    income_minor: int
    expense_minor: int
    occurrence_count: int

    @property
    def net_minor(self) -> int:
        return self.income_minor - self.expense_minor
