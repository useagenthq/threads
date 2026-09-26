"""The host's message_policy (spec/api.json host.message_policy; lane 29 section C): its setup
refusals, the team tools a rule gives an agent in line 0, a rule that makes its from a lead, and
the member budget a start rule caps. Mirrors TypeScript's host/test/message-policy.test.ts. Every
runtime test ends with the team's replay check."""

import contextlib
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import TYPE_CHECKING

import httpx
import pytest
from host.test_http import as_, bearer, run, sse, start, text, use
from pydantic import TypeAdapter
from team.team_kit import assert_team_replays

from threads import MessagePolicyRule, agent, scripted_model, sqlite
from threads.agents.config import ConfigError
from threads.agents.store import Store, open_store, scoped
from threads.host import host
from threads.log import (
    BranchId,
    Budget,
    MemberStartedEvent,
    ModelRequestEvent,
    ThreadStartedEvent,
)
from threads.log.digest import sha256_hex
from threads.render.request import line0
from threads.result import Ok

if TYPE_CHECKING:
    from pydantic import JsonValue

    from threads.agents.factory import Agent

TEAM_TOOLS = frozenset({"ask", "cancel", "monitor", "reply", "send", "start", "wait"})


def rule(sender: str, to: str, allow: Sequence[str], **rest: object) -> MessagePolicyRule:
    return {"from": sender, "to": to, "allow": [*allow], **rest}  # pyright: ignore[reportReturnType]


def billing() -> "Agent[None, str]":
    return agent(name="billing", model=scripted_model({"responses": [text("Paid.")]}))


