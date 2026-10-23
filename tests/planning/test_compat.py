"""Tests for app/planning/compat.py: the legacy CSV day-index and dependency-name helpers."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.planning.compat import (
    legacy_day_to_date,
    resolve_legacy_dependency_field,
)

ANCHOR = date(2026, 1, 5)
TZ = "America/New_York"


# -----------------------------------------------------------------------------
# Legacy day-index <-> calendar date
# -----------------------------------------------------------------------------


def test_day_1_is_anchor_date():
    assert legacy_day_to_date(1, ANCHOR) == ANCHOR


def test_day_n_is_anchor_plus_n_minus_1():
    assert legacy_day_to_date(5, ANCHOR) == ANCHOR + timedelta(days=4)


def test_day_0_is_rejected():
    with pytest.raises(ValueError):
        legacy_day_to_date(0, ANCHOR)


# -----------------------------------------------------------------------------
# Dependency-name resolution
# -----------------------------------------------------------------------------


def test_resolve_exact_full_name_match_preserving_hyphen():
    task_id = uuid.uuid4()
    known = {"Pre-Calc Review": [task_id]}
    ids, diagnostics = resolve_legacy_dependency_field("Study Session", "Pre-Calc Review", known)
    assert ids == [task_id]
    assert diagnostics == []


def test_resolve_semicolon_separated_explicit_references():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    known = {"Task A": [id_a], "Task B": [id_b]}
    ids, diagnostics = resolve_legacy_dependency_field("Task C", "Task A;Task B", known)
    assert set(ids) == {id_a, id_b}
    assert diagnostics == []


def test_resolve_hyphen_delimited_when_unambiguous():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    known = {"Task A": [id_a], "Task B": [id_b]}
    ids, diagnostics = resolve_legacy_dependency_field("Task C", "Task A-Task B", known)
    assert set(ids) == {id_a, id_b}
    assert diagnostics == []


def test_resolve_missing_reference_is_ignored_with_diagnostic():
    ids, diagnostics = resolve_legacy_dependency_field("Task C", "Does Not Exist", {})
    assert ids == []
    assert len(diagnostics) == 1
    assert diagnostics[0].kind == "missing"


def test_resolve_ambiguous_duplicate_name_is_reported_not_guessed():
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    known = {"Study Session": [id_a, id_b]}
    ids, diagnostics = resolve_legacy_dependency_field("Follow Up", "Study Session", known)
    assert ids == []
    assert len(diagnostics) == 1
    assert diagnostics[0].kind == "ambiguous"


def test_resolve_empty_field_returns_nothing():
    ids, diagnostics = resolve_legacy_dependency_field("Task C", "", {})
    assert ids == []
    assert diagnostics == []


def test_resolve_hyphenated_name_preferred_over_hyphen_split():
    """"Pre-Calc Review" must resolve as one task, not split into "Pre" and "Calc Review"."""
    task_id = uuid.uuid4()
    known = {"Pre-Calc Review": [task_id]}
    ids, diagnostics = resolve_legacy_dependency_field("Study Session", "Pre-Calc Review", known)
    assert ids == [task_id]
