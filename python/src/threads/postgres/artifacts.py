"""Artifacts as rows of the `artifacts` table: content-addressed, re-hashed on every read, and
shared by every machine on the database. `created_at` is the putting process's clock, refreshed
by a re-put, and gc's grace window is measured against it."""

from collections.abc import Callable
from functools import partial
from typing import Final

from threads.log import ParseError
from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.store.artifacts import ArtifactSink
from threads.store.conn import Conn, StoreError, run
from threads.store.sql import blob_of, text_of

PAGE: Final = 1000
"""gc reads candidates a page at a time, so no scan holds a long serializable transaction."""


class PgArtifacts:
    def __init__(self, conn: Conn, clock: Callable[[], int]) -> None:
        self._conn = conn
        self._clock = clock

    def put(self, data: bytes) -> str:
        """Stores the bytes, in their own transaction or the caller's. A row already there is
        verified, as a file already there is, and its `created_at` refreshed."""
        sha, now = sha256_hex(data), self._clock()

        def write(conn: Conn) -> str:
            row = conn.execute(
                "INSERT INTO artifacts (sha256, size, bytes, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (sha256) DO UPDATE SET created_at = EXCLUDED.created_at"
                " RETURNING encode(sha256(bytes), 'hex')",
                (sha, len(data), data, now),
            ).fetchone()
            return "" if row is None else text_of(row[0])

        if run(self._conn, write) != sha:
            raise StoreError(f"artifact {sha} is stored with other bytes")
        return sha

    def get(self, sha256: str) -> Ok[bytes] | Err[ParseError]:
        row = run(
            self._conn,
            lambda c: c.execute(
                "SELECT bytes FROM artifacts WHERE sha256 = ?", (sha256,)
            ).fetchone(),
            read_only=True,
        )
        if row is None:
            return Err(ParseError("artifact_missing", f"no artifact {sha256}"))
        data = blob_of(row[0])
        if sha256_hex(data) != sha256:
            return Err(ParseError("artifact_corrupt", f"artifact {sha256} fails its hash"))
        return Ok(data)

    def sink(self) -> ArtifactSink:
        # ponytail: a bytea is held whole on put (the spec's scope cut 3); a streaming blob store
        # is the follow-up for multi-gigabyte artifacts.
        return _Sink(self)

    def sweep(self, keep: frozenset[str], older_than: int) -> tuple[str, ...]:
        """A read-only pass lists a page of candidates, then a short transaction deletes each
        one still older than the window: a re-put since the read keeps its artifact."""
        removed: list[str] = []
        after = ""
        while page := run(
            self._conn, partial(candidates, older=older_than, after=after), read_only=True
        ):
            after = page[-1]
            doomed = [sha for sha in page if sha not in keep]
            removed += run(self._conn, partial(delete_older, doomed=doomed, older=older_than))
        return tuple(removed)


def candidates(conn: Conn, *, older: int, after: str) -> list[str]:
    rows = conn.execute(
        "SELECT sha256 FROM artifacts WHERE created_at < ? AND sha256 > ? ORDER BY sha256 LIMIT ?",
        (older, after, PAGE),
    ).fetchall()
    return [text_of(sha) for (sha,) in rows]


def delete_older(conn: Conn, *, doomed: list[str], older: int) -> list[str]:
    deleted = "DELETE FROM artifacts WHERE sha256 = ? AND created_at < ?"
    return [sha for sha in doomed if conn.execute(deleted, (sha, older)).rowcount == 1]


class _Sink:
    def __init__(self, store: PgArtifacts) -> None:
        self._store = store
        self._data = bytearray()

    def write(self, chunk: bytes) -> None:
        self._data += chunk

    def commit(self) -> str:
        return self._store.put(bytes(self._data))

    def discard(self) -> None:
        self._data.clear()
