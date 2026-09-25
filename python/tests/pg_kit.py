"""The Postgres leg of the store tests (lane 27).

THREADS_TEST_POSTGRES_URL names a server; without it the leg is skipped with a visible reason,
unless THREADS_TEST_POSTGRES_REQUIRED=1 (the Linux CI job), where it fails. Each in-memory store a
test opens on the Postgres leg is a fresh schema of that server, dropped after the test: the same
code path as `postgres()`, reached through `SqliteStore.open()` (":memory:").
"""

import os
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from urllib.parse import quote

import psycopg
import pytest

from threads.log import ParseError
from threads.postgres.driver import PgConn, connector
from threads.postgres.opening import open_postgres
from threads.result import Err, Ok
from threads.store import LOCAL_TENANT, ArtifactStore, SqliteStore
from threads.store.conn import Conn

URL: Final = os.environ.get("THREADS_TEST_POSTGRES_URL")
REQUIRED: Final = os.environ.get("THREADS_TEST_POSTGRES_REQUIRED") == "1"
ENGINE: Final = os.environ.get("THREADS_TEST_STORE", "sqlite")
"""Which engine the conformance runners and the jobs drills use: sqlite or postgres."""


def need_postgres() -> str:
    """The server URL, else skip (or fail, where the Postgres leg is required)."""
    if URL:
        return URL
    reason = "Postgres leg: set THREADS_TEST_POSTGRES_URL to run it"
    if REQUIRED:
        pytest.fail(f"{reason} (THREADS_TEST_POSTGRES_REQUIRED=1)")
    pytest.skip(reason)


def schema_url(url: str, schema: str) -> str:
    """The URL with its search_path set to one schema."""
    options = quote(f"-csearch_path={schema}")
    return f"{url}{'&' if '?' in url else '?'}options={options}"


def admin(url: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(url, autocommit=True)


def create_schema(url: str) -> str:
    schema = f"t_{uuid.uuid4().hex[:16]}"
    with admin(url) as conn:
        conn.execute(f"CREATE SCHEMA {schema}".encode())
    return schema


def drop_schema(url: str, schema: str) -> None:
    with admin(url) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE".encode())


class Leg:
    """The schemas and connections one test's Postgres stores made, released after it."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.schemas: list[str] = []
        self.conns: list[PgConn] = []

    def schema(self) -> str:
        made = create_schema(self.url)
        self.schemas.append(made)
        return made

    def connect(self, url: str) -> Callable[[], Conn]:
        make = connector(url)

        def recorded() -> Conn:
            conn = make()
            self.conns.append(conn)
            return conn

        return recorded

    async def open(
        self, tenant_id: str = LOCAL_TENANT, artifacts: ArtifactStore | None = None
    ) -> Ok[SqliteStore] | Err[ParseError]:
        url = schema_url(self.url, self.schema())
        return await open_postgres(url, tenant_id, connect=self.connect(url), artifacts=artifacts)

    def close(self) -> None:
        for conn in self.conns:
            conn.raw.close()
        for schema in self.schemas:
            drop_schema(self.url, schema)


@contextmanager
def postgres_memory(monkeypatch: pytest.MonkeyPatch) -> Generator[Leg]:
    """Every in-memory store opened meanwhile is a fresh Postgres schema."""
    leg = Leg(need_postgres())
    original = SqliteStore.open

    async def opened(
        path: str | Path = ":memory:",
        *,
        tenant_id: str = LOCAL_TENANT,
        artifacts: ArtifactStore | None = None,
    ) -> Ok[SqliteStore] | Err[ParseError]:
        if str(path) != ":memory:":
            return await original(path, tenant_id=tenant_id, artifacts=artifacts)
        return await leg.open(tenant_id, artifacts)

    monkeypatch.setattr(SqliteStore, "open", opened)
    try:
        yield leg
    finally:
        leg.close()