@contextlib.asynccontextmanager
async def served(
    agents: "Mapping[str, Agent[None, str]]",
    policy: Sequence[MessagePolicyRule],
    store: Store,
) -> AsyncGenerator[httpx.AsyncClient]:
    """The host with its rules, served over its ASGI app (host/test_http.py's `served`, with the
    agents and the policy this lane needs)."""
    served_host = host(store=store, agents=agents, authenticate=bearer, message_policy=policy)
    async with served_host:
        transport = httpx.ASGITransport(app=served_host.asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
            yield client


async def ran(client: httpx.AsyncClient, name: str, prompt: str) -> str:
    """Starts a run of `name` over the API, asserts it completed, returns its branch id."""
    body: JsonValue = {"agent": name, "input": prompt}
    receipt = _ids((await start(client, "alice", f"k-{name}", body)).json())
    follow = f"/v1/threads/{receipt['thread_id']}/runs/{receipt['run_id']}/events"
    last = sse(await client.get(follow, headers=as_("alice")))[-1][1]
    assert isinstance(last, dict)
    result = last["result"]
    assert isinstance(result, dict), result
    assert result["status"] == "completed", result
    return receipt["branch_id"]


def _ids(body: object) -> dict[str, str]:
    """A run receipt's ids, which the route answers as strings."""
    parsed = TypeAdapter(dict[str, str]).validate_python(body)
    return parsed


async def branch_events(store: Store, branch: str) -> list[object]:
    sq = await open_store(scoped(store, "acme"))
    read = await sq.read(BranchId(branch), 0)
    assert isinstance(read, Ok)
    return list(read.value.fold.events)


def test_a_from_or_to_that_names_no_host_agent_is_refused() -> None:
    support = agent(name="support", model=scripted_model({"responses": []}))
    with pytest.raises(ConfigError) as caught:
        host(
            store=sqlite(":memory:"),
            agents={"support": support, "billing": billing()},
            message_policy=[rule("support", "payroll", ["ask"])],
        )
    assert caught.value.code == "invalid_config"
    assert "payroll is not a host agent" in str(caught.value)


def test_an_empty_allow_is_refused_naming_the_rule() -> None:
    support = agent(name="support", model=scripted_model({"responses": []}))
    with pytest.raises(ConfigError) as caught:
        host(
            store=sqlite(":memory:"),
            agents={"support": support, "billing": billing()},
            message_policy=[rule("support", "billing", [])],
        )
    assert caught.value.code == "invalid_config"
    assert "message_policy rule support -> billing: allow is empty" in str(caught.value)


def test_a_repeated_from_to_pair_is_refused_duplicate_name() -> None:
    support = agent(name="support", model=scripted_model({"responses": []}))
    with pytest.raises(ConfigError) as caught:
        host(
            store=sqlite(":memory:"),
            agents={"support": support, "billing": billing()},
            message_policy=[
                rule("support", "billing", ["ask"]),
                rule("support", "billing", ["send"]),
            ],
        )
    assert caught.value.code == "duplicate_name"
    assert "is listed twice" in str(caught.value)


def test_a_rule_from_with_only_ask_sees_only_ask_in_line_0() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        support = agent(
            name="support", model=scripted_model({"responses": [text("Asking billing.")]})
        )
        agents = {"support": support, "billing": billing()}
        async with served(agents, [rule("support", "billing", ["ask"])], store) as client:
            branch = await ran(client, "support", "Is INV-1001 paid?")
        events = await branch_events(store, branch)
        started = next(e for e in events if isinstance(e, ThreadStartedEvent))
        pinned = [t.name for t in started.data.tools if t.name in TEAM_TOOLS]
        assert pinned == ["ask"]
        # Invariant 5: every request of the epoch declares the same line-0 bytes, and those bytes
        # are the pin's, which shows ask and no other team tool.
        declared = [e.data.declared_prefix for e in events if isinstance(e, ModelRequestEvent)]
        assert declared
        assert len({(d.bytes, d.sha256) for d in declared}) == 1
        prefix = line0(started.data, None)  # already ends with its newline
        assert (declared[0].bytes, declared[0].sha256) == (len(prefix), sha256_hex(prefix))
        assert b'"name":"ask"' in prefix
        assert b'"name":"send"' not in prefix

    run(main)


def test_an_agent_no_rule_names_gets_no_team_tool() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        support = agent(
            name="support", model=scripted_model({"responses": [text("No team here.")]})
        )
        agents = {"support": support, "billing": billing()}
        async with served(agents, [rule("billing", "support", ["send"])], store) as client:
            branch = await ran(client, "support", "Hello.")
        events = await branch_events(store, branch)
        started = next(e for e in events if isinstance(e, ThreadStartedEvent))
        assert [t.name for t in started.data.tools if t.name in TEAM_TOOLS] == []

    run(main)


def _support_that_starts_billing() -> "Agent[None, str]":
    script = [
        use("start", {"agent": "billing", "task": "Is INV-1001 paid?"}),
        text("Asked billing."),
        text("Billing says paid."),
    ]
    return agent(name="support", model=scripted_model({"responses": script}))


def test_a_rule_that_allows_start_opens_a_team_lists_the_agent_and_starts_it() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        agents = {"support": _support_that_starts_billing(), "billing": billing()}
        policy = [rule("support", "billing", ["start", "ask"])]
        async with served(agents, policy, store) as client:
            branch = await ran(client, "support", "Is INV-1001 paid?")
        events = await branch_events(store, branch)
        started = next(e for e in events if isinstance(e, ThreadStartedEvent))
        # A rule-only lead has no team of its own, so the rule alone opens one and fills the
        # listing.
        team = started.data.model_dump(mode="json")["team"]
        assert team is not None
        assert (
            "Agents you can start as team members with start: billing." in started.data.instructions
        )
        pinned = sorted(t.name for t in started.data.tools if t.name in TEAM_TOOLS)
        assert pinned == ["ask", "start"]
        assert any(isinstance(e, MemberStartedEvent) for e in events)
        sq = await open_store(scoped(store, "acme"))
        await assert_team_replays(sq, team["id"])

    run(main)


def test_the_rules_budget_is_the_started_members_own() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        agents = {"support": _support_that_starts_billing(), "billing": billing()}
        # A scripted model has no price, so a cost limit can bound no attempt; requests can.
        policy = [rule("support", "billing", ["start"], budget=Budget(max_model_requests=3))]
        async with served(agents, policy, store) as client:
            branch = await ran(client, "support", "Is INV-1001 paid?")
        events = await branch_events(store, branch)
        member = next(e for e in events if isinstance(e, MemberStartedEvent))
        assert member.data.model_dump(mode="json")["budget"] == {"max_model_requests": 3}
        # And it covers the member: every request of its turns reserves against the cap.
        sq = await open_store(scoped(store, "acme"))
        reserved = await sq.run(
            lambda c: c.execute("SELECT DISTINCT budget_id FROM budget_ledger").fetchall()
        )
        assert (f"start:{member.event_id}",) in reserved

    run(main)
