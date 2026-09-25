# pyright: strict
"""Translate spec/schema/store.sql to Postgres (lane 27, spec/schema/README.md, "Storage").

The rules, applied token by token:
  TEXT                          -> text COLLATE "C" (byte order, as SQLite's BINARY)
  INTEGER                       -> bigint
  INTEGER PRIMARY KEY (a rowid) -> bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY
  BLOB                          -> bytea
  ) STRICT;                     -> );
  PRAGMA user_version = N;      -> dropped: the version is the threads_meta row store_version
  a table without a rowid alias -> gains a first column rowid bigint GENERATED ALWAYS AS
                                   IDENTITY, SQLite's implicit rowid, so ORDER BY rowid (insertion
                                   order) is portable; statements name their columns, never *

Every upper-case word must be a keyword this file knows, and every statement a CREATE TABLE or
CREATE [UNIQUE] INDEX, so a construct nobody translated fails here instead of drifting. Two
tables exist only on Postgres: `threads_meta` (the version) and `artifacts` (content-addressed
bytes, which SQLite keeps as files).
"""

import re
from typing import Final

KEYWORDS: Final = frozenset(
    {
        *("CREATE", "TABLE", "INDEX", "IF", "NOT", "EXISTS", "ON", "WHERE", "STRICT"),
        *("PRIMARY", "KEY", "UNIQUE", "REFERENCES", "FOREIGN", "CHECK"),
        *("NULL", "IN", "OR", "AND", "IS", "TEXT", "INTEGER", "BLOB"),
    }
)
TOKEN: Final = re.compile(
    r"\s+|--[^\n]*|'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*|\d+|<>|>=|<=|[(),;=<>]"
)
TYPES: Final = {"TEXT": 'text COLLATE "C"', "INTEGER": "bigint", "BLOB": "bytea"}
ROWID: Final = "bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
ROWID_COLUMN: Final = "rowid bigint GENERATED ALWAYS AS IDENTITY"

POSTGRES_ONLY: Final = (
    'CREATE TABLE IF NOT EXISTS threads_meta (\n  key text COLLATE "C" PRIMARY KEY,\n'
    '  value text COLLATE "C" NOT NULL\n);\n\n'
    'CREATE TABLE IF NOT EXISTS artifacts (\n  sha256 text COLLATE "C" PRIMARY KEY,\n'
    "  size bigint NOT NULL,\n  bytes bytea NOT NULL,\n  created_at bigint NOT NULL\n);\n"
)


class UnknownConstructError(Exception):
    """store.sql holds a construct the Postgres translation doesn't know."""


def tokens(sql: str) -> list[str]:
    out: list[str] = []
    at = 0
    while at < len(sql):
        found = TOKEN.match(sql, at)
        if found is None:
            raise UnknownConstructError(f"store.sql: can't translate {sql[at : at + 20]!r}")
        out.append(found.group())
        at = found.end()
    return out


def statements(sql: str) -> list[list[str]]:
    """The statements, each a list of tokens without whitespace or comments."""
    words = [t for t in tokens(sql) if not t.isspace() and not t.startswith("--")]
    done: list[list[str]] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        if word == ";":
            done.append(current)
            current = []
    if current:
        raise UnknownConstructError("store.sql ends inside a statement")
    return done


def translate(statement: list[str]) -> str | None:
    """One statement in Postgres, or None for the version pragma."""
    if statement[:3] == ["PRAGMA", "user_version", "="]:
        return None
    head = statement[:2] if statement[1] != "UNIQUE" else statement[:3]
    if head not in (["CREATE", "TABLE"], ["CREATE", "INDEX"], ["CREATE", "UNIQUE", "INDEX"]):
        raise UnknownConstructError(f"store.sql: no Postgres form for {' '.join(statement[:4])}")
    for word in statement:
        if word.isupper() and word not in KEYWORDS:
            raise UnknownConstructError(f"store.sql: no Postgres form for {word}")
    if statement[-2:] == ["STRICT", ";"]:
        statement = [*statement[:-2], ";"]
    table = head == ["CREATE", "TABLE"]
    if table and not _has_rowid_alias(statement):
        # SQLite gives every such table an implicit rowid, which listings order by.
        opening = statement.index("(") + 1
        statement = [*statement[:opening], ROWID_COLUMN, ",", *statement[opening:]]
    return render(statement)


def _has_rowid_alias(statement: list[str]) -> bool:
    return any(statement[i : i + 3] == ["INTEGER", "PRIMARY", "KEY"] for i in range(len(statement)))


def render(statement: list[str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(statement):
        word = statement[i]
        rowid = statement[i : i + 3] == ["INTEGER", "PRIMARY", "KEY"]
        if rowid and statement[i + 3] in (",", ")"):
            out.append(ROWID)
            i += 3
            continue
        if word == "STRICT":
            raise UnknownConstructError("store.sql: STRICT only ends a table")
        out.append(TYPES.get(word, word))
        i += 1
    return join(out)


def join(words: list[str]) -> str:
    text = ""
    for word in words:
        glued = word in (",", ")", ";") or text.endswith("(") or not text
        text += word if glued else f" {word}"
    return text


def postgres(sql: str) -> str:
    """The Postgres DDL: every store.sql table and index, then the Postgres-only tables."""
    translated = (translate(s) for s in statements(sql))
    return "\n".join(f"{s}\n" for s in translated if s is not None) + "\n" + POSTGRES_ONLY
