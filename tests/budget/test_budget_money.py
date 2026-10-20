"""Exact money (app/budget/money.py): integer minor units, no floats, no silent rounding."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.budget.errors import BudgetValidationError
from app.budget.money import MAX_AMOUNT_MINOR, format_amount, parse_amount, to_decimal


@pytest.mark.parametrize(("value", "minor"), [
    ("12", 1200), ("12.5", 1250), ("12.50", 1250), ("0.01", 1), (" 1234.56 ", 123456), (Decimal("3.07"), 307),
    (1999, 1999), ("1e3", 100000),
])
def test_amounts_parse_to_exact_minor_units(value, minor) -> None:
    assert parse_amount(value) == minor


@pytest.mark.parametrize("value", [0.1, 1.0, True, "12.345", "abc", "", "NaN", "Infinity", None, [1]])
def test_floats_extra_decimals_and_non_numbers_are_refused(value) -> None:
    with pytest.raises(BudgetValidationError):
        parse_amount(value)


def test_sign_zero_and_bounds() -> None:
    with pytest.raises(BudgetValidationError):
        parse_amount("-5")
    with pytest.raises(BudgetValidationError):
        parse_amount("0")
    assert parse_amount("-5.25", allow_negative=True) == -525
    assert parse_amount("0", allow_zero=True) == 0
    assert parse_amount(MAX_AMOUNT_MINOR) == MAX_AMOUNT_MINOR
    with pytest.raises(BudgetValidationError):
        parse_amount(MAX_AMOUNT_MINOR + 1)


def test_sums_of_minor_units_are_exact_where_floats_are_not() -> None:
    assert 0.1 + 0.2 != 0.3
    assert parse_amount("0.10") + parse_amount("0.20") == parse_amount("0.30")
    assert sum(parse_amount("0.01") for _ in range(1000)) == parse_amount("10")


def test_formatting_round_trips() -> None:
    assert format_amount(123456) == "1234.56"
    assert format_amount(-5) == "-0.05"
    assert format_amount(0) == "0.00"
    assert to_decimal(307) == Decimal("3.07")
    for minor in (1, 99, 100, 123456789, -42):
        assert parse_amount(format_amount(minor), allow_negative=True) == minor
