"""`local_knowledge()` (spec/api.json): admitted versions stored as
artifacts, an FTS5 index over their passages, and a monotonic revision.

Admitted versions are immutable and removes are tombstones, so a search can be answered as of
any revision (a pinned fork). The index is a view: `rebuild_index` drops it and refills it from
the admitted artifacts, and nothing reads it for what an earlier run saw.
"""

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from threads.log import ArtifactRef
from threads.memory import passages
from threads.memory.sqlite_fts import install, match_query, transaction
from threads.memory.types import (
    Doc,
    DocVersion,
    KnowledgeHit,
    KnowledgeSource,
    Outcome,
    ProviderError,
    Scope,
)
from threads.redaction import SecretInStoredBytesError, contains_secret, published
from threads.result import Err, Ok
from threads.store import SqliteStore

_DDL: Final = """
CREATE TABLE IF NOT EXISTS local_knowledge_docs (
  doc_id TEXT NOT NULL, version TEXT NOT NULL, revision INTEGER NOT NULL,
  removed_revision INTEGER, content_sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
  media_type TEXT NOT NULL, location TEXT, namespace TEXT NOT NULL, record_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL, agent TEXT NOT NULL, scope TEXT NOT NULL,
  UNIQUE (tenant_id, agent, scope, doc_id, version)
) STRICT;
CREATE TABLE IF NOT EXISTS local_knowledge_keys (
  key TEXT PRIMARY KEY, digest TEXT NOT NULL, doc_id TEXT NOT NULL, version TEXT NOT NULL,
  revision INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS local_knowledge_revision (
  one INTEGER PRIMARY KEY CHECK (one = 1), revision INTEGER NOT NULL
) STRICT;
CREATE VIRTUAL TABLE IF NOT EXISTS local_knowledge_fts
  USING fts5(text, doc_rowid UNINDEXED, span_start UNINDEXED, span_end UNINDEXED);
"""
_SCOPE: Final = "d.tenant_id = ? AND d.agent = ? AND d.scope = ?"
_LIVE: Final = f"""
d.revision <= ? AND (d.removed_revision IS NULL OR d.removed_revision > ?)
AND d.revision = (SELECT max(e.revision) FROM local_knowledge_docs e
  WHERE e.doc_id = d.doc_id AND e.tenant_id = d.tenant_id AND e.agent = d.agent
  AND e.scope = d.scope AND e.revision <= ?) AND {_SCOPE}
"""
_SEARCH: Final = f"""
SELECT d.doc_id, d.version, f.span_start, f.span_end, f.text, d.namespace, d.record_id,
  bm25(local_knowledge_fts)
FROM local_knowledge_fts f JOIN local_knowledge_docs d ON d.rowid = f.doc_rowid
WHERE local_knowledge_fts MATCH ? AND {_LIVE}
"""
_UNBOUND: Final = Err(ProviderError("unavailable", "local_knowledge isn't bound to a store"))
_TEXT: Final = ("text/", "application/json")
_REBUILD_ATTEMPTS: Final = 3
"""A rebuild reads its sources outside the transaction; an ingest in between means reading
again, a few times, before giving up with unavailable."""

type _Row = tuple[str, str, int, int, str, str, str, float]


def _where(scope: Scope) -> tuple[str, str, str]:
    return (scope.tenant_id, scope.agent, scope.scope)


def _revision(conn: sqlite3.Connection) -> int:
    row: tuple[int] | None = conn.execute(
        "SELECT revision FROM local_knowledge_revision WHERE one = 1"
    ).fetchone()
    return 0 if row is None else row[0]


def _bump(conn: sqlite3.Connection) -> int:
    revision = _revision(conn) + 1
    conn.execute(
        "INSERT INTO local_knowledge_revision (one, revision) VALUES (1, ?)"
        " ON CONFLICT (one) DO UPDATE SET revision = excluded.revision",
        (revision,),
    )
    return revision


def _index(conn: sqlite3.Connection, rowid: int, text: str) -> None:
    for span, body in passages.split(text):
        conn.execute(
            "INSERT INTO local_knowledge_fts (text, doc_rowid, span_start, span_end)"
            " VALUES (?, ?, ?, ?)",
            (body, rowid, span[0], span[1]),
        )


