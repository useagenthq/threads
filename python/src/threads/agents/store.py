"""`sqlite()` (spec/api.json): the one log and artifact store, opened lazily on first use."""

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from weakref import WeakKeyDictionary

from threads.agents.config import ConfigError
from threads.log import BranchId
from threads.result import Err
from threads.store import LOCAL_TENANT, SqliteStore, Writer


@dataclass(frozen=True, eq=False)
class Store:
    """The SQLite log and artifact store (spec/api.json `Store`). Sealed: no public methods."""

    path: str
    tenant: str = field(default=LOCAL_TENANT, kw_only=True)
    """Every read and write is scoped to this tenant; `scoped` makes one."""
    root: "Store | None" = field(default=None, kw_only=True, repr=False)
    """The store this one scopes: they share one database handle."""


def scoped(store: Store, tenant: str) -> Store:
    """The same database, scoped to another tenant: what the host serves a principal with."""
    root = store.root or store
    return Store(root.path, tenant=tenant, root=root)


LIVE: Final[dict[BranchId, Writer]] = {}
"""The writers of runs in flight in this process, by branch. A thread control (cancel,
approve, ...) appends through the run's own writer instead of competing for its lease."""


HOLDER: Final = uuid.uuid4().hex
"""This process's fork holder id, which finds the forks a crash interrupted.
A finished fork hands the child's lease back; each run takes the branch as its own holder."""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


_OPENED: WeakKeyDictionary[Store, SqliteStore] = WeakKeyDictionary()


def sqlite(path: str) -> Store:
    """spec/api.json `sqlite`. ":memory:" is a throwaway store (tests); any other path is a
    directory holding `threads.db` and `artifacts/`. No I/O until first use."""
    return Store(path)


async def open_store(store: Store) -> SqliteStore:
    """The store's SQLite handle, opened once. A store this version can't read (a newer schema)
    is a setup error."""
    opened = _OPENED.get(store)
    if opened is not None:
        return opened
    if store.root is not None:
        scoped_store = (await open_store(store.root)).scoped(store.tenant)
        _OPENED[store] = scoped_store
        return scoped_store
    memory = store.path == ":memory:"
    if not memory:
        Path(store.path).mkdir(parents=True, exist_ok=True)
    at = ":memory:" if memory else Path(store.path) / "threads.db"
    result = await SqliteStore.open(at, tenant_id=store.tenant)
    if isinstance(result, Err):
        raise ConfigError("invalid_config", f"store {store.path}: {result.error.message}")
    _OPENED[store] = result.value
    return result.value
