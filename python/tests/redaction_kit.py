"""Shared by the secret-redaction tests: a store, a writer on it, event drafts, and a race
that registers a value when the store's thread is handed a statement."""

import sqlite3
from collections.abc import Callable

import pytest
from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.redaction import register
from threads.result import Ok
from threads.store import Draft, SqliteStore, Writer
from threads.store.worker import Worker

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000
ALICE: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "acme", "subject": "alice"},
}


def started(settings: dict[str, JsonValue]) -> Draft:
    return Draft(
        "thread_started",
        {
            "agent_name": "demo",
            "config_hash": "0" * 64,
            "instructions": "You are a helpful agent.",
            "model": {"provider": "scripted", "name": "scripted-1"},
            "model_params": {"max_tokens": 1024},
            "adapter": {"name": "scripted", "version": "1", "settings": settings},
            "tools": [],
        },
    )


def user(text: str) -> Draft:
    return Draft("user_input", {"source": "api", "text": text}, actor=ALICE)


async def opened() -> SqliteStore:
    store = await SqliteStore.open(":memory:")
    assert isinstance(store, Ok)
    return store.value


async def writer(store: SqliteStore) -> Writer:
    assert await store.create(THREAD, ROOT, T0) == Ok(None)
    acquired = await store.acquire(ROOT, "holder", lambda: T0)
    assert isinstance(acquired, Ok)
    return acquired.value


def escaping_marker() -> None:
    """Redacting FIRSTVALUE gives `[secret X"Y]`; canonical JSON escapes its quote into the
    second value."""
    register("FIRSTVALUE", 'X"Y')
    register('[secret X\\"Y]', "Z")


class Race:
    """Registers `value` when the store's thread is handed its `at`-th statement from now."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, value: str, at: int = 1) -> None:
        self.left = at
        original = Worker.call

        async def call[T](worker: Worker, statement: Callable[[sqlite3.Connection], T]) -> T:
            self.left -= 1
            if self.left == 0:
                register(value, "raced")
            return await original(worker, statement)

        monkeypatch.setattr(Worker, "call", call)


RACED = "raced-abcdefgh"
