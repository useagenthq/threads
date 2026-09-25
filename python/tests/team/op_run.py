"""One op vector's op under its writer, at the writer's clock, through this runtime's own store ops:
shared by the vector suite and the two-process races (the same as TypeScript's
test/team/op-run.ts)."""

import json
from collections.abc import Callable, Sequence

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING
from team.vectors import Obj, agents, obj, vector_mint

from threads.agents.outcome import output_text
from threads.log import MemberIdleEvent, MemberRef, Principal, ToolCallEvent, ToolResultEvent
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store import Draft, Writer
from threads.store.writer import DecideTx, Refusal
from threads.team.ask import AskPlan, ask, open_ask, reply
from threads.team.batch import Batch
from threads.team.call import CallContext, call_request, named
from threads.team.cancel import cancel, request_cancel
from threads.team.close import reader_of
from threads.team.constants import TEAM_CONSTANTS
from threads.team.consume import ConsumeContext, consume
from threads.team.deadline import deadline
from threads.team.dynamic import InvalidDefinition, Resolved, Template, resolve_definition
from threads.team.operator import (
    OperatorContext,
    OperatorInput,
    OperatorOp,
    Replayed,
    open_operator,
    ref_target,
)
from threads.team.ops import StartPlan, TeamLimits, send, start
from threads.team.provenance import turn_provenance
from threads.team.request import Request
from threads.team.rows import TeamRow, team_of_log
from threads.team.settle import Completed, SettleContext, Settlement, settle
from threads.team.watch import WaitMode, monitor, open_wait, wait, wait_members


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


def _listed(given: Obj, inp: Obj, args: Obj) -> tuple[dict[str, str], Resolved | InvalidDefinition]:
    """The agents the team lists with their pins' hashes (a dynamic start's from the vector),
    and the start's fields resolved against its agent."""
    listed = agents()
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
            to = named(ctx, str(args["to"]))
            return send(call_request(ctx), to, str(args["text"]), _limits(v))
        case "start":
            return start(call_request(ctx), str(args["agent"]), str(args["task"]), _plan(v, args))
        case "ask":
            plan_ = AskPlan(_limits(v), lambda _row: headroom)
            return ask(ctx, str(args["to"]), str(args["question"]), plan_)
        case "reply":
            return reply(ctx, str(args["ask_id"]), str(args["text"]))
        case "monitor" | "cancel":
            op = monitor if v["op"] == "monitor" else cancel
            return op(ctx, str(args["member"]))
        case _:
            members = args["members"]
            assert isinstance(members, list)
            return wait(ctx, [str(m) for m in members])


def _plan(v: Obj, args: Obj) -> StartPlan:
    inp, given = obj(v["input"]), obj(v["given"])
    headroom = given.get("headroom", True) is True
    listed, resolved = _listed(given, inp, args)
    return StartPlan(listed, _limits(v), lambda _a: headroom, str(inp["thread_id"]), resolved)


_OPS: dict[str, OperatorOp] = {
    "start": "start",
    "send": "send",
    "ask": "ask",
    "wait": "wait",
    "cancel": "cancel",
}


def _refs(raw: JsonValue) -> list[MemberRef]:
    assert isinstance(raw, list)
    return [MemberRef.model_validate(m) for m in raw]


async def _operator(w: Writer, v: Obj) -> JsonValue:
    """An operator request's outcome: what the op returns, or what its key replays. A wait's
    mode above its member count is refused before the writer, as the handle does."""
    inp = obj(v["input"])
    body = obj(inp["body"])
    op = _OPS[str(v["op"])]
    if op == "wait" and wait_members(_refs(body["members"]), _mode(body)) == "invalid_request":
        return {"code": "invalid_request", "status": "refused"}
    key = inp.get("idempotency_key")
    principal = Principal.model_validate(inp["principal"])
    request = OperatorInput(
        str(inp["request_id"]), op, principal, body, None if key is None else str(key)
    )
    out: list[JsonValue] = []

    def decide(tx: DecideTx, batch: Batch) -> None:
        team = team_of_log(tx.conn, w.branch_id)
        assert team is not None
        opened = open_operator(
            OperatorContext(tx.conn, tx.fold.events, batch, _no_text, team), request
        )
        if isinstance(opened, Replayed):
            out.append(opened.outcome)
        else:
            out.append(_operator_op(_context(w, tx, batch), opened.request, team, v))

    await _decided(w, decide)
    return out[0]


def _mode(body: Obj) -> WaitMode | None:
    mode = body.get("mode")
    if mode is None or isinstance(mode, int):
        return mode
    assert mode in ("all", "any")
    return "all" if mode == "all" else "any"


def _operator_op(ctx: ConsumeContext, req: Request, team: TeamRow, v: Obj) -> JsonValue:
    body = obj(obj(v["input"])["body"])
    conn = ctx.conn
    match v["op"]:
        case "send":
            to = ref_target(conn, team, MemberRef.model_validate(body["to"]))
            return send(req, to, str(body["text"]), _limits(v))
        case "ask":
            headroom = obj(v["given"]).get("headroom", True) is True
            timeout = body.get("timeout_ms", TEAM_CONSTANTS.ask_wait_default_ms)
            assert isinstance(timeout, int)
            plan = AskPlan(_limits(v), lambda _row: headroom, timeout)
            to = ref_target(conn, team, MemberRef.model_validate(body["to"]))
            return open_ask(req, to, str(body["question"]), plan)
        case "wait":
            members = wait_members(_refs(body["members"]), _mode(body))
            assert members != "invalid_request"
            targets = [ref_target(conn, team, m) for m in members]
            timeout = body.get("timeout_ms", TEAM_CONSTANTS.ask_wait_default_ms)
            assert isinstance(timeout, int)
            return open_wait(req, ctx, targets, _mode(body) or "all", timeout)
        case "cancel":
            member = MemberRef.model_validate(body["member"])
            return request_cancel(req, ref_target(conn, team, member))
        case _:
            return start(req, str(body["agent"]), str(body["task"]), _plan(v, body))


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
    result = settled.data.result
    assert result is not MISSING, "a Phase 1 member_idle has a result"
    return {"status": "idle", "result": to_json(result)}


async def run_on(w: Writer, v: Obj) -> JsonValue:
    """The op under `w`: its outcome as the vector states it."""
    match v["op"]:
        case "send" | "start" | "ask" | "wait" | "cancel" if "request_id" in obj(v["input"]):
            return await _operator(w, v)
        case "send" | "start" | "ask" | "reply" | "wait" | "monitor" | "cancel":
            return await _call(w, v)
        case "consume":
            return await _consume(w)
        case "deadline":
            return await _deadline(w, v)
        case "idle" | "end":
            return await _settle(w, v)
        case op:
            raise AssertionError(f"op {op} runs on a store, not a writer")
