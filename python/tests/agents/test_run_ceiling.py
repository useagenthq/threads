"""The run ceiling (spec/api.json Agent.run/stream `ceiling`, host `ceiling`; ): every decision of the run, and of every handoff target it starts, is
also decided under it and the stricter wins. A handoff target is capped by the ceilings of the
run that handed off, never by the source agent's own policy."""

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, JsonValue

from threads import Agent, HandedOff, RunContext, Thread, agent, scripted_model, sqlite, tool
from threads.host import host
from threads.log import Event, Permissions, Principal, ToolResultEvent
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALICE = Principal(issuer="api", tenant="local", subject="alice")


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def call(name: str, args: JsonValue, call_id: str = "c1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def rules(allow: Sequence[str] = (), deny: Sequence[str] = ()) -> Permissions:
    return Permissions(
        mode="default",
        allow=list(allow),
        ask=[],
        deny=list(deny),
        protected_paths=[],
        allow_bypass=False,
        plan_exit_mode="default",
    )


class Note(BaseModel):
    text: str


async def results_of(thread: Thread) -> list[ToolResultEvent]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    events: list[Event] = [e.event for e in timeline.value.entries]
    return [e for e in events if isinstance(e, ToolResultEvent)]


def sender(sent: list[str], responses: Sequence[JsonValue], name: str = "agent") -> Agent[None]:
    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        return "sent"

    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    return agent(
        name=name,
        model=scripted_model({"responses": list(responses)}),
        tools=[send_tool],
        permissions=rules(allow=["send"]),
    )


def test_a_run_ceiling_denies_what_the_agent_allows() -> None:
    sent: list[str] = []

    async def main() -> None:
        bot = sender(sent, [call("send", {"text": "x"}), text("ok")])
        done = await bot.run(
            "go", store=sqlite(":memory:"), deps=None, ceiling=rules(deny=["send"])
        )
        (denied,) = await results_of(done.thread)
        assert (denied.data.origin, denied.data.is_error) == ("denied", True)

    asyncio.run(main())
    assert sent == []


def test_a_handoff_target_is_capped_by_the_runs_ceiling_not_the_source_policy() -> None:
    def front(target: Agent[None]) -> Agent[None]:
        handoff = call("handoff", {"agent": "billing"})
        return agent(
            model=scripted_model({"responses": [handoff]}),
            handoffs=[target],
            permissions=rules(deny=["send"]),
        )

    async def moved(ceiling: Permissions | None, sent: list[str]) -> list[ToolResultEvent]:
        billing = sender(sent, [call("send", {"text": "x"}), text("done")], "billing")
        store = sqlite(":memory:")
        start = front(billing)
        if ceiling is None:
            result = await start.run("refund", store=store)
        else:
            result = await start.run("refund", store=store, ceiling=ceiling)
        assert isinstance(result, HandedOff), result
        return await results_of(result.to_thread)

    free: list[str] = []
    (ran,) = asyncio.run(moved(None, free))
    assert (ran.data.origin, free) == ("executed", ["x"])
    capped: list[str] = []
    (denied,) = asyncio.run(moved(rules(deny=["send"]), capped))
    assert (denied.data.origin, capped) == ("denied", [])


def test_a_host_ceiling_caps_the_runs_it_starts() -> None:
    sent: list[str] = []

    async def main() -> None:
        from threads._generated.host_api_v1 import StartRunRequest  # noqa: PLC0415

        bot = sender(sent, [call("send", {"text": "x"}), text("ok")])
        store = sqlite(":memory:")
        async with host(store=store, agents={"bot": bot}, ceiling=rules(deny=["send"])) as served:
            request = StartRunRequest.model_validate({"agent": "bot", "input": "go"})
            accepted = await served.start_run(request, principal=ALICE, idempotency_key="k")
            assert isinstance(accepted, Ok)
            thread = Thread(accepted.value.thread_id, accepted.value.branch_id, store)
            found: list[ToolResultEvent] = []
            for _ in range(100):
                found = await results_of(thread)
                if found:
                    break
                await asyncio.sleep(0.01)
            assert [r.data.origin for r in found] == ["denied"]

    asyncio.run(main())
    assert sent == []
