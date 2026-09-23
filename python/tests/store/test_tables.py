"""The host tables (store.sql): rows bound to appends by companions, the inbox, receipts."""

import asyncio
import sqlite3
from collections.abc import Sequence

from test_writer import DONE, STARTED, T0, THREAD, Clock, user

from threads.log import BranchId, ParseError, ThreadId
from threads.result import Err, Ok
from threads.store import Draft, SqliteStore, StoredEvent, Writer, approvals, inbox, receipts

BRANCH = BranchId("0192b000-0000-7000-8000-0000000000aa")
CHALLENGE = "0192c000-0000-7000-8000-000000000001"


async def _writer(tenant: str = "acme") -> tuple[SqliteStore, Writer]:
    opened = await SqliteStore.open(tenant_id=tenant)
    assert isinstance(opened, Ok)
    sq, clock = opened.value, Clock()
    assert await sq.create(THREAD, BRANCH, T0) == Ok(None)
    taken = await sq.acquire(BRANCH, "h", clock)
    assert isinstance(taken, Ok)
    assert isinstance(await taken.value.append([STARTED]), Ok)
    return sq, taken.value


def _refuse(_conn: sqlite3.Connection, _events: Sequence[StoredEvent]) -> ParseError | None:
    return ParseError("approval_duplicate", "no")


def test_a_refusing_companion_rolls_the_append_back_and_the_writer_goes_on() -> None:
    asyncio.run(_a_refusing_companion_rolls_the_append_back_and_the_writer_goes_on())


async def _a_refusing_companion_rolls_the_append_back_and_the_writer_goes_on() -> None:
    sq, writer = await _writer()
    refused = await writer.append([user("hi")], _refuse)
    assert refused == Err(ParseError("approval_duplicate", "no"))
    assert writer.fold.seq == 1
    assert isinstance(await writer.append([user("hi")]), Ok)
    read = await sq.read(BRANCH, T0)
    assert isinstance(read, Ok)
    assert read.value.fold.seq == writer.fold.seq


def test_an_approval_requested_append_opens_its_single_use_row() -> None:
    asyncio.run(_an_approval_requested_append_opens_its_single_use_row())


async def _an_approval_requested_append_opens_its_single_use_row() -> None:
    sq, writer = await _writer()
    await writer.append([user("go")])
    requested = Draft(
        "approval_requested",
        {"challenge_id": CHALLENGE, "call_id": "c1", "args_hash": "a" * 64, "expires_at": T0 + 9},
    )
    # A challenge needs a pending call; the reducer checks that, so record one first.
    call = Draft(
        "tool_call",
        {"call_id": "c1", "name": "x", "input": {}, "request_event_id": CHALLENGE},
    )
    done = await writer.append([call, requested])
    assert isinstance(done, Ok), done
    found = await sq.tables.challenge(CHALLENGE)
    assert found is not None
    assert (found.state, found.call_id, found.installation_id) == ("open", "c1", None)
    assert await sq.scoped("other").tables.challenge(CHALLENGE) is None
    consume = approvals.consume(CHALLENGE, "granted", "api/acme/alice", T0)
    assert await sq.run(lambda c: consume(c, ())) is None
    assert await sq.run(lambda c: consume(c, ())) == ParseError(
        "approval_duplicate", f"challenge {CHALLENGE} is already answered"
    )


def test_a_receipt_binds_the_key_once() -> None:
    asyncio.run(_a_receipt_binds_the_key_once())


async def _a_receipt_binds_the_key_once() -> None:
    sq, writer = await _writer()
    key = receipts.Key("acme", "startRun", "k1", "api/acme/alice", "b" * 64)
    done = await writer.append([user("go")], receipts.insert(key, T0))
    assert isinstance(done, Ok)
    found = await sq.tables.receipt(key)
    assert found is not None
    assert found.run_id == done.value[0].event_id
    assert isinstance(await writer.append([DONE]), Ok)
    again = await writer.append([user("go")], receipts.insert(key, T0))
    assert isinstance(again, Err)
    assert again.error.code == "idempotency_key_reused"


def test_a_redelivered_batch_inserts_nothing_and_maps_one_thread() -> None:
    asyncio.run(_a_redelivered_batch_inserts_nothing_and_maps_one_thread())


async def _a_redelivered_batch_inserts_nothing_and_maps_one_thread() -> None:
    sq, _ = await _writer()
    items = [
        inbox.Item("slack", "T1", f"Ev1#{i}", "Ev1", "C1", b'{"kind":"ignore"}') for i in (0, 1)
    ]
    fresh = iter([ThreadId(f"0192a000-0000-7000-8000-00000000000{i}") for i in range(2, 9)])
    first = await sq.tables.intake(items, T0, lambda: next(fresh))
    second = await sq.tables.intake(items, T0, lambda: next(fresh))
    assert first == second
    rows = await sq.tables.inbox_rows()
    assert [r.item_key for r in rows] == ["Ev1#0", "Ev1#1"]
    (thread,) = first
    assert await sq.tables.pending(thread) == rows
    conversation = await sq.tables.conversation(thread)
    assert conversation == inbox.Conversation("slack", "T1", "C1")
    assert await sq.scoped("other").tables.inbox_rows() == ()
