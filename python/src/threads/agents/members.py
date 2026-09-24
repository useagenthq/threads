"""The team tools start and send (spec/schema/README.md, "Teams", Model tools), run by the loop like
any framework tool: each is one decided append under the caller's writer, holding the policy
decision, the op's events and the call's one result, so a call re-dispatched after a crash either
finds that append or makes it now, never twice."""

from collections.abc import Callable, Sequence

from pydantic import JsonValue

from threads._generated.tools_v1 import SendInput, StartInput
from threads.log import ToolCallEvent
from threads.loop.budget import room_for
from threads.loop.history import CallState
from threads.loop.runtime import Halt, Runtime, lost
from threads.loop.team_runtime import TeamRuntime
from threads.loop.teams import put_text
from threads.result import Err
from threads.store import Draft
from threads.store.lines import uuid7
from threads.store.writer import DecideTx, Refusal
from threads.team.batch import Batch
from threads.team.call import CallContext
from threads.team.constants import TEAM_CONSTANTS
from threads.team.ops import StartPlan, send, start


def _team(rt: Runtime) -> TeamRuntime:
    if rt.team is None:
        raise AssertionError("a team tool outside a team")
    return rt.team


async def _decided(
    rt: Runtime, call: ToolCallEvent, big: JsonValue, op: Callable[[CallContext], None]
) -> Halt | None:
    """One team op decided under the caller's writer."""
    team = _team(rt)

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, team.mint)
        op(CallContext(tx.conn, tx.fold.events, batch, call, lambda _text: big))
        return batch.drafts

    done = await rt.append_decided(decide)
    if isinstance(done, Refusal):
        raise AssertionError("a team op records its refusals")
    # A cancel that landed first (Barred): the cancellation step closes the call.
    return lost(done.error) if isinstance(done, Err) else None


async def start_call(rt: Runtime, state: CallState) -> Halt | None:
    """member.start: the listed agent's config is stored before the append that pins it."""
    team = _team(rt)
    call = state.call
    args = StartInput.model_validate(dict(call.data.input))
    pinned = await team.pin(args.agent)
    agents: dict[str, str] = {}
    room = False
    if pinned is not None:
        await rt.store.put_artifact(pinned.config)
        agents[args.agent] = pinned.config_hash
        room = await room_for(rt, pinned)
    thread = uuid7(rt.clock())
    plan = StartPlan(agents, team.limits, lambda _agent: room, thread)
    return await _decided(rt, call, None, lambda ctx: start(ctx, args.agent, args.task, plan))


async def send_call(rt: Runtime, state: CallState) -> Halt | None:
    team = _team(rt)
    call = state.call
    args = SendInput.model_validate(dict(call.data.input))
    big = None
    if len(args.text.encode()) > TEAM_CONSTANTS.inline_cap_bytes:
        big = await put_text(rt, args.text)
    return await _decided(rt, call, big, lambda ctx: send(ctx, args.to, args.text, team.limits))