@dataclass(frozen=True, slots=True)
class LocalKnowledge:
    """spec/api.json `KnowledgeProvider`. `paths` are ingested by the host at run setup."""

    paths: tuple[str, ...]
    store: SqliteStore | None = None

    async def bind(self, store: SqliteStore) -> "LocalKnowledge":
        await store.run(install(_DDL))
        return replace(self, store=store)

    async def revision(self, scope: Scope) -> Outcome[int]:
        """The store's current revision (0 before the first ingest). One revision covers every
        scope, so `scope` only satisfies the protocol."""
        return Ok(0 if self.store is None else await self.store.run(_revision))

    async def ingest(self, scope: Scope, source: KnowledgeSource, key: str) -> Outcome[DocVersion]:
        if self.store is None:
            return _UNBOUND
        if not source.media_type.startswith(_TEXT):
            return Err(ProviderError("invalid", f"can't parse {source.media_type}"))
        try:
            text = source.content.decode("utf-8")
        except UnicodeDecodeError:
            return Err(ProviderError("invalid", f"{source.source_id}: not UTF-8 text"))
        if contains_secret(source.content):
            # Byte-exact (its digest is its version): a source holding a value is refused.
            message = f"{source.source_id}: holds a registered secret; not ingested"
            return Err(ProviderError("invalid", message))
        digest = hashlib.sha256(source.content).hexdigest()
        admit = transaction(_admit(scope, source, key, digest, text))
        try:
            # The bytes are durable before the rows (and passages) that reference them, and a
            # value registered after the check above can't slip in between.
            return await self.store.run(admit, publishing=source.content)
        except SecretInStoredBytesError:
            message = f"{source.source_id}: holds a registered secret; not ingested"
            return Err(ProviderError("invalid", message))

    async def remove(self, scope: Scope, doc_id: str, key: str) -> Outcome[None]:
        if self.store is None:
            return _UNBOUND

        def write(conn: sqlite3.Connection) -> Outcome[None]:
            if conn.execute("SELECT 1 FROM local_knowledge_keys WHERE key = ?", (key,)).fetchone():
                return Ok(None)
            revision = _bump(conn)
            conn.execute(
                "UPDATE local_knowledge_docs AS d SET removed_revision = ?"
                f" WHERE d.doc_id = ? AND d.removed_revision IS NULL AND {_SCOPE}",
                (revision, doc_id, *_where(scope)),
            )
            conn.execute(
                "INSERT INTO local_knowledge_keys VALUES (?, 'remove', ?, '', ?)",
                (key, doc_id, revision),
            )
            return Ok(None)

        return await self.store.run(transaction(write))

    async def search(
        self,
        scope: Scope,
        query: str,
        *,
        k: int = 5,
        sources: Sequence[str] | None = None,
        as_of: int | None = None,
    ) -> Outcome[Sequence[KnowledgeHit]]:
        if self.store is None:
            return _UNBOUND
        match = match_query(query)
        if match is None:
            return Ok(())
        only = tuple(sources or ())
        sql = _SEARCH
        if only:
            sql += f" AND d.doc_id IN ({', '.join('?' * len(only))})"
        sql += " ORDER BY bm25(local_knowledge_fts) LIMIT ?"

        def read(conn: sqlite3.Connection) -> list[_Row]:
            at = _revision(conn) if as_of is None else as_of
            return conn.execute(sql, (match, at, at, at, *_where(scope), *only, k)).fetchall()

        return Ok(tuple(_hit(row) for row in await self.store.run(read)))

    async def get(self, scope: Scope, doc_id: str, version: str) -> Outcome[Doc]:
        if self.store is None:
            return _UNBOUND

        def read(conn: sqlite3.Connection) -> tuple[str, int, str, str, str] | None:
            return conn.execute(
                "SELECT content_sha256, bytes, media_type, namespace, record_id"
                f" FROM local_knowledge_docs AS d WHERE doc_id = ? AND version = ? AND {_SCOPE}",
                (doc_id, version, *_where(scope)),
            ).fetchone()

        row = await self.store.run(read)
        if row is None:
            return Err(ProviderError("not_found", f"no {doc_id}@{version} in this scope"))
        sha, size, media_type, namespace, record_id = row
        ref = ArtifactRef(sha256=sha, bytes=size, media_type=media_type)
        binding = {"namespace": namespace, "record_id": record_id}
        return Ok(
            Doc.model_validate(
                {
                    "doc_id": doc_id,
                    "version": version,
                    "media_type": media_type,
                    "content_ref": ref,
                    "binding": binding,
                }
            )
        )

    async def rebuild_index(self) -> Outcome[None]:
        """Drops the index and refills it from the admitted artifacts (F14.3), in one step. A
        source holding a value registered since it was admitted refuses the rebuild before
        anything is cleared: the index stays as it was (C5)."""
        store = self.store
        if store is None:
            return _UNBOUND

        for _ in range(_REBUILD_ATTEMPTS):
            try:
                if await _rebuilt(store):
                    return Ok(None)
            except SecretInStoredBytesError:
                return Err(
                    ProviderError("invalid", "a source holds a registered secret; index kept")
                )
        return Err(ProviderError("unavailable", "sources kept changing during the rebuild"))


