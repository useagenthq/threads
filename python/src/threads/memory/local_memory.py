"""`local_memory()` (spec/api.json): records in the run's store, recalled with
FTS5. Writes are idempotent on their key forever, so a replayed or retried save never
duplicates and a retried forget never double-deletes."""

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from threads.log import EffectClass
from threads.memory.sqlite_fts import install, match_query, transaction
from threads.memory.types import (
    MemoryHit,
    MemoryRecord,
    Outcome,
    ProviderError,
    RecordRef,
    Scope,
)
from threads.result import Err, Ok
from threads.store import SqliteStore

FOREVER_MS: Final = 2**53 - 1
"""The dedup window of a key: the table keeps every key for good."""
_DDL: Final = """
CREATE TABLE IF NOT EXISTS local_memory (
  id TEXT NOT NULL UNIQUE, key TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
  text TEXT NOT NULL, origin TEXT NOT NULL, provenance TEXT NOT NULL,
  namespace TEXT NOT NULL, record_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL, agent TEXT NOT NULL, scope TEXT NOT NULL,
  forgotten INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE VIRTUAL TABLE IF NOT EXISTS local_memory_fts USING fts5(text);
"""
_RECALL: Final = """
SELECT m.id, m.text, m.origin, m.namespace, m.record_id, bm25(local_memory_fts)
FROM local_memory_fts JOIN local_memory m ON m.rowid = local_memory_fts.rowid
WHERE local_memory_fts MATCH ? AND m.tenant_id = ? AND m.agent = ? AND m.scope = ?
  AND m.forgotten = 0
ORDER BY bm25(local_memory_fts) LIMIT ?
"""
_UNBOUND: Final = Err(ProviderError("unavailable", "local_memory isn't bound to a store"))


def _where(scope: Scope) -> tuple[str, str, str]:
    return (scope.tenant_id, scope.agent, scope.scope)


@dataclass(frozen=True, slots=True)
class LocalMemory:
    """spec/api.json `MemoryProvider`; the framework binds it to the run's store at setup."""

    store: SqliteStore | None = None

    @property
    def write_effect(self) -> EffectClass:
        return "idempotent"

    @property
    def dedup_window_ms(self) -> int:
        return FOREVER_MS

    async def bind(self, store: SqliteStore) -> "LocalMemory":
        """This provider on `store`, its tables created. No FTS5: ConfigError."""
        await store.run(install(_DDL))
        return replace(self, store=store)

    async def remember(self, scope: Scope, record: MemoryRecord, key: str) -> Outcome[RecordRef]:
        if self.store is None:
            return _UNBOUND
        digest = hashlib.sha256(record.model_dump_json().encode("utf-8")).hexdigest()
        ref = RecordRef(id="mem_" + hashlib.sha256(key.encode()).hexdigest()[:24], version="1")

        def write(conn: sqlite3.Connection) -> Outcome[RecordRef]:
            row: tuple[str] | None = conn.execute(
                "SELECT digest FROM local_memory WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                same = row[0] == digest
                return Ok(ref) if same else Err(ProviderError("invalid", f"key {key} reused"))
            b = record.binding
            cursor = conn.execute(
                "INSERT INTO local_memory (id, key, digest, text, origin, provenance, namespace,"
                " record_id, tenant_id, agent, scope) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ref.id,
                    key,
                    digest,
                    record.text,
                    record.origin,
                    record.provenance.model_dump_json(),
                    b.namespace,
                    b.record_id,
                    *_where(scope),
                ),
            )
            conn.execute(
                "INSERT INTO local_memory_fts (rowid, text) VALUES (?, ?)",
                (cursor.lastrowid, record.text),
            )
            return Ok(ref)

        return await self.store.run(transaction(write))

    async def recall(self, scope: Scope, query: str, *, k: int = 5) -> Outcome[Sequence[MemoryHit]]:
        if self.store is None:
            return _UNBOUND
        match = match_query(query)
        if match is None:
            return Ok(())

        def read(conn: sqlite3.Connection) -> list[tuple[str, str, str, str, str, float]]:
            return conn.execute(_RECALL, (match, *_where(scope), k)).fetchall()

        rows = await self.store.run(read)
        return Ok(tuple(_hit(row) for row in rows))

    async def forget(self, scope: Scope, id: str, key: str) -> Outcome[None]:
        if self.store is None:
            return _UNBOUND

        def write(conn: sqlite3.Connection) -> Outcome[None]:
            row: tuple[int, int] | None = conn.execute(
                "SELECT rowid, forgotten FROM local_memory"
                " WHERE id = ? AND tenant_id = ? AND agent = ? AND scope = ?",
                (id, *_where(scope)),
            ).fetchone()
            if row is None:
                return Err(ProviderError("invalid", f"no memory {id} in this scope"))
            if row[1] == 0:  # forgetting twice is a no-op
                conn.execute("UPDATE local_memory SET forgotten = 1 WHERE rowid = ?", (row[0],))
                conn.execute("DELETE FROM local_memory_fts WHERE rowid = ?", (row[0],))
            return Ok(None)

        return await self.store.run(transaction(write))


def _hit(row: tuple[str, str, str, str, str, float]) -> MemoryHit:
    id, text, origin, namespace, record_id, bm25 = row
    # Rows this provider wrote from parsed records; parsing again keeps the one boundary rule.
    return MemoryHit.model_validate_json(
        json.dumps(
            {
                "id": id,
                "version": "1",
                "text": text,
                "score": -bm25,
                "origin": origin,
                "binding": {"namespace": namespace, "record_id": record_id},
            }
        )
    )


def local_memory() -> LocalMemory:
    """spec/api.json `local_memory`. Pure: the tables are made when a run binds it."""
    return LocalMemory()
