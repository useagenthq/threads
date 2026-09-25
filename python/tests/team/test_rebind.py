"""A failed rebind (spec/schema/README.md, "Teams", "A failed rebind"): materialize finds the
member's definition changed (pin_mismatch). One append opens the member's branch and ends it
failed; no model request is ever made for it, not then and not on any later run; the task
notification wakes the lead. A setup that fails for now (a server that can't be reached) is not a
failed rebind: the member is tried again after a backoff (N4). Mirrors TypeScript's
test/team/rebind.test.ts."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal

from pydantic import BaseModel
from team.run_kit import call, events, member_events, receipts, say, sq_of, start, types
from team.team_kit import assert_team_replays

from threads import Completed, RunContext, agent, scripted_model, sqlite, tool
from threads.agents.bindings import AppTool, Fence
from threads.log import MemberEndedEvent


class _Doc(BaseModel):
    id: str


async def _text(_args: _Doc, _ctx: RunContext[object]) -> str:
    return "text"


class _Drifting:
    """An MCP server double whose nth connect lists `connects[n]`'s tool, or raises at a None or
    past the list."""

    def __init__(self, connects: Sequence[str | None]) -> None:
        self._connects = list(connects)

    @property
    def name(self) -> str:
        return "docs"

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]:
        name = self._connects.pop(0) if self._connects else None
        if name is None:
            raise RuntimeError("the docs server is gone")
        return self._session(name)

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


async def _failed_rebind(connects: Sequence[str], code: Literal["pin_mismatch"]) -> None:
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


def test_a_member_whose_setup_fails_for_now_is_tried_again_and_runs_once_it_is_back() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        # Pinned at start, gone once at materialize, then back.
        docs = _Drifting(["read_a", None, "read_a", "read_a", "read_a"])
        model = scripted_model({"responses": [say("Read the docs.")]})
        researcher = agent(name="researcher", model=model, tools=[docs])
        lead_script = [
            start("c1", "researcher", "Read the docs."),
            say("Started."),
            say("The researcher read the docs."),
        ]
        lead = agent(
            name="lead", model=scripted_model({"responses": lead_script}), team=[researcher]
        )
        r = await asyncio.wait_for(lead.run("Work.", store=store), 30)
        assert isinstance(r, Completed), r
        assert r.output == "The researcher read the docs."
        member = await member_events(store, r.team.ref.id, "researcher-1")
        assert "member_idle" in types(member)
        assert "member_ended" not in types(member)
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_a_setup_that_never_recovers_ends_the_member_setup_failed() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        # Pinned at start; the server is down for good after that.
        docs = _Drifting(["read_a"])
        model = scripted_model({"responses": [say("never")]})
        researcher = agent(name="researcher", model=model, tools=[docs])
        lead_script = [
            start("c1", "researcher", "Read the docs."),
            say("Started."),
            say("The researcher could not start."),
        ]
        lead = agent(
            name="lead", model=scripted_model({"responses": lead_script}), team=[researcher]
        )
        r = await asyncio.wait_for(lead.run("Work.", store=store), 30)
        assert isinstance(r, Completed), r
        assert r.output == "The researcher could not start."
        member = await member_events(store, r.team.ref.id, "researcher-1")
        ended = next(e for e in member if isinstance(e, MemberEndedEvent))
        result = ended.data.model_dump(mode="json", by_alias=True)["result"]
        assert result["error"]["code"] == "setup_failed"
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_a_member_stuck_in_setup_can_still_be_cancelled() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        docs = _Drifting(["read_a"])
        model = scripted_model({"responses": [say("never")]})
        researcher = agent(name="researcher", model=model, tools=[docs])
        lead_script = [
            start("c1", "researcher", "Read the docs."),
            call("c2", "cancel", {"member": "researcher-1"}),
            say("Cancelled it."),
            say("The researcher was cancelled."),
        ]
        lead = agent(
            name="lead", model=scripted_model({"responses": lead_script}), team=[researcher]
        )
        r = await asyncio.wait_for(lead.run("Work.", store=store), 30)
        assert isinstance(r, Completed), r
        assert r.output == "The researcher was cancelled."
        member = await member_events(store, r.team.ref.id, "researcher-1")
        assert types(member) == [
            "thread_started",
            "user_input",
            "message_received",
            "cancel_requested",
            "cancelled",
            "turn_completed",
            "member_ended",
            "message_sent",
        ]
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())
