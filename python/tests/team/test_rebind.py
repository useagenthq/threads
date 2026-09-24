"""A failed rebind (spec/schema/README.md, "Teams", "A failed rebind"): materialize finds the
member's definition changed (pin_mismatch) or not runnable here (pin_unavailable). One append opens
the member's branch and ends it failed; no model request is ever made for it, not then and not on
any later run; the task notification wakes the lead. Mirrors TypeScript's
test/team/rebind.test.ts."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal

from pydantic import BaseModel
from team.run_kit import events, member_events, receipts, say, sq_of, start, types
from team.team_kit import assert_team_replays

from threads import Completed, RunContext, agent, scripted_model, sqlite, tool
from threads.agents.bindings import AppTool, Fence
from threads.log import MemberEndedEvent


class _Doc(BaseModel):
    id: str


async def _text(_args: _Doc, _ctx: RunContext[object]) -> str:
    return "text"


class _Drifting:
    """An MCP server double whose nth connect lists `connects[n]`'s tool, or raises past the
    list."""

    def __init__(self, connects: Sequence[str]) -> None:
        self._connects = list(connects)

    @property
    def name(self) -> str:
        return "docs"

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]:
        if not self._connects:
            raise RuntimeError("the docs server is gone")
        return self._session(self._connects.pop(0))

    @asynccontextmanager
    async def _session(self, name: str) -> AsyncGenerator[Sequence[AppTool[object]]]:
        yield (
            tool(
                name=f"mcp__docs__{name}",
                description="Read a document.",
                input=_Doc,
                runs="host",
                effect="read_only",
                execute=_text,
            ),
        )


async def _failed_rebind(
    connects: Sequence[str], code: Literal["pin_mismatch", "pin_unavailable"]
) -> None:
    store = sqlite(":memory:")
    model = scripted_model({"responses": [say("never")]})
    researcher = agent(name="researcher", model=model, tools=[_Drifting(connects)])
    lead_script = [
        start("c1", "researcher", "Read the docs."),
        say("Started."),
        say("The researcher could not start."),
        say("Still here."),
    ]
    lead = agent(name="lead", model=scripted_model({"responses": lead_script}), team=[researcher])
    r = await lead.run("Work.", store=store)
    assert isinstance(r, Completed), r
    assert r.output == "The researcher could not start."
    member = await member_events(store, r.team.ref.id, "researcher-1")
    assert types(member) == [
        "thread_started",
        "user_input",
        "turn_completed",
        "member_ended",
        "message_sent",
    ]
    ended = next(e for e in member if isinstance(e, MemberEndedEvent))
    result = ended.data.model_dump(mode="json", by_alias=True)["result"]
    assert result["status"] == "failed"
    assert result["error"] == {"code": code, "message": f"rebind failed: {code}"}
    assert len(receipts(await events(store, r.thread), "member_ended")) == 1
    # A later run of the lead (a restart) sends the member nothing and no model request.
    again = await lead.run("Anything else?", store=store, thread=r.thread)
    assert isinstance(again, Completed), again
    assert again.output == "Still here."
    after = await member_events(store, r.team.ref.id, "researcher-1")
    assert len(after) == len(member)
    assert model.remaining == 1
    await assert_team_replays(await sq_of(store), r.team.ref.id)


def test_pin_mismatch_the_definition_changed_between_start_and_materialize() -> None:
    asyncio.run(_failed_rebind(["read_a", "read_b", "read_b"], "pin_mismatch"))


def test_pin_unavailable_the_definition_cant_be_set_up_here() -> None:
    asyncio.run(_failed_rebind(["read_a"], "pin_unavailable"))
