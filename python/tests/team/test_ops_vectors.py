"""The op vectors this build runs (spec/conformance/vectors/team-ops.json): each op on its world
through this runtime's own store ops reaches the reference's outcome, appends the same event types
per log and makes the same row changes; the team then replays. The same selection as TypeScript's
test/team/ops.test.ts."""

import asyncio
import json
from collections.abc import Callable, Sequence

import pytest
from pydantic import JsonValue
from team.team_kit import assert_team_replays
from team.vectors import (
    TEAM,
    Obj,
    agents,
    changes,
    obj,
    rows,
    seeded,
    vector_mint,
    vectors,
    world_logs,
)

from threads.log import (
    BranchId,
    Event,
    MailEnvelope,
    MemberIdleEvent,
    MemberStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from threads.result import Err, Ok
from threads.store import Draft, SqliteStore, Writer
from threads.store.writer import DecideTx, Refusal
from threads.team.batch import Batch
from threads.team.call import CallContext
from threads.team.consume import ConsumeContext, consume
from threads.team.dynamic import InvalidDefinition, Resolved, Template, resolve_definition
from threads.team.materialize import MaterializeOptions, Rebind, materialize
from threads.team.ops import StartPlan, TeamLimits, send, start
from threads.team.provenance import turn_provenance
from threads.team.settle import Completed, SettleContext, Settlement, settle

LATER = frozenset({"ask", "reply", "wait", "monitor", "cancel", "deadline"})
"""Ops other lanes build: asks, waits, monitors and cancels (21E)."""
CONTROL_21E = frozenset(
    {
        "ask-reply-closes-ask",
        "ask-bounce-closes-member-ended",
        "cancel-applied-running-member",
        "cancel-applied-parked-asker",
        "cancel-applied-asker-with-pending-reply",
        "cancel-applied-waiter",
        "cancel-applied-waiter-counts-committed-settlement-cancel-first",
        "cancel-applied-waiter-counts-committed-settlement-settlement-first",
        "cancel-stops-new-work",
    }
)
"""The control mail only lane 21E consumes."""
MINE = [
    v
    for v in vectors()
    if v["by"] != "team" and v["op"] not in LATER and v["name"] not in CONTROL_21E
]


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("the vectors carry no body above the inline cap")


def _clock(v: Obj) -> Callable[[], int]:
    now = v["now"]
    assert isinstance(now, int)
    return lambda: now


async def _writer(store: SqliteStore, v: Obj) -> Writer:
    branch = BranchId(str(world_logs(v)[str(v["by"])]["branch_id"]))
    got = await store.acquire(branch, "vectors", _clock(v))
    assert isinstance(got, Ok), got
    return got.value


async def _decided(w: Writer, decide: Callable[[DecideTx, Batch], None]) -> None:
    def run(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, vector_mint)
        decide(tx, batch)
        return batch.drafts

    done = await w.append_decided(run)
    assert isinstance(done, Ok), done


def _thread(w: Writer) -> str:
    thread = w.fold.thread_id
    assert thread is not None
    return thread


async def _call(store: SqliteStore, v: Obj) -> JsonValue:
    w = await _writer(store, v)
    inp = obj(v["input"])
    args = obj(inp["args"])
    call = next(
        e
        for e in w.fold.events
        if isinstance(e, ToolCallEvent) and e.data.call_id == inp["call_id"]
    )
    given = obj(v["given"])
    mailbox, concurrent = given.get("mailbox", 100), given.get("concurrent", 4)
    assert isinstance(mailbox, int)
    assert isinstance(concurrent, int)
    limits = TeamLimits(concurrent, mailbox)

    def decide(tx: DecideTx, batch: Batch) -> None:
        ctx = CallContext(tx.conn, tx.fold.events, batch, call, _no_text)
        if v["op"] == "send":
            send(ctx, str(args["to"]), str(args["text"]), limits)
        else:
            headroom = given.get("headroom", True) is True
            listed, resolved = _listed(given, inp)
            plan = StartPlan(listed, limits, lambda _a: headroom, str(inp["thread_id"]), resolved)
            start(ctx, str(args["agent"]), str(args["task"]), plan)

    await _decided(w, decide)
    result = next(e for e in reversed(w.fold.events) if isinstance(e, ToolResultEvent))
    return json.loads(result.data.preview)


def _listed(given: Obj, inp: Obj) -> tuple[dict[str, str], Resolved | InvalidDefinition]:
    """The agents the team lists with their pins' hashes (a dynamic start's from the vector),
    and the start's fields resolved against its agent."""
    args, listed = obj(inp["args"]), agents()
    agent, raw = str(args["agent"]), obj(given.get("templates", {})).get(str(args["agent"]))
    template = None
    if raw is not None:
        t = obj(raw)
        template = Template(tuple(_strs(t["tools"])), tuple(_strs(t["models"])))
        listed[agent] = str(inp["config_hash"])
    tools = args.get("tools")
    got = resolve_definition(
        template,
        label=_opt(args, "label"),
        instructions=_opt(args, "instructions"),
        tools=None if tools is None else tuple(_strs(tools)),
        model=_opt(args, "model"),
    )
    return listed, got.value if isinstance(got, Ok) else got.error


def _strs(value: JsonValue) -> list[str]:
    assert isinstance(value, list)
    return [str(x) for x in value]


def _opt(args: Obj, key: str) -> str | None:
    value = args.get(key)
    return None if value is None else str(value)


async def _consume(store: SqliteStore, v: Obj) -> JsonValue:
    w = await _writer(store, v)
    out: list[JsonValue] = []

    def decide(tx: DecideTx, batch: Batch) -> None:
        got = consume(ConsumeContext(tx.conn, batch, _thread(w), w.branch_id, tx.fold))
        if got.status == "nothing_pending":
            out.append({"status": got.status})
        else:
            out.append({"status": got.status, "mail_ids": list(got.mail_ids)})

    await _decided(w, decide)
    return out[0]


def _last_text(events: Sequence[Event]) -> str:
    from threads.agents.outcome import output_text  # noqa: PLC0415 - test-local

    return output_text(events)


async def _settle(store: SqliteStore, v: Obj) -> JsonValue:
    w = await _writer(store, v)
    inp = obj(v["input"])
    idle = v["op"] == "idle"
    how: Settlement = Completed(_last_text(w.fold.events)) if idle else obj(inp["result"])

    def decide(tx: DecideTx, batch: Batch) -> None:
        batch.add(Draft("turn_completed", {"reason": inp.get("reason", "end_turn")}))
        provenance = turn_provenance(tx.conn, tx.fold.events)
        ctx = SettleContext(tx.conn, batch, _thread(w), w.branch_id, provenance, _no_text)
        settle(ctx, how)

    await _decided(w, decide)
    if not idle:
        return {"status": "ended"}
    settled = next(e for e in reversed(w.fold.events) if isinstance(e, MemberIdleEvent))
    from threads.reduce.handlers import to_json  # noqa: PLC0415 - test-local

    return {"status": "idle", "result": to_json(settled.data.result)}


async def _materialize(store: SqliteStore, v: Obj) -> JsonValue:
    inp = obj(v["input"])
    status = inp["rebind"]
    assert status in ("ok", "pin_unavailable", "pin_mismatch")

    async def rebind(_started: MemberStartedEvent, _task: MailEnvelope) -> Rebind:
        return Rebind(status)

    o = MaterializeOptions(
        rebind, "vectors", 30_000, _clock(v), vector_mint, BranchId(str(inp["branch_id"]))
    )
    got = await materialize(store, TEAM, str(inp["member"]), o)
    assert isinstance(got, Ok), got
    m = got.value
    if m.status == "rebind_failed":
        return {"status": m.status, "code": m.code}
    return {"status": m.status}


async def _run(store: SqliteStore, v: Obj) -> JsonValue:
    match v["op"]:
        case "send" | "start":
            return await _call(store, v)
        case "consume":
            return await _consume(store, v)
        case "idle" | "end":
            return await _settle(store, v)
        case _:
            return await _materialize(store, v)


async def _appended(store: SqliteStore, v: Obj, heads: dict[str, int]) -> Obj:
    labels = {label: str(log["branch_id"]) for label, log in world_logs(v).items()}
    inp = obj(v["input"])
    if v["op"] == "materialize":
        labels[str(inp["label"])] = str(inp["branch_id"])
    out: Obj = {}
    for label, branch in labels.items():
        read = await store.read(BranchId(branch), 0)
        if isinstance(read, Err):
            continue
        types: list[JsonValue] = [
            e.type for e in read.value.fold.events if e.seq > heads.get(label, 0)
        ]
        if types:
            out[label] = types
    return out


def test_the_selection_covers_this_builds_ops() -> None:
    assert {str(v["op"]) for v in MINE} == {
        "start",
        "send",
        "consume",
        "materialize",
        "idle",
        "end",
    }
    # Pinned: a vector that drops out of the selection fails here, not silently.
    assert len(MINE) == 44  # noqa: PLR2004 - the pinned selection size


@pytest.mark.parametrize("v", MINE, ids=[str(v["name"]) for v in MINE])
def test_vector(v: Obj) -> None:
    async def main() -> None:
        store = await seeded(v)
        try:
            before = await store.run(rows)
            heads: dict[str, int] = {}
            for label, log in world_logs(v).items():
                read = await store.read(BranchId(str(log["branch_id"])), 0)
                assert isinstance(read, Ok)
                heads[label] = read.value.fold.seq
            expect = obj(v["expect"])
            assert await _run(store, v) == expect["outcome"]
            assert await _appended(store, v, heads) == expect["appended"]
            assert changes(before, await store.run(rows)) == expect["rows"]
            await assert_team_replays(store, TEAM)
        finally:
            await store.close()

    asyncio.run(main())
