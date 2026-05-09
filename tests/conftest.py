"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from app.state import state


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Wipe the global state before AND after every test so cases stay
    independent (locks are torn down too — they'll be re-bound to the
    test's event loop on next access)."""
    state.reset()
    yield
    state.reset()
