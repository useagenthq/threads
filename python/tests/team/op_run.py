"""One op vector's op under its writer, at the writer's clock, through this runtime's own store ops:
shared by the vector suite and the two-process races (the same as TypeScript's
test/team/op-run.ts)."""

import json
from collections.abc import Callable, Sequence

from pydantic import JsonValue
from team.vectors import Obj, agents, obj, vector_mint

from threads.agents.outcome import output_text
from threads.log import MemberIdleEvent, ToolCallEvent, ToolResultEvent
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store import Draft, Writer
from threads.store.writer import DecideTx, Refusal
from threads.team.ask import AskPlan, ask, reply
from threads.team.batch import Batch
from threads.team.call import CallContext
from threads.team.close import reader_of
from threads.team.consume import ConsumeContext, consume
from threads.team.deadline import deadline
from threads.team.dynamic import InvalidDefinition, Resolved, Template, resolve_definition
from threads.team.ops import StartPlan, TeamLimits, send, start
from threads.team.provenance import turn_provenance
from threads.team.settle import Completed, SettleContext, Settlement, settle
from threads.team.watch import monitor, wait


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("the vectors carry no body above the inline cap")


def _thread(w: Writer) -> str:
    thread = w.fold.thread_id
    assert thread is not None
    return thread


async def _decided(w: Writer, decide: Callable[[DecideTx, Batch], None]) -> None:
    def run(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, vector_mint)
        decide(tx, batch)
        return batch.drafts

    done = await w.append_decided(run)
    assert isinstance(done, Ok), done


def _limits(v: Obj) -> TeamLimits:
    given = obj(v["given"])
    mailbox, concurrent = given.get("mailbox", 100), given.get("concurrent", 4)
    assert isinstance(mailbox, int)
    assert isinstance(concurrent, int)
    return TeamLimits(concurrent, mailbox)


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


def _model_op(ctx: CallContext, v: Obj) -> JsonValue:
    inp = obj(v["input"])
    args = obj(inp["args"])
    headroom = obj(v["given"]).get("headroom", True) is True
    match v["op"]:
        case "send":
            return send(ctx, str(args["to"]), str(args["text"]), _limits(v))
        case "start":
            listed, resolved = _listed(obj(v["given"]), inp)
            thread = str(inp["thread_id"])
            plan = StartPlan(listed, _limits(v), lambda _a: headroom, thread, resolved)
            return start(ctx, str(args["agent"]), str(args["task"]), plan)
        case "ask":
            plan_ = AskPlan(_limits(v), lambda _row: headroom)
            return ask(ctx, str(args["to"]), str(args["question"]), plan_)
        case "reply":
            return reply(ctx, str(args["ask_id"]), str(args["text"]))
        case "monitor":
            return monitor(ctx, str(args["member"]))
        case _:
            members = args["members"]
            assert isinstance(members, list)
            return wait(ctx, [str(m) for m in members])


async def _call(w: Writer, v: Obj) -> JsonValue:
    inp = obj(v["input"])
    call = next(
        e
        for e in w.fold.events
        if isinstance(e, ToolCallEvent) and e.data.call_id == inp["call_id"]
    )
    out: list[JsonValue] = []

    def decide(tx: DecideTx, batch: Batch) -> None:
        ctx = CallContext(tx.conn, tx.fold, batch, call, _no_text, reader_of(tx.read))
        out.append(_model_op(ctx, v))

    await _decided(w, decide)
    if v["op"] in ("send", "start"):
        result = next(e for e in reversed(w.fold.events) if isinstance(e, ToolResultEvent))
        return json.loads(result.data.preview)
    return out[0]


def _context(w: Writer, tx: DecideTx, batch: Batch) -> ConsumeContext:
    return ConsumeContext(tx.conn, batch, _thread(w), w.branch_id, tx.fold, reader_of(tx.read))


async def _consume(w: Writer) -> JsonValue:
    out: list[JsonValue] = []

    def decide(tx: DecideTx, batch: Batch) -> None:
        got = consume(_context(w, tx, batch))
        if got.status == "nothing_pending":
            out.append({"status": got.status})
        else:
            out.append({"status": got.status, "mail_ids": list(got.mail_ids)})

    await _decided(w, decide)
    return out[0]


async def _deadline(w: Writer, v: Obj) -> JsonValue:
    out: list[JsonValue] = []
    ident = str(obj(v["input"])["id"])

    def decide(tx: DecideTx, batch: Batch) -> None:
        out.append(deadline(_context(w, tx, batch), ident))

    await _decided(w, decide)
    return out[0]


async def _settle(w: Writer, v: Obj) -> JsonValue:
    inp = obj(v["input"])
    idle = v["op"] == "idle"
    how: Settlement = Completed(output_text(w.fold.events)) if idle else obj(inp["result"])

    def decide(tx: DecideTx, batch: Batch) -> None:
        batch.add(Draft("turn_completed", {"reason": inp.get("reason", "end_turn")}))
        provenance = turn_provenance(tx.conn, tx.fold.events)
        ctx = SettleContext(tx.conn, batch, _thread(w), w.branch_id, provenance, _no_text)
        settle(ctx, how)

    await _decided(w, decide)
    if not idle:
        return {"status": "ended"}
    settled = next(e for e in reversed(w.fold.events) if isinstance(e, MemberIdleEvent))
    return {"status": "idle", "result": to_json(settled.data.result)}


async def run_on(w: Writer, v: Obj) -> JsonValue:
    """The op under `w`: its outcome as the vector states it."""
    match v["op"]:
        case "send" | "start" | "ask" | "reply" | "wait" | "monitor":
            return await _call(w, v)
        case "consume":
            return await _consume(w)
        case "deadline":
            return await _deadline(w, v)
        case "idle" | "end":
            return await _settle(w, v)
        case op:
            raise AssertionError(f"op {op} runs on a store, not a writer")
