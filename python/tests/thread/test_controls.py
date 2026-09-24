"""Thread controls (spec/api.json Thread): approvals are single-use (their authority is in
test_authority), every control is recorded with its principal, and a control reaches a run in
flight through the run's own writer."""

import asyncio

from pydantic import BaseModel, JsonValue

from threads import (
    Agent,
    Cancelled,
    Completed,
    Parked,
    RunContext,
    Thread,
    agent,
    scripted_model,
    tool,
)
from threads._generated.host_api_v1 import SettingsChange
from threads.agents.run import execute
from threads.agents.store import LIVE, now_ms, open_store, scoped, sqlite
from threads.log import (
    ApprovalGrantedEvent,
    CallId,
    CancelRequestedEvent,
    ModeChangedEvent,
    ModelRef,
    ParseError,
    PermissionDecisionEvent,
    Principal,
    ResumedEvent,
    SettingsChangedEvent,
)
from threads.result import Err, Ok
from threads.thread.approvals import suggested_rules
from threads.thread.control import LOCAL_OPERATOR
from threads.thread.handle import open_thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
OTHER_TENANT = Principal(issuer="api", tenant="acme", subject="operator")


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Note(BaseModel):
    text: str


def _drop(_item: object) -> None:
    pass


async def _parked(sent: list[str], *more: JsonValue) -> tuple[Thread, Agent[None, str]]:
    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        return "sent"

    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    script: JsonValue = {"responses": [use("send", {"text": "x"}), *more, text("Done.")]}
    bot = agent(model=scripted_model(script), tools=[send_tool])
    result = await bot.run("send it", store=sqlite(":memory:"), deps=None)
    assert isinstance(result, Parked)
    return result.thread, bot


def test_an_approval_is_single_use_and_resumes_the_run() -> None:
    sent: list[str] = []

    async def main() -> None:
        thread, bot = await _parked(sent)
        pending = await thread.pending_approvals()
        assert isinstance(pending, Ok)
        (challenge,) = pending.value
        assert (challenge.tool, challenge.suggested_rules) == ("send", ("send",))
        refused = await thread.approve(challenge.challenge_id, OTHER_TENANT)
        assert isinstance(refused, Err)
        assert refused.error.code == "forbidden"
        granted = await thread.approve(challenge.challenge_id, LOCAL_OPERATOR)
        assert isinstance(granted, Ok)
        again = await thread.deny(challenge.challenge_id, LOCAL_OPERATOR)
        assert isinstance(again, Err)
        assert again.error.code == "approval_duplicate"
        read = await (await open_store(thread.store)).read(thread.branch, 0)
        assert isinstance(read, Ok)
        tail = read.value.fold.events[-2:]
        assert isinstance(tail[0], ApprovalGrantedEvent)
        assert tail[0].actor.principal == LOCAL_OPERATOR
        assert isinstance(tail[1], ResumedEvent)
        assert tail[1].data.cause_event_id == granted.value.event_id
        resumed = await execute(bot.definition, None, {"thread": thread}, None, _drop)
        assert isinstance(resumed, Completed)
        assert resumed.output == "Done."

    asyncio.run(main())
    assert sent == ["x"]


def test_an_expired_challenge_is_a_denial() -> None:
    async def main() -> None:
        thread, _ = await _parked([])
        pending = await thread.pending_approvals()
        assert isinstance(pending, Ok)
        challenge = pending.value[0].challenge_id
        sq = await open_store(thread.store)
        await sq.run(lambda c: c.execute("UPDATE approvals SET expires_at = 1"))
        expired = await thread.approve(challenge, LOCAL_OPERATOR)
        assert isinstance(expired, Err)
        assert expired.error.code == "approval_expired"
        row = await sq.tables.challenge(challenge)
        assert row is not None
        assert row.state == "expired"

    asyncio.run(main())


