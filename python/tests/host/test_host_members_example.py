"""`python/examples/host_members.py` (lane 29's proof list): the example builds its host without
raising, and the shape it describes works end to end on a scripted model — support asks billing,
billing answers with its own tool and `reply`, and support's parked ask call gets one result.
Mirrors TypeScript's host/test/host-members-example.test.ts."""

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from host.host_members_kit import events_of, ran, rule, served, support
from host.test_http import run, text, use
from pydantic import BaseModel
from team.run_kit import answers, reply_to, say
from team.team_kit import assert_team_replays

from threadsai import RunContext, agent, sqlite, tool
from threadsai.agents.store import Store, open_store, scoped
from threadsai.host.members import HostMemberOptions
from threadsai.log import (
    Event,
    ModelResponseEvent,
    TextPart,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompletedEvent,
)
from threadsai.team.host_team import host_team_ids

if TYPE_CHECKING:
    from pydantic import JsonValue

    from threadsai.agents.factory import Agent

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "host_members.py"
INVOICES: dict[str, str] = {"INV-1001": "paid", "INV-1002": "overdue"}


class InvoiceInput(BaseModel):
    id: str


async def _invoice_status(args: InvoiceInput, _ctx: RunContext[None]) -> str:
    return INVOICES.get(args.id, "unknown invoice")


def _billing() -> "Agent[None, str]":
    """The example's billing: it looks the invoice up, then replies to the ask it took."""

    def look_up(_request: str) -> "JsonValue":
        return use("invoice_status", {"id": "INV-1001"})

    def answer(request: str) -> "JsonValue":
        return reply_to("r1", request, "INV-1001 is paid.")

    def done(_request: str) -> "JsonValue":
        return say("Answered.")

    invoice_status = tool(
        name="invoice_status",
        description="Look up an invoice's status by id, e.g. INV-1001.",
        input=InvoiceInput,
        runs="host",
        effect="read_only",
        execute=_invoice_status,
    )
    return agent(name="billing", model=answers([look_up, answer, done]), tools=[invoice_status])


def test_the_example_builds_its_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """One line, and it catches config drift the day someone changes a refusal."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    spec = importlib.util.spec_from_file_location("example_host_members", EXAMPLE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert getattr(module, "app", None) is not None


def test_the_example_shape_answers_a_caller_end_to_end() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        script = [use("ask", {"to": "billing", "question": "Is INV-1001 paid?"})]
        script += [text("Billing says it is paid.")]
        agents = {"support": support(script), "billing": _billing()}
        members = {"billing": HostMemberOptions()}
        async with served(agents, [rule("support", "billing", ["ask"])], members, store) as client:
            branch, _status = await ran(client, "support", "Is INV-1001 paid?")
            caller = await _answered(store, branch)
        results = [e for e in caller if isinstance(e, ToolResultEvent)]
        assert len(results) == 1
        answered = json.loads(results[0].data.preview)
        assert answered["status"] == "answered"
        assert answered["text"] == "INV-1001 is paid."
        # The caller's turn runs on to its own answer: an ask parks a run until the answer comes
        # back (design D.5), and recovery reads a resumed park as work to send, not as an
        # interrupted turn.
        ends = [e for e in caller if isinstance(e, TurnCompletedEvent)]
        assert [e.data.reason for e in ends] == ["end_turn"]
        assert _said(caller) == "Billing says it is paid."
        ids = host_team_ids("acme")
        sq = await open_store(scoped(store, "acme"))
        await assert_team_replays(sq, ids.team)

    run(main)


async def _answered(store: Store, branch: str) -> list[Event]:
    """The caller's log once its parked ask has its answer and its turn has ended. The caller
    never runs the member's tool: what it gets back is the reply's text."""
    for _ in range(300):
        events = await events_of(store, branch)
        if any(isinstance(e, TurnCompletedEvent) for e in events):
            assert not [e for e in events if isinstance(e, ToolCallEvent) and e.data.name != "ask"]
            return events
        await asyncio.sleep(0.05)
    raise AssertionError("the caller's ask never got its result")


def _said(events: list[Event]) -> str:
    """The caller's own last words: what support tells its user once billing has answered."""
    responses = [e for e in events if isinstance(e, ModelResponseEvent)]
    if not responses:
        return ""
    return "".join(p.text for p in responses[-1].data.content if isinstance(p, TextPart))
