"""Host-issued memory and knowledge bindings (store.sql `memory_bindings`,
`knowledge_bindings`, `provider_audit`).

The host decides which scope owns an item, never the provider: a binding is issued and recorded
here before the provider sees the write, and a returned item counts only when its binding is a
row for the calling scope. Bindings are derived from the scope and the write key, so a retried
write under the same key carries the same binding.
"""

import hashlib
import sqlite3
from typing import Final, Literal

from threads.store.worker import Worker

type Kind = Literal["memory", "knowledge"]

_SQL: Final[dict[Kind, tuple[str, str]]] = {
    "memory": (
        "INSERT OR IGNORE INTO memory_bindings"
        " (namespace, record_id, tenant_id, agent, scope) VALUES (?, ?, ?, ?, ?)",
        "SELECT 1 FROM memory_bindings WHERE namespace = ? AND record_id = ?"
        " AND tenant_id = ? AND agent = ? AND scope = ?",
    ),
    "knowledge": (
        "INSERT OR IGNORE INTO knowledge_bindings"
        " (namespace, record_id, tenant_id, agent, scope) VALUES (?, ?, ?, ?, ?)",
        "SELECT 1 FROM knowledge_bindings WHERE namespace = ? AND record_id = ?"
        " AND tenant_id = ? AND agent = ? AND scope = ?",
    ),
}


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:32]


class Bindings:
    def __init__(self, worker: Worker, kind: Kind) -> None:
        self._worker = worker
        self._kind: Kind = kind
        self._insert, self._owned = _SQL[kind]

    async def issue(self, tenant_id: str, agent: str, scope: str, key: str) -> tuple[str, str]:
        """(namespace, record_id) for a write under `key`, recorded before it is used."""
        namespace = _digest(tenant_id, agent, scope)
        record_id = _digest(namespace, key)
        row = (namespace, record_id, tenant_id, agent, scope)
        await self._worker.call(lambda conn: conn.execute(self._insert, row))
        return namespace, record_id

    async def owned(
        self, tenant_id: str, agent: str, scope: str, items: tuple[tuple[str, str], ...]
    ) -> frozenset[tuple[str, str]]:
        """Which of the (namespace, record_id) pairs belong to the calling scope."""

        def read(conn: sqlite3.Connection) -> frozenset[tuple[str, str]]:
            return frozenset(
                (namespace, record_id)
                for namespace, record_id in items
                if conn.execute(
                    self._owned, (namespace, record_id, tenant_id, agent, scope)
                ).fetchone()
            )

        return await self._worker.call(read)

    async def violation(self, tenant_id: str, namespace: str, record_id: str, at: int) -> None:
        """Records a dropped item in the host's audit table."""
        row = (tenant_id, self._kind, namespace, record_id, at)
        await self._worker.call(
            lambda conn: conn.execute(
                "INSERT INTO provider_audit (tenant_id, kind, code, namespace, record_id, at)"
                " VALUES (?, ?, 'scope_violation', ?, ?, ?)",
                row,
            )
        )
