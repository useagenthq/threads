"""spec/tools/store_pg.py: store.sql's Postgres translation, and its refusal of anything else."""

from pathlib import Path

import pytest
from store_pg import UnknownConstructError, postgres

STORE_SQL = Path(__file__).resolve().parents[3] / "spec" / "schema" / "store.sql"


def test_types_collation_rowid_and_strict() -> None:
    sql = (
        "-- a comment\nCREATE TABLE IF NOT EXISTS t (\n  id INTEGER PRIMARY KEY,\n"
        "  name TEXT NOT NULL,\n  n INTEGER,\n  b BLOB\n) STRICT;\nPRAGMA user_version = 7;\n"
    )
    ddl = postgres(sql).split("\n\n")[0]
    assert ddl == (
        "CREATE TABLE IF NOT EXISTS t (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
        ' name text COLLATE "C" NOT NULL, n bigint, b bytea);'
    )


def test_partial_indexes_and_checks_are_kept() -> None:
    sql = (
        "CREATE UNIQUE INDEX IF NOT EXISTS i ON t (a, b) WHERE state = 'pending';\n"
        "CREATE TABLE IF NOT EXISTS u (s TEXT CHECK (s IN ('a', 'b')), CHECK (s <> 'c'));\n"
    )
    assert postgres(sql).startswith(
        "CREATE UNIQUE INDEX IF NOT EXISTS i ON t (a, b) WHERE state = 'pending';\n\n"
        "CREATE TABLE IF NOT EXISTS u (rowid bigint GENERATED ALWAYS AS IDENTITY,"
        " s text COLLATE \"C\" CHECK (s IN ('a', 'b')),"
        " CHECK (s <> 'c'));\n"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE VIEW v AS SELECT 1;",
        "CREATE TABLE IF NOT EXISTS t (a REAL);",
        "CREATE TABLE IF NOT EXISTS t (a TEXT DEFAULT CURRENT_TIMESTAMP);",
        "CREATE TABLE IF NOT EXISTS t (a INTEGER PRIMARY KEY AUTOINCREMENT);",
        "CREATE TABLE IF NOT EXISTS t (a TEXT) WITHOUT ROWID;",
        "CREATE TRIGGER x AFTER INSERT ON t BEGIN SELECT 1; END;",
        "CREATE TABLE IF NOT EXISTS t (a TEXT)",
    ],
)
def test_an_unknown_construct_fails(sql: str) -> None:
    with pytest.raises(UnknownConstructError):
        postgres(sql)


def test_every_table_gets_explicit_byte_collation() -> None:
    ddl = postgres(STORE_SQL.read_text(encoding="utf-8"))
    assert " TEXT" not in ddl
    assert "STRICT" not in ddl
    assert "PRAGMA" not in ddl
    for table in ("team_members", "schedule_threads", "questions", "artifacts", "threads_meta"):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in ddl
