"""The portable subset (spec/schema/README.md, "Storage"): no statement in the store's code uses
a form only one engine runs, and none runs outside a transaction. The contract suite is the real
proof; this scan catches the known traps before it runs. The SQLite driver (PRAGMAs) and the
SQLite-only FTS5 providers are exempt."""

import io
import re
import tokenize
from pathlib import Path

import pytest

from threads.store.sqlite_driver import connect

SRC = Path(__file__).resolve().parents[2] / "src" / "threads"
EXEMPT = {
    SRC / "store" / "sqlite_driver.py",
    SRC / "store" / "conn.py",
    SRC / "memory" / "local_memory.py",
    SRC / "memory" / "local_knowledge.py",
    SRC / "memory" / "sqlite_fts.py",
}
BANNED = {
    "changes()": re.compile(r"\bchanges\(\)", re.I),
    "IS ?": re.compile(r"\bIS\s+\?"),
    "two-argument max/min": re.compile(r"\b(max|min)\([^()]*,[^()]*\)", re.I),
    "INSERT OR": re.compile(r"\bINSERT\s+OR\b", re.I),
    "PRAGMA": re.compile(r"\bPRAGMA\b", re.I),
    "datetime(": re.compile(r"\bdatetime\(", re.I),
    "unixepoch(": re.compile(r"\bunixepoch\(", re.I),
    "SELECT *": re.compile(r"\bSELECT\s+\*\s+FROM\b", re.I),
}
SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|ON CONFLICT|WHERE)\b")


def _sql_strings(path: Path) -> list[tuple[int, str]]:
    """String literals that look like SQL (the scan's haystack), with their line."""
    found: list[tuple[int, str]] = []
    tokens = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
    for token in tokens:
        if token.type == tokenize.STRING and SQL.search(token.string):
            found.append((token.start[0], token.string))
    return found


def _violations() -> list[str]:
    out: list[str] = []
    for path in sorted(SRC.glob("**/*.py")):
        if path in EXEMPT or "_generated" in path.parts:
            continue
        for line, text in _sql_strings(path):
            out += [
                f"{path.relative_to(SRC)}:{line}: {name}"
                for name, banned in BANNED.items()
                if banned.search(text)
            ]
    return out


def test_no_statement_uses_a_form_only_one_engine_runs() -> None:
    assert _violations() == []


def test_the_scan_catches_each_banned_form(tmp_path: Path) -> None:
    for name, banned in BANNED.items():
        sample = {
            "changes()": "SELECT changes()",
            "IS ?": "SELECT 1 WHERE a IS ?",
            "two-argument max/min": "UPDATE t SET a = max(a, ?)",
            "INSERT OR": "INSERT OR IGNORE INTO t VALUES (1)",
            "PRAGMA": "PRAGMA user_version",
            "datetime(": "SELECT datetime('now')",
            "unixepoch(": "SELECT unixepoch()",
            "SELECT *": "SELECT * FROM t WHERE a = 1",
        }[name]
        assert banned.search(sample), name


def test_a_statement_outside_a_transaction_is_a_bug(tmp_path: Path) -> None:
    conn = connect(str(tmp_path / "threads.db"))
    try:
        with pytest.raises(AssertionError, match="inside a transaction"):
            conn.execute("SELECT 1")
    finally:
        conn.close()
