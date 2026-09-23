"""`sqlite()` (spec/api.json): the one log and artifact store, opened lazily on first use."""

from dataclasses import dataclass
from pathlib import Path
from weakref import WeakKeyDictionary

from threads.agents.config import ConfigError
from threads.result import Err
from threads.store import SqliteStore


@dataclass(frozen=True, eq=False)
class Store:
    """The SQLite log and artifact store (spec/api.json `Store`). Sealed: no public methods."""

    path: str


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
    memory = store.path == ":memory:"
    if not memory:
        Path(store.path).mkdir(parents=True, exist_ok=True)
    result = await SqliteStore.open(":memory:" if memory else Path(store.path) / "threads.db")
    if isinstance(result, Err):
        raise ConfigError("invalid_config", f"store {store.path}: {result.error.message}")
    _OPENED[store] = result.value
    return result.value
