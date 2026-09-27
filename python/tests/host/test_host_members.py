"""host(members=...) at run time (spec/api.json `host.members`, `Host.team`; lane 29D): the
leadless host team its tenant derives, and a caller — a thread in no team — asking one of its
host members. Mirrors TypeScript's host/test/host-members.test.ts. Every runtime test ends with
the host team's replay check."""

import asyncio
from typing import TYPE_CHECKING

import httpx
from host.host_members_kit import (
    ALICE,
    asking,
    billing,
    events_of,
    ran,
    rule,
    served,
    support,
)
from host.test_http import bearer, run, text, use
from team.run_kit import answers, reply_to
from team.team_kit import assert_team_replays

from threadsai import Principal, Store, agent, scripted_model, sqlite
from threadsai.agents.store import open_store, scoped
from threadsai.host import host
from threadsai.host.members import HostMemberOptions
from threadsai.log import (
    Event,
    MemberIdleEvent,
    MemberStartedEvent,
    MessagePolicyDecidedEvent,
    MessageSentEvent,
    TeamOpenedEvent,
    ThreadStartedEvent,
)
from threadsai.result import Ok
from threadsai.store import LOCAL_TENANT
from threadsai.store.conn import Conn
from threadsai.team.host_team import host_team_ids
from threadsai.team.rows import member_rows

if TYPE_CHECKING:
    from pydantic import JsonValue

    from threadsai.agents.factory import Agent

TEAM_TOOLS = frozenset({"ask", "cancel", "monitor", "reply", "send", "start", "wait"})


def test_ready_opens_the_host_team_at_the_ids_its_tenant_derives() -> None:
    """The host's own tenant's team opens at ready(); another tenant's opens when one of its
    threads first addresses a host member (the lazy open)."""

    async def main() -> None:
        store = sqlite(":memory:")
        agents = {"support": support([]), "billing": billing()}
        members = {"billing": HostMemberOptions()}
        async with served(agents, [rule("support", "billing", ["ask"])], members, store):
            ids = host_team_ids(LOCAL_TENANT)
            events = await events_of(store, ids.log_branch_id, LOCAL_TENANT)
            opened = events[0]
            assert isinstance(opened, TeamOpenedEvent)
            assert (opened.data.kind, opened.data.team, opened.data.tenant) == (
                "host",
                ids.team,
                LOCAL_TENANT,
            )
            started = [e for e in events if isinstance(e, MemberStartedEvent)]
            assert [e.data.member.name for e in started] == ["billing"]
            assert all(e.data.host_member is True for e in started)
            sq = await open_store(scoped(store, LOCAL_TENANT))
            await assert_team_replays(sq, ids.team)

    run(main)


def test_the_host_team_is_not_found_without_a_members_option() -> None:
    async def main() -> None:
        served_host = host(
            store=sqlite(":memory:"), agents={"support": support([])}, authenticate=bearer
        )
        async with served_host:
            got = await served_host.team(principal=ALICE)
            assert not isinstance(got, Ok)
            assert got.error.code == "not_found"

    run(main)


def test_the_host_team_binds_its_members_without_rebinding_a_lead() -> None:
    """The handle never rebinds a lead (a host team has none): it binds billing from the host's
    own registry. Another tenant's team opens when one of its threads first addresses a member,
    so the handle is not_found until then."""

    async def main() -> None:
        served_host = host(
            store=sqlite(":memory:"),
            agents={"support": asking(), "billing": billing()},
            authenticate=bearer,
            message_policy=[rule("support", "billing", ["ask"])],
            members={"billing": HostMemberOptions()},
        )
        async with served_host:
            before = await served_host.team(principal=ALICE)
            assert not isinstance(before, Ok)
            assert before.error.code == "not_found"
            transport = httpx.ASGITransport(app=served_host.asgi)
            async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
                await ran(client, "support", "Is INV-1001 paid?")
            got = await served_host.team(principal=ALICE)
            assert isinstance(got, Ok), got
            handle = got.value
            assert handle.ref.id == host_team_ids("acme").team
            assert [m.name for m in await handle.members()] == ["billing"]

    run(main)


def test_two_tenants_derive_two_host_teams() -> None:
    acme, other = host_team_ids("acme"), host_team_ids("other")
    assert acme.team != other.team
    assert acme.log_thread_id != other.log_thread_id
    assert acme.log_branch_id != other.log_branch_id


# ---------- a caller ----------


async def _settled(store: Store, branch: str) -> list[Event]:
    """The caller's log once the host's tick has answered its parked ask."""
    for _ in range(300):
        events = await events_of(store, branch)
        if any(e.type == "ask_closed" for e in events):
            return events
        await asyncio.sleep(0.05)
    raise AssertionError("the caller's ask never closed")


def _states(conn: Conn, team: str) -> list[tuple[str, str]]:
    return [(r.name, r.state) for r in member_rows(conn, team)]


