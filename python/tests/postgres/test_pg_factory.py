"""postgres(): lazy, a URL or DATABASE_URL, the placeholder rewrite's shared vectors, and what it
refuses (local memory and knowledge, a missing URL)."""

import asyncio
import json
import traceback
from pathlib import Path

import pytest
from pg_kit import Leg, need_postgres

from threads.agents.config import ConfigError
from threads.agents.store import open_store
from threads.memory.local_knowledge import local_knowledge
from threads.memory.local_memory import local_memory
from threads.postgres import postgres
from threads.postgres.placeholders import psycopg
from threads.result import Ok
from threads.store import StoreError
from threads.store.conn import RETRY_BUDGET_S, pause_s

VECTORS = Path(__file__).resolve().parents[3] / "spec/conformance/vectors/sql-placeholders.json"
SQLITE_BUSY_TIMEOUT_S = 5.0
MANY_ATTEMPTS = 20
CASES = json.loads(VECTORS.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_placeholder_rewrite_passes_the_shared_vectors(case: dict[str, str]) -> None:
    assert psycopg(case["sql"]) == case["psycopg"]


def test_postgres_is_lazy_and_without_a_url_is_invalid_config_at_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = postgres()  # nothing connects yet
    with pytest.raises(ConfigError) as refused:
        asyncio.run(open_store(store))
    assert refused.value.code == "invalid_config"
    assert "DATABASE_URL" in str(refused.value)


def test_url_defaults_to_database_url_without_a_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL nobody listens on is an outage at first use, never the missing-URL setup error:
    postgres() tried the variable's server."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://threads:threads@127.0.0.1:1/threads")
    with pytest.raises(StoreError):
        asyncio.run(open_store(postgres()))


@pytest.mark.parametrize(
    "dsn",
    [
        "host=127.0.0.1 port=1 user=threads password=SEKRET-kv dbname=threads",
        "host=127.0.0.1 port=1 password='SEKRET-kv quoted' sslmode=bogus",
        "password SEKRET-kv host=127.0.0.1",
        "postgresql://threads:SEKRET-url@127.0.0.1:1/threads",
        "postgresql://threads:SEKRET%2Durl@127.0.0.1:notaport/threads",
        "postgresql://threads:SEKRET-url@127.0.0.1:1/threads?sslmode=bogus",
    ],
)
def test_a_dsns_password_is_in_no_part_of_the_error(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """key=value and URL forms, malformed or unreachable: the error, its causes and its traceback
    never carry the password."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    with pytest.raises((StoreError, ConfigError)) as failed:
        asyncio.run(open_store(postgres()))
    shown = "".join(traceback.format_exception(failed.value))
    chain: list[BaseException] = []
    at: BaseException | None = failed.value
    while at is not None and at not in chain:
        chain.append(at)
        at = at.__cause__ or at.__context__
    assert "SEKRET" not in shown
    assert all("SEKRET" not in repr(e) and "SEKRET" not in str(e) for e in chain)


def test_postgres_reads_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    url = need_postgres()
    leg = Leg(url)
    from pg_kit import schema_url  # noqa: PLC0415

    monkeypatch.setenv("DATABASE_URL", schema_url(url, leg.schema()))

    async def main() -> None:
        sq = await open_store(postgres())
        assert sq.dialect == "postgres"
        await sq.close()

    try:
        asyncio.run(main())
    finally:
        leg.close()


def test_local_memory_and_knowledge_are_refused_on_postgres_naming_the_alternatives() -> None:
    leg = Leg(need_postgres())

    async def main() -> None:
        opened = await leg.open()
        assert isinstance(opened, Ok)
        with pytest.raises(ConfigError) as memory:
            await local_memory().bind(opened.value)
        assert memory.value.code == "invalid_config"
        assert "supermemory() or zep()" in str(memory.value)
        with pytest.raises(ConfigError) as knowledge:
            await local_knowledge(paths=[]).bind(opened.value)
        assert knowledge.value.code == "invalid_config"
        await opened.value.close()

    try:
        asyncio.run(main())
    finally:
        leg.close()


def test_a_conflicts_pause_doubles_from_10_ms_to_at_most_250_ms_within_a_5_s_budget() -> None:
    most = [round(pause_s(n, lambda: 1.0), 3) for n in range(1, 8)]
    assert most == [0.01, 0.02, 0.04, 0.08, 0.16, 0.25, 0.25]
    assert pause_s(9, lambda: 0.0) == 0
    assert RETRY_BUDGET_S == SQLITE_BUSY_TIMEOUT_S
    spent, attempts = 0.0, 0
    while spent < RETRY_BUDGET_S:
        attempts += 1
        spent += pause_s(attempts, lambda: 1.0)
    assert attempts > MANY_ATTEMPTS  # even at the longest pauses
