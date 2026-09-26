"""The team tools (spec/schema/README.md, "Teams", Model tools), run by the loop like any framework
tool: each is one decided append under the caller's writer, holding the policy decision, the op's
events and the call's one result, so a call re-dispatched after a crash either finds that append
or makes it now, never twice. An ask or a wait stays a pending call: its re-dispatch only parks,
and the writer's close records its one result."""

from collections.abc import Callable, Sequence

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import (
    AskInput,
    CancelInput,
    MonitorInput,
    ReplyInput,
    SendInput,
    StartInput,
    WaitInput,
)
from threads.agents.start_pin import Chosen, start_pin
from threads.log import ThreadStartedEvent, ToolCallEvent
from threads.loop.budget import room_for, room_in
from threads.loop.history import CallState
from threads.loop.runtime import Halt, Parked, Runtime, lost
from threads.loop.team_runtime import TeamRuntime
from threads.loop.teams import put_text
from threads.reduce.fold import loop_parked
from threads.result import Err
from threads.store import Draft
from threads.store.conn import Conn
from threads.store.lines import uuid7
from threads.store.writer import DecideTx, Refusal
from threads.team.ask import AskPlan, ask, reply
from threads.team.batch import Batch
from threads.team.call import CallContext, call_request, named
from threads.team.cancel import cancel
from threads.team.close import reader_of
from threads.team.constants import TEAM_CONSTANTS
from threads.team.ops import StartPlan, send, start
from threads.team.policy import rule_for
from threads.team.rows import MemberRow, member_named, own_rows
from threads.team.watch import monitor, wait


def _team(rt: Runtime) -> TeamRuntime:
    if rt.team is None:
        raise AssertionError("a team tool outside a team")
    return rt.team


async def _decided(
    rt: Runtime, call: ToolCallEvent, big: JsonValue, op: Callable[[CallContext], object]
) -> Halt | None:
    """One team op decided under the caller's writer."""
    team = _team(rt)

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, team.mint)
        op(
            CallContext(
                tx.conn, tx.fold, batch, call, lambda _text: big, reader_of(tx.read), team.rules
            )
        )
        return batch.drafts

    done = await rt.append_decided(decide)
    if isinstance(done, Refusal):
        raise AssertionError("a team op records its refusals")
    if isinstance(done, Err):
        return lost(done.error)
    # An ask or a wait parked its call: the park stops the run until an answer resumes it. (A
    # cancel that landed first, Barred, leaves the call to the cancellation step.)
    held = loop_parked(rt.fold)
    return Parked("awaiting_member", tuple(held)) if held else None


async def start_call(rt: Runtime, state: CallState) -> Halt | None:
    """member.start: the start's fields are resolved against the listed agent, and the member's
    config (a dynamic agent's with the define) is stored before the append that pins it."""
    team = _team(rt)
    call = state.call
    args = StartInput.model_validate(dict(call.data.input))
    starter = _name(rt)
    got = await start_pin(team.pin, rt.store.put_artifact, args.agent, _chosen(args), starter)
    pinned = got.pinned
    agents = {} if pinned is None else {args.agent: pinned.config_hash}
    # A model start has no budget of its own, so the member's is the rule's alone (lane 29C).
    rule = rule_for(team.rules, starter, args.agent, "start")
    cap = None if rule is None else rule.get("budget")
    room = pinned is not None and await room_for(rt, pinned, cap)
    thread = uuid7(rt.clock())
    plan = StartPlan(agents, team.limits, lambda _agent: room, thread, got.resolved, cap)
    big = await _big(rt, args.task)
    return await _decided(
        rt, call, big, lambda ctx: start(call_request(ctx), args.agent, args.task, plan)
    )


def _chosen(args: StartInput) -> Chosen:
    def given[T](value: T | MISSING) -> T | None:
        return None if value is MISSING else value

    return Chosen(
        label=given(args.label),
        instructions=given(args.instructions),
        tools=None if args.tools is MISSING else tuple(args.tools),
        model=given(args.model),
    )


def _name(rt: Runtime) -> str:
    """The starter's name: the calling lead's agent name, which marks the written block."""
    started = next(e for e in rt.events if isinstance(e, ThreadStartedEvent))
    return started.data.agent_name


async def _big(rt: Runtime, text: str) -> JsonValue:
    """A text over the inline cap, stored before the append that names it."""
    return (
        await put_text(rt, text) if len(text.encode()) > TEAM_CONSTANTS.inline_cap_bytes else None
    )


async def send_call(rt: Runtime, state: CallState) -> Halt | None:
    team = _team(rt)
    call = state.call
    args = SendInput.model_validate(dict(call.data.input))
    big = await _big(rt, args.text)
    return await _decided(
        rt,
        call,
        big,
        lambda ctx: send(call_request(ctx), named(ctx, args.to), args.text, team.limits),
    )


async def ask_call(rt: Runtime, state: CallState) -> Halt | None:
    """ask.open; headroom is on the recipient's budgets, read from its log before the append."""
    team = _team(rt)
    call = state.call
    args = AskInput.model_validate(dict(call.data.input))
    big = await _big(rt, args.question)
    room = await _room(rt, team, args.to)
    plan = AskPlan(team.limits, lambda _row: room)
    return await _decided(rt, call, big, lambda ctx: ask(ctx, args.to, args.question, plan))


async def _room(rt: Runtime, team: TeamRuntime, name: str) -> bool:
    """Whether the named member's budgets have room for one request of its model."""
    if team.recipient is None:
        return True
    row = await rt.store.run(lambda c: _named(c, rt, name))
    to = None if row is None else await team.recipient(row)
    return to is None or await room_in(rt, to)


def _named(conn: Conn, rt: Runtime, name: str) -> MemberRow | None:
    """The named member of the team this thread acts in (a nested lead's own team first)."""
    thread = rt.fold.thread_id
    rows = [] if thread is None else own_rows(conn, thread)
    row = next((r for r in rows if r.role == "lead"), rows[0] if rows else None)
    return None if row is None else member_named(conn, row.team_id, name)


async def reply_call(rt: Runtime, state: CallState) -> Halt | None:
    call = state.call
    args = ReplyInput.model_validate(dict(call.data.input))
    big = await _big(rt, args.text)
    return await _decided(rt, call, big, lambda ctx: reply(ctx, args.ask_id, args.text))


async def wait_call(rt: Runtime, state: CallState) -> Halt | None:
    call = state.call
    args = WaitInput.model_validate(dict(call.data.input))
    return await _decided(rt, call, None, lambda ctx: wait(ctx, args.members))


async def monitor_call(rt: Runtime, state: CallState) -> Halt | None:
    call = state.call
    args = MonitorInput.model_validate(dict(call.data.input))
    return await _decided(rt, call, None, lambda ctx: monitor(ctx, args.member))


async def cancel_call(rt: Runtime, state: CallState) -> Halt | None:
    """cancel's request: durable intent; the member's writer applies it at its next step."""
    call = state.call
    args = CancelInput.model_validate(dict(call.data.input))
    return await _decided(rt, call, None, lambda ctx: cancel(ctx, args.member))