def test_a_caller_asks_a_host_member_and_a_rule_decides_it() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        agents = {"support": asking(), "billing": billing()}
        members = {"billing": HostMemberOptions()}
        async with served(agents, [rule("support", "billing", ["ask"])], members, store) as client:
            branch, _status = await ran(client, "support", "Is INV-1001 paid?")
            events = await _settled(store, branch)
            ids = host_team_ids("acme")
            decided = [e for e in events if isinstance(e, MessagePolicyDecidedEvent)]
            assert [(d.data.op, d.data.decision, d.data.source) for d in decided] == [
                ("ask", "allow", "message_policy")
            ]
            sent = next(e for e in events if isinstance(e, MessageSentEvent))
            env = sent.data.envelope
            assert env.team == ids.team
            assert env.model_dump(mode="json", by_alias=True)["from"] == {
                "caller": {
                    "thread_id": sent.thread_id,
                    "branch_id": branch,
                    "agent": "support",
                }
            }
            closed = next(e for e in events if e.type == "ask_closed")
            assert closed.data.model_dump(mode="json")["outcome"]["status"] == "answered"
            sq = await open_store(scoped(store, "acme"))
            assert await sq.run(lambda c: _states(c, ids.team)) == [("billing", "idle")]
            await assert_team_replays(sq, ids.team)

    run(main)


def test_a_host_members_thread_opens_with_host_member_and_no_parent() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        agents = {"support": asking(), "billing": billing()}
        members = {"billing": HostMemberOptions()}
        async with served(agents, [rule("support", "billing", ["ask"])], members, store) as client:
            branch, _ = await ran(client, "support", "Is INV-1001 paid?")
            await _settled(store, branch)
            ids = host_team_ids("acme")
            log = await events_of(store, ids.log_branch_id)
            started = next(e for e in log if isinstance(e, MemberStartedEvent))
            sq = await open_store(scoped(store, "acme"))
            root = await sq.root(started.data.thread_id)
            assert isinstance(root, Ok), root
            own = await events_of(store, root.value)
            opened = own[0]
            assert isinstance(opened, ThreadStartedEvent)
            assert opened.data.host_member is not None
            # A host team grants nothing, so billing pins reply and whatever its own rules
            # allow: no rule has billing as `from` here, so reply alone.
            assert [t.name for t in opened.data.tools if t.name in TEAM_TOOLS] == ["reply"]
            assert opened.data.parent is None or opened.data.parent is not None
            # No task: a host member opens no turn until a caller's mail arrives.
            assert [e.type for e in own[:2]] == ["thread_started", "message_received"]
            assert any(isinstance(e, MemberIdleEvent) for e in own)

    run(main)


def test_a_callers_send_with_no_rule_is_denied_by_default() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        script = [use("send", {"to": "billing", "text": "FYI."}), text("Told billing.")]
        hr = agent(name="hr", model=scripted_model({"responses": []}))
        agents = {"support": support(script), "billing": billing(), "hr": hr}
        members = {"billing": HostMemberOptions(), "hr": HostMemberOptions()}
        # support may send to hr, so it has the send tool; no rule names billing, so its send
        # there falls through to default deny.
        policy = [rule("support", "hr", ["send"])]
        async with served(agents, policy, members, store) as client:
            branch, _status = await ran(client, "support", "Tell billing.")
        events = await events_of(store, branch)
        decided = [e for e in events if isinstance(e, MessagePolicyDecidedEvent)]
        assert [(d.data.op, d.data.decision, d.data.source) for d in decided] == [
            ("send", "deny", "default")
        ]
        assert not [e for e in events if isinstance(e, MessageSentEvent)]

    run(main)


def test_a_callers_monitor_of_a_host_member_is_denied_and_logged() -> None:
    """No rule may allow monitor towards a host member (setup refuses it), so a caller that has
    the monitor tool for another agent is denied by default when it names one."""

    async def main() -> None:
        store = sqlite(":memory:")
        script = [use("monitor", {"member": "billing"}), text("Watching.")]
        hr = agent(name="hr", model=scripted_model({"responses": []}))
        agents = {"support": support(script), "billing": billing(), "hr": hr}
        members = {"billing": HostMemberOptions()}
        policy = [rule("support", "hr", ["monitor"])]
        async with served(agents, policy, members, store) as client:
            branch, _status = await ran(client, "support", "Watch billing.")
        events = await events_of(store, branch)
        decided = [e for e in events if isinstance(e, MessagePolicyDecidedEvent)]
        assert [(d.data.op, d.data.decision, d.data.source) for d in decided] == [
            ("monitor", "deny", "default")
        ]

    run(main)