def test_controls_record_their_principal_and_refuse_other_tenants() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": [text("Hi.")]}))
        store = sqlite(":memory:")
        done = await bot.run("hi", store=store)
        thread = done.thread
        assert isinstance(await thread.cancel(OTHER_TENANT), Err)
        assert isinstance(await thread.set_mode("plan", LOCAL_OPERATOR), Ok)
        model = ModelRef(provider="scripted", name="scripted-1")
        changed = await thread.set_model(SettingsChange(model=model), LOCAL_OPERATOR)
        assert isinstance(changed, Ok), changed
        assert isinstance(await thread.cancel(LOCAL_OPERATOR), Ok)
        no_question = await thread.answer(CallId("call_9"), "yes", LOCAL_OPERATOR)
        assert no_question == Err(ParseError("no_open_question", "no open question call_9"))
        not_parked = await thread.resolve_parked("b:c", "assume_done", LOCAL_OPERATOR)
        assert isinstance(not_parked, Err)
        assert not_parked.error.code == "not_parked"
        read = await (await open_store(store)).read(thread.branch, 0)
        assert isinstance(read, Ok)
        kinds = [type(e) for e in read.value.fold.events[-3:]]
        assert kinds == [ModeChangedEvent, SettingsChangedEvent, CancelRequestedEvent]
        (main_branch,) = await thread.branches()
        assert (main_branch.branch_id, main_branch.runnable) == (thread.branch, True)
        foreign = await open_thread(scoped(store, "acme"), thread.id)
        assert isinstance(foreign, Err)

    asyncio.run(main())


def test_a_control_reaches_a_run_in_flight_through_its_writer() -> None:
    async def main() -> None:
        store = sqlite(":memory:")

        async def stop(_args: Note, ctx: RunContext[None]) -> str:
            opened = await open_thread(store, ctx.thread_id)
            assert isinstance(opened, Ok)
            assert isinstance(await opened.value.cancel(LOCAL_OPERATOR), Ok)
            return "stopping"

        stop_tool = tool(
            name="stop",
            description="Stop.",
            input=Note,
            runs="host",
            execute=stop,
            effect="read_only",
        )
        script: JsonValue = {"responses": [use("stop", {"text": "x"}), text("never")]}
        bot = agent(model=scripted_model(script), tools=[stop_tool])
        result = await bot.run("go", store=store, deps=None)
        assert isinstance(result, Cancelled), result

    asyncio.run(main())


def test_a_control_through_a_writer_that_lost_its_lease_is_branch_busy() -> None:
    """The run in flight here lost the branch to another holder: the control is refused as
    branch_busy, the same answer as a lease held elsewhere, never a writer's internal code."""

    async def main() -> None:
        store = sqlite(":memory:")
        done = await agent(model=scripted_model({"responses": [text("hi")]})).run("go", store=store)
        sq = await open_store(store)
        taken = await sq.acquire(done.thread.branch, "run", now_ms)
        assert isinstance(taken, Ok)
        await taken.value.release()
        LIVE[done.thread.branch] = taken.value
        try:
            refused = await done.thread.cancel(LOCAL_OPERATOR)
        finally:
            LIVE.pop(done.thread.branch)
        assert isinstance(refused, Err)
        assert refused.error.code == "branch_busy"

    asyncio.run(main())


def test_a_remembered_rule_must_be_one_the_challenge_suggested() -> None:
    async def main() -> None:
        thread, _ = await _parked([])
        pending = await thread.pending_approvals()
        assert isinstance(pending, Ok)
        challenge = pending.value[0].challenge_id
        other = await thread.approve(challenge, LOCAL_OPERATOR, remember_rule="bash(rm:*)")
        assert isinstance(other, Err)
        assert other.error.code == "invalid_request"
        assert isinstance(await thread.approve(challenge, LOCAL_OPERATOR), Ok)

    asyncio.run(main())


def test_bash_any_is_never_a_suggested_rule() -> None:
    # bash(*) allows every command: only configured policy may hold it.
    assert suggested_rules("bash", {"command": "*"}) == ()
    assert suggested_rules("bash", {"command": "ls"}) == ("bash(ls)", "bash(ls:*)")


def test_a_remembered_rule_allows_the_next_call_on_the_thread() -> None:
    sent: list[str] = []

    async def main() -> None:
        thread, bot = await _parked(sent, use("send", {"text": "y"}, "call_2"))
        pending = await thread.pending_approvals()
        assert isinstance(pending, Ok)
        (challenge,) = pending.value
        remembered = await thread.approve(
            challenge.challenge_id, LOCAL_OPERATOR, remember_rule="send"
        )
        assert isinstance(remembered, Ok)
        resumed = await execute(bot.definition, None, {"thread": thread}, None, _drop)
        assert isinstance(resumed, Completed)
        read = await (await open_store(thread.store)).read(thread.branch, 0)
        assert isinstance(read, Ok)
        events = read.value.fold.events
        decided = [
            (e.data.decision, e.data.source)
            for e in events
            if isinstance(e, PermissionDecisionEvent)
        ]
        assert decided == [("ask", "mode"), ("allow", "thread_rule")]

    asyncio.run(main())
    assert sent == ["x", "y"]
