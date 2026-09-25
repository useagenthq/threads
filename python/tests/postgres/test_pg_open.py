"""Opening a Postgres store: one schema however many open an empty database at once, schemas
that never wait on each other, SQLite's version rules on threads_meta, the Postgres 16 floor,
and tables equal to SQLite's (byte collation on every text column)."""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing

import pytest
from pg_kit import Leg, admin, need_postgres, schema_url

from threads._generated.store_pg_sql import STORE_VERSION
from threads.log import ParseError
from threads.postgres.driver import PgConn, connector, raw_connector
from threads.postgres.opening import lock_key, open_postgres
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.store.conn import Cursor, Params
from threads.store.sql import text_of

OPENERS = 8


@pytest.fixture
def leg() -> Iterator[Leg]:
    made = Leg(need_postgres())
    yield made
    made.close()


def _url(leg: Leg) -> str:
    return schema_url(leg.url, leg.schema())


def test_eight_openers_of_one_empty_database_make_one_schema_with_no_retry(leg: Leg) -> None:
    url = _url(leg)
    conns: list[PgConn] = []

    def recorded() -> PgConn:
        made = connector(url)()
        conns.append(made)
        return made

    async def main() -> list[Ok[SqliteStore] | Err[ParseError]]:
        return await asyncio.gather(
            *(open_postgres(url, "local", connect=recorded) for _ in range(OPENERS))
        )

    opened = asyncio.run(main())
    try:
        assert all(isinstance(o, Ok) for o in opened)
        assert [c.retries for c in conns] == [0] * OPENERS
        with admin(url) as conn:
            rows = conn.execute("SELECT key, value FROM threads_meta").fetchall()
        assert rows == [("store_version", str(STORE_VERSION))]
    finally:
        for c in conns:
            c.raw.close()


def test_another_schema_opens_while_one_schema_is_locked(leg: Leg) -> None:
    locked, free = leg.schema(), _url(leg)
    with admin(leg.url) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (lock_key(locked),))

        async def main() -> Ok[SqliteStore] | Err[ParseError]:
            return await asyncio.wait_for(open_postgres(free, "local"), timeout=10)

        opened = asyncio.run(main())
        assert isinstance(opened, Ok)
        asyncio.run(opened.value.close())


@pytest.mark.parametrize("found", [STORE_VERSION - 1, STORE_VERSION + 1])
def test_another_store_version_is_unsupported_format(leg: Leg, found: int) -> None:
    url = _url(leg)
    first = asyncio.run(open_postgres(url, "local"))
    assert isinstance(first, Ok)
    asyncio.run(first.value.close())
    with admin(url) as conn:
        conn.execute("UPDATE threads_meta SET value = %s", (str(found),))
    again = asyncio.run(open_postgres(url, "local"))
    assert isinstance(again, Err)
    assert again.error.code == "unsupported_format"


class _Postgres15(PgConn):
    """A server that says it is Postgres 15."""

    def _execute(self, sql: str, params: Params) -> Cursor:
        if sql == "SHOW server_version_num":
            return super()._execute("SELECT '150004'", params)
        if sql == "SHOW server_version":
            return super()._execute("SELECT '15.4'", params)
        return super()._execute(sql, params)


def test_postgres_before_16_is_unsupported_format_naming_the_version(leg: Leg) -> None:
    url = _url(leg)
    conns: list[PgConn] = []

    def old() -> PgConn:
        made = _Postgres15(raw_connector(url))
        conns.append(made)
        return made

    opened = asyncio.run(open_postgres(url, "local", connect=old))
    assert isinstance(opened, Err)
    assert opened.error.code == "unsupported_format"
    assert "15.4" in opened.error.message


def _sqlite_tables() -> dict[str, list[tuple[str, str, bool]]]:
    from threads._generated.store_sql import STORE_SQL  # noqa: PLC0415

    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(STORE_SQL)
        names = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        out: dict[str, list[tuple[str, str, bool]]] = {}
        for name in names:
            cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
            out[name] = sorted((c[1], str(c[2]).lower(), bool(c[5])) for c in cols)
        return out


_TYPES = {"text": "text", "bigint": "integer", "bytea": "blob"}


def test_the_tables_equal_sqlites_and_every_text_column_sorts_by_bytes(leg: Leg) -> None:
    url = _url(leg)
    opened = asyncio.run(open_postgres(url, "local"))
    assert isinstance(opened, Ok)
    asyncio.run(opened.value.close())
    schema = leg.schemas[-1]
    with admin(url) as conn:
        cols = conn.execute(
            "SELECT c.table_name, c.column_name, c.data_type, c.collation_name,"
            " EXISTS (SELECT 1 FROM information_schema.key_column_usage k"
            "   JOIN information_schema.table_constraints t"
            "     ON t.constraint_name = k.constraint_name AND t.table_schema = k.table_schema"
            "   WHERE t.constraint_type = 'PRIMARY KEY' AND k.table_schema = c.table_schema"
            "     AND k.table_name = c.table_name AND k.column_name = c.column_name)"
            " FROM information_schema.columns c WHERE c.table_schema = %s",
            (schema,),
        ).fetchall()
    tables: dict[str, list[tuple[str, str, bool]]] = {}
    for row in cols:
        table, column, kind = (text_of(v) for v in row[:3])
        collation, pk = row[3:]
        if kind == "text":
            assert collation == "C", (table, column)
        if column == "rowid" and not pk:
            continue  # SQLite's implicit rowid, spelled out
        tables.setdefault(table, []).append((column, _TYPES[kind], bool(pk)))
    for only in ("threads_meta", "artifacts"):
        tables.pop(only)
    assert {t: sorted(c) for t, c in tables.items()} == _sqlite_tables()
