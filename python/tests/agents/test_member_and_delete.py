"""Team member names are unique while a member runs, and deleting a thread takes its subagent
threads with it but never a handoff target (spec/schema/README.md: Team tools, Deleting a
thread)."""

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, JsonValue

from threads import Completed, HandedOff, RunContext, Store, agent, scripted_model, sqlite, tool
from threads.agents.store import now_ms, open_store
from threads.log import AgentSpawnedEvent, Event, Permissions, ThreadId, ToolResultEvent
from threads.result import Err, Ok
from threads.store.deletion import delete_thread
from threads.thread.handle import open_thread

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
GATE = asyncio.Event()


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def call(name: str, args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def spawn(name: str, call_id: str, *, background: bool = False) -> JsonValue:
    args: dict[str, JsonValue] = {"agent": name, "prompt": "Go."}
    if background:
        args["background"] = True
    return call("spawn_agent", args, call_id)


class Empty(BaseModel):
    pass


async def wait(_args: Empty, _ctx: RunContext[None]) -> str:
    await GATE.wait()
    return "done waiting"


async def release(_args: Empty, _ctx: RunContext[None]) -> str:
    GATE.set()
    return "released"


WAIT = tool(name="wait", description="Wait.", input=Empty, runs="host", execute=wait)
RELEASE = tool(name="release", description="Release.", input=Empty, runs="host", execute=release)


def allow(*names: str) -> Permissions:
    return Permissions(
        mode="default",
        allow=list(names),
        ask=[],
        deny=[],
        protected_paths=[],
        allow_bypass=False,
        plan_exit_mode="default",
    )


def only[T](events: Sequence[Event], kind: type[T]) -> list[T]:
    return [e for e in events if isinstance(e, kind)]


async def events(store: Store, thread: ThreadId) -> list[Event]:
    opened = await open_thread(store, thread)
    assert isinstance(opened, Ok)
    timeline = await opened.value.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def test_a_second_spawn_of_a_running_member_is_refused_and_appends_nothing() -> None:
    async def main() -> None:
        GATE.clear()
        scanner = agent(
            name="scanner",
            model=scripted_model({"responses": [call("wait", {}, "w1"), text("clean")]}),
            tools=[WAIT],
            permissions=allow("wait"),
        )
        steps = [
            spawn("scanner", "c1", background=True),
            spawn("scanner", "c2"),
            call("release", {}, "c3"),
            text("ok"),
        ]
        lead = agent(
            model=scripted_model({"responses": steps}),
            subagents=[scanner],
            tools=[RELEASE],
            permissions=allow("release", "spawn_agent"),
        )
        store = sqlite(":memory:")
        result = await lead.run("scan twice", store=store, deps=None)
        assert isinstance(result, Completed)
        got = await events(store, result.thread.id)
        assert len(only(got, AgentSpawnedEvent)) == 1
        refused = next(r for r in only(got, ToolResultEvent) if r.data.call_id == "c2")
        assert refused.data.preview == "member_active: scanner is still running"
        assert refused.data.is_error

    asyncio.run(main())


def test_deleting_a_thread_deletes_its_subagents_recursively() -> None:
    async def main() -> None:
        linter = agent(name="linter", model=scripted_model({"responses": [text("tidy")]}))
        reviewer = agent(
            name="reviewer",
            model=scripted_model({"responses": [spawn("linter", "r1"), text("LGTM")]}),
            subagents=[linter],
        )
        lead = agent(
            model=scripted_model({"responses": [spawn("reviewer", "c1"), text("done")]}),
            subagents=[reviewer],
        )
        store = sqlite(":memory:")
        result = await lead.run("go", store=store)
        assert isinstance(result, Completed)
        child = only(await events(store, result.thread.id), AgentSpawnedEvent)[0].data
        grandchild = only(await events(store, child.child_thread_id), AgentSpawnedEvent)[0].data
        sq = await open_store(store)
        tenant, root, now = store.tenant, result.thread.id, now_ms()
        assert await sq.run(lambda c: delete_thread(c, tenant, root, now)) == Ok(3)
        for gone in (root, child.child_thread_id, grandchild.child_thread_id):
            assert isinstance(await open_thread(store, gone), Err)

    asyncio.run(main())


def test_deleting_a_thread_keeps_its_handoff_target() -> None:
    async def main() -> None:
        billing = agent(name="billing", model=scripted_model({"responses": [text("Refunded.")]}))
        front = agent(
            model=scripted_model({"responses": [call("handoff", {"agent": "billing"}, "h1")]}),
            handoffs=[billing],
        )
        store = sqlite(":memory:")
        result = await front.run("refund", store=store)
        assert isinstance(result, HandedOff)
        sq = await open_store(store)
        tenant, source, now = store.tenant, result.thread.id, now_ms()
        assert await sq.run(lambda c: delete_thread(c, tenant, source, now)) == Ok(1)
        assert isinstance(await open_thread(store, source), Err)
        assert isinstance(await open_thread(store, result.to_thread.id), Ok)

    asyncio.run(main())
