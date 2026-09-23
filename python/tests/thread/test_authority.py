"""Approval authority (spec/schema/README.md, "Approval authority"): a host handle decides under
the root run's approver set: configured approvers only, or with none configured the root run's
originating principal, for approvals and parked-effect resolutions alike. The in-process Thread
API has operator authority and records the principal it is given."""

import asyncio

from pydantic import BaseModel, JsonValue

from threads import Agent, Parked, RunContext, Thread, agent, scripted_model, sqlite, tool
from threads.agents.run import execute
from threads.agents.store import Store, open_store
from threads.host import host
from threads.log import EffectResolvedEvent, ParseError, Principal
from threads.result import Err, Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALICE = Principal(issuer="api", tenant="local", subject="alice")
BOB = Principal(issuer="api", tenant="local", subject="bob")
FORBIDDEN = Err(ParseError("forbidden", "not an approver of this thread"))


def _text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def _use() -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "c1", "name": "send", "input": {"text": "x"}}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Note(BaseModel):
    text: str


def _drop(_item: object) -> None:
    pass


def _bot(
    sent: list[str], gate: asyncio.Event | None = None, approvers: list[Principal] | None = None
) -> Agent[None]:
    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        if gate is not None:
            await gate.wait()
        return "sent"

    send_tool = tool(
        name="send", description="Send.", input=Note, runs="host", execute=send, effect="unguarded"
    )
    script: JsonValue = {"responses": [_use(), _text("Done."), _text("Done.")]}
    model = scripted_model(script)
    if approvers is None:
        return agent(model=model, tools=[send_tool])
    return agent(model=model, tools=[send_tool], approvers=approvers)


async def _parked_by_alice(bot: Agent[None], store: Store) -> Thread:
    parked = await execute(bot.definition, "go", {"store": store, "principal": ALICE}, None, _drop)
    assert isinstance(parked, Parked)
    return parked.thread


async def _challenge(thread: Thread) -> str:
    pending = await thread.pending_approvals()
    assert isinstance(pending, Ok)
    return pending.value[0].challenge_id


def test_unconfigured_the_originating_principal_approves_over_the_host_and_no_one_else() -> None:
    bot = _bot([])

    async def main() -> None:
        store = sqlite(":memory:")
        thread = await _parked_by_alice(bot, store)
        challenge = await _challenge(thread)
        async with host(store=store, agents={"bot": bot}) as served:
            as_bob = await served.thread(BOB, thread.id, None)
            assert isinstance(as_bob, Ok)
            assert await as_bob.value.approve(challenge, BOB) == FORBIDDEN
            as_alice = await served.thread(ALICE, thread.id, None)
            assert isinstance(as_alice, Ok)
            assert isinstance(await as_alice.value.approve(challenge, ALICE), Ok)

    asyncio.run(main())


def test_configured_approvers_exclude_the_originator() -> None:
    bot = _bot([], approvers=[BOB])

    async def main() -> None:
        store = sqlite(":memory:")
        thread = await _parked_by_alice(bot, store)
        challenge = await _challenge(thread)
        async with host(store=store, agents={"bot": bot}) as served:
            as_alice = await served.thread(ALICE, thread.id, None)
            assert isinstance(as_alice, Ok)
            assert await as_alice.value.approve(challenge, ALICE) == FORBIDDEN
            assert isinstance(await as_alice.value.approve(challenge, BOB), Ok)

    asyncio.run(main())


def test_resolving_a_parked_effect_needs_authority_and_records_the_principal() -> None:
    """A send whose run died after effect_begin parks. Over the host, only the root run's
    originating principal may resolve it, either way; the resolution records who accepted."""
    sent: list[str] = []
    bot = _bot(sent, asyncio.Event())

    async def main() -> None:
        store = sqlite(":memory:")
        thread = await _parked_by_alice(bot, store)
        assert isinstance(await thread.approve(await _challenge(thread), ALICE), Ok)
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
        async with host(store=store, agents={"bot": bot}) as served:
            as_bob = await served.thread(BOB, thread.id, None)
            assert isinstance(as_bob, Ok)
            refusals = [
                await as_bob.value.resolve_parked(address.id, "assume_done", BOB),
                await as_bob.value.resolve_parked(address.id, "assume_not_done", BOB),
            ]
            assert refusals == [FORBIDDEN, FORBIDDEN]
            as_alice = await served.thread(ALICE, thread.id, None)
            assert isinstance(as_alice, Ok)
            done = await as_alice.value.resolve_parked(address.id, "assume_not_done", ALICE)
            assert isinstance(done, Ok), done
        read = await (await open_store(store)).read(thread.branch, 0)
        assert isinstance(read, Ok)
        settled = [e for e in read.value.fold.events if isinstance(e, EffectResolvedEvent)]
        assert settled[-1].actor.principal == ALICE

    asyncio.run(main())


def test_the_in_process_thread_has_operator_authority_and_records_the_principal() -> None:
    async def main() -> None:
        thread = await _parked_by_alice(_bot([], approvers=[ALICE]), sqlite(":memory:"))
        assert isinstance(await thread.approve(await _challenge(thread), BOB), Ok)

    asyncio.run(main())
