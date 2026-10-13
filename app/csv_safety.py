"""
app/csv_safety.py

Spreadsheet programs treat a cell that starts with "=", "+", "-", "@" (or a
tab or carriage return before one) as a formula. A task name such as
'=HYPERLINK("http://...")' written verbatim into a report would run when the
report is opened. spreadsheet_safe() prefixes such text with an apostrophe,
which spreadsheets show as plain text (OWASP "CSV Injection").

Used by the exports meant to be *opened in a spreadsheet*: the execution
and productivity reports and the legacy 30-minute schedule CSV.

Not used by the planning export (app/planning/csv_export.py): that file is
the lossless data format -- it must round-trip through import exactly, so its
cells are written as they are. Treat it as data for the application, not as
a sheet to open; a name that starts with "=" is kept as typed there.

Numbers are left alone (a negative duration stays a number); only strings
are examined.
"""

from __future__ import annotations

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe(value):
    """`value`, or -- for text a spreadsheet would evaluate -- the same text made inert."""
    if isinstance(value, str) and value.startswith(_FORMULA_STARTS):
        return "'" + value
    return value


def safe_row(row):
    """A row (dict or sequence) with every cell passed through spreadsheet_safe()."""
    if isinstance(row, dict):
        return {key: spreadsheet_safe(cell) for key, cell in row.items()}
    return [spreadsheet_safe(cell) for cell in row]