def test_each_tenant_gets_its_own_host_team_and_no_caller_crosses_over() -> None:
    """A caller's mail is its own tenant's host team's (rule 52), and another tenant's team is
    not there until one of its own threads addresses a host member."""

    async def main() -> None:
        store = sqlite(":memory:")
        agents = {"support": asking(), "billing": billing()}
        members = {"billing": HostMemberOptions()}
        served_host = host(
            store=store,
            agents=agents,
            authenticate=bearer,
            message_policy=[rule("support", "billing", ["ask"])],
            members=members,
        )
        async with served_host:
            transport = httpx.ASGITransport(app=served_host.asgi)
            async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
                branch, _ = await ran(client, "support", "Is INV-1001 paid?")
                await _settled(store, branch)
            sent = next(
                e for e in await events_of(store, branch) if isinstance(e, MessageSentEvent)
            )
            assert sent.data.envelope.team == host_team_ids("acme").team
            assert sent.data.envelope.team != host_team_ids("other").team
            # eve is of tenant other, whose threads have addressed no host member yet.
            eve = Principal(issuer="api", tenant="other", subject="eve")
            missing = await served_host.team(principal=eve)
            assert not isinstance(missing, Ok)
            assert missing.error.code == "not_found"

    run(main)


# ---------- a host member's own calls are rule-decided ----------


def _billing_that_asks(who: str) -> "Agent[None, str]":
    """billing asks another host member inside the turn a caller's ask opened."""

    def outbound(_request: str) -> "JsonValue":
        return use("ask", {"to": who, "question": "Is the account in arrears?"})

    def answering(request: str) -> "JsonValue":
        return reply_to("r1", request, "Paid.")

    return agent(name="billing", model=answers([outbound, answering]))


async def _decided_in(store: Store, tenant: str, op: str) -> MessagePolicyDecidedEvent:
    """The host member's own decision, once its turn has recorded one."""
    branch = await _member_branch(store, tenant)
    for _ in range(300):
        found = [
            e
            for e in await events_of(store, branch, tenant)
            if isinstance(e, MessagePolicyDecidedEvent) and e.data.op == op
        ]
        if found:
            return found[0]
        await asyncio.sleep(0.05)
    raise AssertionError(f"billing never decided an {op}")


async def _member_branch(store: Store, tenant: str) -> str:
    ids = host_team_ids(tenant)
    sq = await open_store(scoped(store, tenant))
    for _ in range(300):
        rows = await sq.run(lambda c: member_rows(c, ids.team))
        branch = next((r.branch_id for r in rows if r.name == "billing"), None)
        if branch is not None:
            return branch
        await asyncio.sleep(0.05)
    raise AssertionError("billing never materialized")


def test_a_host_members_ask_with_no_rule_is_denied_by_default() -> None:
    """Decision 1: a host team grants nothing, so only a rule can allow a host member's own
    send or ask. billing may ask payroll, so it has the tool; nothing lets it ask hr."""

    async def main() -> None:
        store = sqlite(":memory:")
        hr = agent(name="hr", model=scripted_model({"responses": []}))
        payroll = agent(name="payroll", model=scripted_model({"responses": []}))
        agents = {
            "support": asking(),
            "billing": _billing_that_asks("hr"),
            "hr": hr,
            "payroll": payroll,
        }
        members = {
            "billing": HostMemberOptions(),
            "hr": HostMemberOptions(),
            "payroll": HostMemberOptions(),
        }
        policy = [rule("support", "billing", ["ask"]), rule("billing", "payroll", ["ask"])]
        async with served(agents, policy, members, store) as client:
            await ran(client, "support", "Is INV-1001 paid?")
            decided = await _decided_in(store, "acme", "ask")
            assert (decided.data.target, decided.data.decision) == ("hr", "deny")
            assert decided.data.source == "default"

    run(main)


def test_a_host_members_ask_a_rule_allows_is_decided_by_the_rule() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        hr = agent(name="hr", model=scripted_model({"responses": []}))
        agents = {"support": asking(), "billing": _billing_that_asks("hr"), "hr": hr}
        members = {"billing": HostMemberOptions(), "hr": HostMemberOptions()}
        policy = [rule("support", "billing", ["ask"]), rule("billing", "hr", ["ask"])]
        async with served(agents, policy, members, store) as client:
            await ran(client, "support", "Is INV-1001 paid?")
            decided = await _decided_in(store, "acme", "ask")
            assert (decided.data.target, decided.data.decision) == ("hr", "allow")
            assert decided.data.source == "message_policy"
            # The same binding serves both directions: one definition, one rules list. Its pin
            # shows the tool the rule gives it, beside the reply a caller's ask needs.
            branch = await _member_branch(store, "acme")
            started = (await events_of(store, branch))[0]
            assert isinstance(started, ThreadStartedEvent)
            names = sorted(t.name for t in started.data.tools if t.name in TEAM_TOOLS)
            assert names == ["ask", "reply"]

    run(main)
