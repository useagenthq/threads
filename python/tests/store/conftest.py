"""Every store test runs on both engines: `store_factory` makes each in-memory store a SQLite
one, then a Postgres one (a fresh schema; the leg is skipped without a server). A test that pins
SQLite itself (its pragmas, its file) is marked `sqlite_only`."""

from collections.abc import Iterator

import pytest
from pg_kit import postgres_memory


@pytest.fixture(autouse=True, params=["sqlite", "postgres"])
def store_factory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    engine: str = request.param
    if engine == "sqlite":
        yield engine
        return
    if "sqlite_only" in request.keywords:
        pytest.skip("pins SQLite itself")
    with postgres_memory(monkeypatch):
        yield engine
