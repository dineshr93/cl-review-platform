"""Session-wide cleanup so a suite run leaves nothing in /tmp and no open database behind.

`helpers.tmpdir()` hands out 19+ directories per run and `helpers.make_service()` opens a
sqlite database per call; no individual test owns that bookkeeping, so the release happens
once, here, at session end.
"""
from __future__ import annotations

import pytest

import helpers


@pytest.fixture(scope="session", autouse=True)
def _no_temp_dirs_left_behind():
    yield
    helpers.release_tempdirs()
    helpers.close_databases()
