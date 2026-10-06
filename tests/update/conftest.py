"""Fixtures of the updater tests."""

from __future__ import annotations

import pytest

from tests.update_fakes import FakeGitHub


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()
