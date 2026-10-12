"""Shared test fixtures: a real PostgreSQL (bundled via pgserver)."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from collections.abc import Iterator

import pgserver
import pytest

# psycopg async mode is incompatible with Windows' default ProactorEventLoop;
# the selector loop is required for AsyncPostgresSaver tests on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session")
def pg_uri() -> Iterator[str]:
    """Session-scoped PostgreSQL over a UNIX socket (no root required)."""
    dbdir = tempfile.mkdtemp(prefix="warden-pg-")
    srv = pgserver.get_server(dbdir)  # type: ignore[attr-defined]
    try:
        yield srv.get_uri()
    finally:
        try:
            srv.cleanup()
        finally:
            shutil.rmtree(dbdir, ignore_errors=True)
