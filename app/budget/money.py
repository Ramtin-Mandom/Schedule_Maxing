"""
app/budget/money.py

Exact money. Every amount is stored and computed as an integer number of
minor units (cents: MINOR_DIGITS = 2), so sums are exact; Decimal is used
only at the edges (parsing user text, showing a value). A float is never
accepted -- 0.1 + 0.2 is not 0.3 in binary floating point -- and neither is
a value with more decimal places than the currency has (rounding would
silently change what the user typed).

There is one currency per workspace; the code does not record which.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.budget.errors import BudgetValidationError

#: Decimal places of the (single) currency.
MINOR_DIGITS = 2
_SCALE = 10 ** MINOR_DIGITS
#: The largest magnitude of any stored amount, in minor units (just under 100 billion whole units).
MAX_AMOUNT_MINOR = 10 ** 13 - 1


def parse_amount(value: object, *, allow_negative: bool = False, allow_zero: bool = False) -> int:
    """
    `value` as integer minor units. Accepts an int (already minor units), a
    Decimal or a decimal string ("12", "12.5", "-3.07") in major units.
    Refuses floats, bools, more than MINOR_DIGITS decimals, values beyond
    MAX_AMOUNT_MINOR, and -- unless allowed -- negative or zero amounts.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise BudgetValidationError("Amounts must be exact: pass a decimal string, a Decimal or minor units, "
                                    "never a float.")
    if isinstance(value, int):
        minor = value
    elif isinstance(value, (str, Decimal)):
        try:
            number = Decimal(value.strip()) if isinstance(value, str) else value
        except InvalidOperation:
            raise BudgetValidationError(f"{value!r} is not an amount.") from None
        if not number.is_finite():
            raise BudgetValidationError(f"{value!r} is not an amount.")
        scaled = number * _SCALE
        if scaled != scaled.to_integral_value():
            raise BudgetValidationError(f"An amount has at most {MINOR_DIGITS} decimal places.")
        minor = int(scaled)
    else:
        raise BudgetValidationError(f"{value!r} is not an amount.")
    return check_amount(minor, allow_negative=allow_negative, allow_zero=allow_zero)


def check_amount(minor: int, *, allow_negative: bool = False, allow_zero: bool = False) -> int:
    """`minor` itself, if it is an acceptable amount in minor units."""
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise BudgetValidationError("An amount in minor units is a whole number.")
    if abs(minor) > MAX_AMOUNT_MINOR:
        raise BudgetValidationError("The amount is too large.")
    if minor < 0 and not allow_negative:
        raise BudgetValidationError("The amount cannot be negative.")
    if minor == 0 and not allow_zero:
        raise BudgetValidationError("The amount must be greater than zero.")
    return minor


def to_decimal(minor: int) -> Decimal:
    """Minor units as an exact Decimal in major units (1234 -> Decimal('12.34'))."""
    return Decimal(minor).scaleb(-MINOR_DIGITS)


def format_amount(minor: int) -> str:
    """Minor units as plain text in major units, always with MINOR_DIGITS decimals (-5 -> '-0.05')."""
    sign = "-" if minor < 0 else ""
    whole, part = divmod(abs(minor), _SCALE)
    return f"{sign}{whole}.{part:0{MINOR_DIGITS}d}"
