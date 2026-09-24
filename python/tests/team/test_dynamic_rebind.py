"""A dynamic member rebound at materialize (spec/schema/README.md, Teams, "Dynamic members"): its
template with the recorded define and starter. A chosen tool that is gone is pin_unavailable, a
tool that pins differently pin_mismatch, each one end append with no model request. An approval
raised in a dynamic member shows what its starter chose. Mirrors TypeScript's
test/team/dynamic-rebind.test.ts."""

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, JsonValue
from team.run_kit import call, member_events, say, sq_of, types
from team.team_kit import assert_team_replays

from threads import (
    Completed,
    Parked,
    RunContext,
    agent,
    dynamic_agent,
    scripted_model,
    sqlite,
    tool,
)
from threads._generated.host_api_v1 import Member
from threads.agents.bindings import AppTool, Fence
from threads.agents.dynamic_agent import member_definition
from threads.log import Budget, MemberDefine, MemberEndedEvent, ThreadId
from threads.result import Ok
from threads.team.rows import member_rows
from threads.thread.handle import open_thread


class _Doc(BaseModel):
    id: str


async def _text(_args: _Doc, _ctx: RunContext[object]) -> str:
    return "text"


class _Drifting:
    """An MCP server double whose nth connect lists `connects[n]`: a tool name and description."""

    def __init__(self, connects: Sequence[tuple[str, str]]) -> None:
        self._connects = list(connects)

    @property
    def name(self) -> str:
        return "docs"

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]:
        return self._session(*self._connects.pop(0))

    @asynccontextmanager
    async def _session(
        self, name: str, description: str
    ) -> AsyncGenerator[Sequence[AppTool[object]]]:
        yield (
            tool(
                name=f"mcp__docs__{name}",
                description=description,
                input=_Doc,
                runs="host",
                effect="read_only",
                execute=_text,
            ),
        )


async def _rebound(
    connects: Sequence[tuple[str, str]], code: Literal["pin_mismatch", "pin_unavailable"]
) -> None:
    store = sqlite(":memory:")
    model = scripted_model({"responses": [say("never")]})
    template = dynamic_agent(name="reader", models={"fast": model}, tools=[_Drifting(connects)])
    args: JsonValue = {"agent": "reader", "task": "Read.", "tools": ["mcp__docs__read_a"]}
    lead_script = [call("c1", "start", args), say("Started."), say("It could not start.")]
    lead = agent(name="lead", model=scripted_model({"responses": lead_script}), team=[template])
    r = await lead.run("Work.", store=store)
    assert isinstance(r, Completed), r
    member = await member_events(store, r.team.ref.id, "reader-1")
    assert "model_request" not in types(member)
    ended = [e for e in member if isinstance(e, MemberEndedEvent)]
    assert len(ended) == 1
    result = ended[0].data.model_dump(mode="json", by_alias=True)["result"]
    assert result["error"] == {"code": code, "message": f"rebind failed: {code}"}
    assert model.remaining == 1
    await assert_team_replays(await sq_of(store), r.team.ref.id)


def test_a_chosen_tool_gone_before_materialize_is_pin_unavailable() -> None:
    # Connects: start's base pin, start's pin with the define, then materialize's rebind.
    connects = [("read_a", "Read."), ("read_a", "Read."), ("read_b", "Read.")]
    asyncio.run(_rebound(connects, "pin_unavailable"))


def test_a_chosen_tool_described_differently_is_pin_mismatch() -> None:
    connects = [("read_a", "Read."), ("read_a", "Read."), ("read_a", "Read it all.")]
    asyncio.run(_rebound(connects, "pin_mismatch"))


class _Mail(BaseModel):
    to: str


async def _send(_args: _Mail, _ctx: RunContext[None]) -> str:
    return "sent"


def test_an_approval_in_a_dynamic_member_shows_its_define_and_label() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        mail = tool(name="send_email", description="Send.", input=_Mail, runs="host", execute=_send)
        fast = scripted_model({"responses": [call("m1", "send_email", {"to": "bob"})]})
        template = dynamic_agent(name="mailer", models={"fast": fast}, tools=[mail])
        args: JsonValue = {"agent": "mailer", "task": "Mail bob.", "label": "bob mailer"}
        script = [call("c1", "start", args), say("Started.")]
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[template])
        r = await lead.run("Mail bob.", store=store)
        assert isinstance(r, Parked), r
        sq = await sq_of(store)
        rows = await sq.run(lambda c: member_rows(c, r.team.ref.id))
        row = next(m for m in rows if m.name == "mailer-1")
        handle = await open_thread(store, ThreadId(row.thread_id))
        assert isinstance(handle, Ok)
        pending = await handle.value.pending_approvals()
        assert isinstance(pending, Ok)
        member = pending.value[0].member
        assert isinstance(member, Member)
        assert member.model_dump(mode="json", exclude_unset=True) == {
            "name": "mailer-1",
            "label": "bob mailer",
            "define": {"tools": ["send_email"], "model": "fast"},
        }

    asyncio.run(main())


def test_the_template_budget_stops_a_member_and_the_lead_hears_of_it() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        mail = tool(
            name="send_email",
            description="Send.",
            input=_Mail,
            runs="host",
            effect="read_only",
            execute=_send,
        )
        fast = scripted_model({"responses": [call("m1", "send_email", {"to": "bob"}), say("x")]})
        template = dynamic_agent(
            name="mailer", models={"fast": fast}, tools=[mail], budget=Budget(max_model_requests=1)
        )
        script = [
            call("c1", "start", {"agent": "mailer", "task": "Mail bob."}),
            say("Started."),
            say("The mailer ran out of budget."),
        ]
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[template])
        r = await lead.run("Mail bob.", store=store)
        assert isinstance(r, Completed), r
        assert r.output == "The mailer ran out of budget."
        member = await member_events(store, r.team.ref.id, "mailer-1")
        ended = next(e for e in member if isinstance(e, MemberEndedEvent))
        result = ended.data.model_dump(mode="json", by_alias=True)["result"]
        assert result["status"] == "budget_exhausted"

    asyncio.run(main())


def test_golden_member_config() -> None:
    """The cross-language golden's Python side (lane 26): a lead's specialist-1."""

    async def _invoice(_args: _Doc, _ctx: RunContext[None]) -> str:
        return "paid"

    invoice = tool(
        name="invoice_status",
        description="An invoice's status.",
        input=_Doc,
        runs="host",
        effect="read_only",
        execute=_invoice,
    )
    template = dynamic_agent(
        name="specialist",
        instructions="You are a careful analyst.",
        models={"fast": scripted_model({"responses": []})},
        tools=[invoice],
    )
    define = MemberDefine(instructions="Answer yes or no.", tools=["invoice_status"], model="fast")
    d = replace(member_definition(template.definition, define, "lead"), in_team=True)
    started, raw = d.pin()
    config = json.loads(raw)
    assert config["dynamic"] == {
        "template": "specialist",
        "define": {
            "instructions": "Answer yes or no.",
            "tools": ["invoice_status"],
            "model": "fast",
        },
    }
    print(raw.decode(), started["config_hash"])
