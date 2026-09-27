"""One wire name for a run receipt's operation (store.sql `run_receipts`).

A receipt is written by whichever language served `POST /v1/runs` and read by whichever serves the
retry, so `operation` has to be the same bytes in both: with a spelling of its own, a host reads
none of the other's receipts, the Idempotency-Key looks unused, and the run starts twice. Asserted
from the store, not from the answer: the row this host wrote, and, for a row written as the other
language writes it, that the retry appends no second `user_input`.

The TypeScript twin is packages/host/test/runs.test.ts, "a receipt row written by the other
language". Both spell the name out, so neither can drift from store.sql on its own.
"""

import asyncio
from http import HTTPStatus

from host.test_http import served, start, text
from pydantic import JsonValue

from threadsai import agent, scripted_model, sqlite
from threadsai.agents.store import open_store, scoped
from threadsai.log import UserInputEvent
from threadsai.result import Ok
from threadsai.store.conn import Conn, Row
from threadsai.store.sql import text_of

START_RUN = "start_run"
"""The `run_receipts` operation of a run started through POST /v1/runs, as both languages write
and read it."""

BODY: JsonValue = {"agent": "support", "input": "hi"}
NOW = 1_790_000_000_000


def _rows(conn: Conn) -> list[Row]:
    return conn.execute(
        "SELECT operation, principal_key, body_hash, thread_id, branch_id, run_id"
        " FROM run_receipts ORDER BY idempotency_key"
    ).fetchall()


def _seed(conn: Conn, row: Row, key: str) -> None:
    """The receipt of the same run under another key, written as the other language writes it:
    the operation is the wire name spelled out, never this language's own constant."""
    conn.execute(
        "INSERT INTO run_receipts (tenant_id, idempotency_key, created_at, operation,"
        " principal_key, body_hash, thread_id, branch_id, run_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("acme", key, NOW, START_RUN, *row[1:]),
    )


def test_a_receipt_written_by_the_other_language_starts_no_second_run() -> None:
    asyncio.run(_honoured())


async def _honoured() -> None:
    store = sqlite(":memory:")
    bot = agent(model=scripted_model({"responses": [text("Hi.")]}))
    async with served(bot, store=store) as client:
        first = await start(client, "alice", "k-1", BODY)
        assert first.status_code == HTTPStatus.ACCEPTED
        accepted = first.json()
        sq = await open_store(scoped(store, "acme"))
        (written,) = await sq.run(_rows)
        # What this host stored is the wire name, so the other language's reader finds it.
        assert text_of(written[0]) == START_RUN
        await sq.run(lambda c: _seed(c, written, "k-2"))
        replayed = await start(client, "alice", "k-2", BODY)
        assert replayed.status_code == HTTPStatus.ACCEPTED
        assert replayed.json() == accepted
        # The store, not the answer: two receipts of one run, and one input on its branch.
        rows = await sq.run(_rows)
        assert [text_of(r[5]) for r in rows] == [accepted["run_id"], accepted["run_id"]]
        read = await sq.read(accepted["branch_id"], 0)
        assert isinstance(read, Ok)
        inputs = [e for e in read.value.fold.events if isinstance(e, UserInputEvent)]
        assert len(inputs) == 1