async def _rebuilt(store: SqliteStore) -> bool:
    """One rebuild attempt: reads the admitted sources, then checks them and refills the index
    in one transaction, with registration paused through its commit. False when an ingest
    moved the rows."""
    read = await store.run(_admitted)
    sources: list[tuple[int, bytes]] = []
    for rowid, sha in read:
        got = await store.get_artifact(sha)
        if isinstance(got, Err):
            raise AssertionError(f"an admitted version's artifact is gone: {sha}")
        sources.append((rowid, got.value))
    pieces = [content for _, content in sources]

    def refill(conn: sqlite3.Connection) -> bool:
        # Registration stays paused until the transaction has committed, not only while the
        # rows are written: the check covers the index until it is durable.
        return published(pieces, lambda: transaction(lambda c: _refill(c, read, sources))(conn))

    return await store.run(refill)


def _insert(
    conn: sqlite3.Connection, scope: Scope, source: KnowledgeSource, at: DocVersion, text: str
) -> None:
    b = source.binding
    cursor = conn.execute(
        "INSERT INTO local_knowledge_docs (doc_id, version, revision, content_sha256, bytes,"
        " media_type, location, namespace, record_id, tenant_id, agent, scope)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            at.doc_id,
            at.version,
            at.revision,
            at.content_sha256,
            len(source.content),
            source.media_type,
            source.location,
            b.namespace,
            b.record_id,
            *_where(scope),
        ),
    )
    if cursor.lastrowid is None:
        raise AssertionError("an INSERT has a rowid")
    _index(conn, cursor.lastrowid, text)


def _admit(
    scope: Scope, source: KnowledgeSource, key: str, digest: str, text: str
) -> Callable[[sqlite3.Connection], Outcome[DocVersion]]:
    version = digest[:16]

    def write(conn: sqlite3.Connection) -> Outcome[DocVersion]:
        done: tuple[str, str, str, int] | None = conn.execute(
            "SELECT digest, doc_id, version, revision FROM local_knowledge_keys WHERE key = ?",
            (key,),
        ).fetchone()
        if done is not None:
            if done[:2] != (digest, source.source_id):
                return Err(ProviderError("invalid", f"key {key} reused with other content"))
            return Ok(_version(done[1], done[2], digest, done[3]))
        revision = _bump(conn)
        found: tuple[int] | None = conn.execute(
            f"SELECT rowid FROM local_knowledge_docs AS d WHERE doc_id = ? AND version = ?"
            f" AND {_SCOPE}",
            (source.source_id, version, *_where(scope)),
        ).fetchone()
        if found is not None:
            # ponytail: re-admitting removed bytes moves the row's revision, so an as_of search
            # before the removal misses it; keep one row per admission if that history matters.
            conn.execute(
                "UPDATE local_knowledge_docs SET revision = ?, removed_revision = NULL"
                " WHERE rowid = ? AND removed_revision IS NOT NULL",
                (revision, found[0]),
            )
        else:
            _insert(
                conn, scope, source, _version(source.source_id, version, digest, revision), text
            )
        conn.execute(
            "INSERT INTO local_knowledge_keys VALUES (?, ?, ?, ?, ?)",
            (key, digest, source.source_id, version, revision),
        )
        return Ok(_version(source.source_id, version, digest, revision))

    return write


def _admitted(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    return conn.execute(
        "SELECT rowid, content_sha256 FROM local_knowledge_docs ORDER BY rowid"
    ).fetchall()


def _refill(
    conn: sqlite3.Connection, read: list[tuple[int, str]], sources: Sequence[tuple[int, bytes]]
) -> bool:
    """Clears and refills the index, in the caller's transaction, only if the admitted rows are
    still the ones `sources` was read from: an ingest in between would be dropped (False)."""
    if _admitted(conn) != read:
        return False
    conn.execute("DELETE FROM local_knowledge_fts")
    for rowid, content in sources:
        _index(conn, rowid, content.decode("utf-8"))
    return True


def _version(doc_id: str, version: str, digest: str, revision: int) -> DocVersion:
    return DocVersion(doc_id=doc_id, version=version, content_sha256=digest, revision=revision)


def _hit(row: _Row) -> KnowledgeHit:
    doc_id, version, start, end, text, namespace, record_id, bm25 = row
    return KnowledgeHit.model_validate(
        {
            "doc_id": doc_id,
            "version": version,
            "span": {"start": start, "end": end},
            "text": text,
            "score": -bm25,
            "binding": {"namespace": namespace, "record_id": record_id},
        }
    )


def local_knowledge(*, paths: Sequence[str]) -> LocalKnowledge:
    """spec/api.json `local_knowledge`. Pure: the host ingests `paths` when a run binds it."""
    return LocalKnowledge(tuple(paths))
