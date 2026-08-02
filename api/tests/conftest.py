"""Shared api-test fixtures."""

import pytest

from api import settings


@pytest.fixture(autouse=True)
def _isolate_runtime_settings(monkeypatch):
    """The runtime settings store reads the real Postgres via SessionLocal (like
    the role map). In the test suite that would let production overrides leak into
    tests, so neutralise it by default: every allow-listed read falls back to
    .env/built-in default (what the governance tests expect). Tests that exercise
    the override behaviour re-patch `_load_overrides` themselves."""
    monkeypatch.setattr(settings, "_load_overrides", lambda: {})
