"""Who may answer a challenge or settle a parked effect: with no approvers
configured, the principal whose input started the root run may approve its own calls; a
resolution that accepts duplicate risk (assume_not_done) needs a configured approver or the local
operator; configured approvers are the only ones."""

import asyncio

from pydantic import BaseModel, JsonValue

from threads import Parked, RunContext, Thread, agent, scripted_model, sqlite, tool
from threads.agents.run import execute
from threads.agents.store import open_store
from threads.log import EffectResolvedEvent, ParseError, Principal
from threads.result import Err, Ok
from threads.thread.control import LOCAL_OPERATOR

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALICE = Principal(issuer="api", tenant="local", subject="alice")
BOB = Principal(issuer="api", tenant="local", subject="bob")


def _text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def _use() -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "c1", "name": "send", "input": {"text": "x"}}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Note(BaseModel):
    text: str


def _drop(_item: object) -> None:
    pass


async def _challenge(thread: Thread) -> str:
    pending = await thread.pending_approvals()
    assert isinstance(pending, Ok)
    return pending.value[0].challenge_id


def test_unconfigured_the_originator_approves_its_own_call_and_no_one_else() -> None:
    sent: list[str] = []

    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        return "sent"

    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    bot = agent(model=scripted_model({"responses": [_use(), _text("Done.")]}), tools=[send_tool])

    async def main() -> None:
        parked = await execute(
            bot.definition,
            "send it",
            {"store": sqlite(":memory:"), "principal": ALICE},
            None,
            _drop,
        )
        assert isinstance(parked, Parked)
        challenge = await _challenge(parked.thread)
        refused = await parked.thread.approve(challenge, BOB)
        assert refused == Err(ParseError("forbidden", "not an approver of this thread"))
        assert isinstance(await parked.thread.approve(challenge, ALICE), Ok)

    asyncio.run(main())


def test_configured_approvers_exclude_the_originator() -> None:
    async def send(_args: Note, _ctx: RunContext[None]) -> str:
        return "sent"

    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    bot = agent(
        model=scripted_model({"responses": [_use(), _text("Done.")]}),
        tools=[send_tool],
        approvers=[BOB],
    )

    async def main() -> None:
        parked = await execute(
            bot.definition,
            "send it",
            {"store": sqlite(":memory:"), "principal": ALICE},
            None,
            _drop,
        )
        assert isinstance(parked, Parked)
        challenge = await _challenge(parked.thread)
        assert isinstance(await parked.thread.approve(challenge, ALICE), Err)
        assert isinstance(await parked.thread.approve(challenge, BOB), Ok)

    asyncio.run(main())


def test_accepting_duplicate_risk_needs_the_operator_when_no_approver_is_configured() -> None:
    """A send whose run crashed after effect_begin parks; the originator may assume it done but
    not re-send it; the local operator may, and is recorded."""
    gate = asyncio.Event()
    sent: list[str] = []

    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        await gate.wait()
        return "sent"

    send_tool = tool(
        name="send", description="Send.", input=Note, runs="host", execute=send, effect="unguarded"
    )
    script: JsonValue = {"responses": [_use(), _text("Done."), _text("Done.")]}
    bot = agent(model=scripted_model(script), tools=[send_tool])

    async def main() -> None:
        store = sqlite(":memory:")
        parked = await execute(
            bot.definition, "send it", {"store": store, "principal": ALICE}, None, _drop
        )
        assert isinstance(parked, Parked)
        thread = parked.thread
        assert isinstance(await thread.approve(await _challenge(thread), ALICE), Ok)
        # The dispatch begins, then the process dies before it settles.
        crashed = asyncio.create_task(
            execute(bot.definition, None, {"thread": thread}, None, _drop)
        )
        while not sent:
            await asyncio.sleep(0.01)
        crashed.cancel()
        await asyncio.gather(crashed, return_exceptions=True)
        doubt = await execute(bot.definition, None, {"thread": thread}, None, _drop)
        assert isinstance(doubt, Parked)
        (address,) = [p for p in doubt.pending if p.kind == "effect"]
        refused = await thread.resolve_parked(address.id, "assume_not_done", ALICE)
        assert isinstance(refused, Err)
        assert refused.error.code == "forbidden"
        done = await thread.resolve_parked(address.id, "assume_not_done", LOCAL_OPERATOR)
        assert isinstance(done, Ok), done
        read = await (await open_store(store)).read(thread.branch, 0)
        assert isinstance(read, Ok)
        settled = [e for e in read.value.fold.events if isinstance(e, EffectResolvedEvent)]
        assert settled[-1].actor.principal == LOCAL_OPERATOR

    asyncio.run(main())
