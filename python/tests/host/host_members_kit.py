"""Shared by the host-members tests (lane 29D): the agents, the served host and the run helper
they all need."""

import contextlib
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import TYPE_CHECKING

import httpx
from host.test_http import as_, bearer, sse, start, text, use
from pydantic import TypeAdapter
from team.run_kit import answers, reply_to, say

from threads import MessagePolicyRule, Principal, agent, scripted_model, sqlite
from threads.agents.store import Store, open_store, scoped
from threads.host import host
from threads.host.members import HostMemberOptions
from threads.log import BranchId, Event
from threads.result import Ok

if TYPE_CHECKING:
    from pydantic import JsonValue

    from threads.agents.factory import Agent

ALICE = Principal(issuer="api", tenant="acme", subject="alice")


def rule(sender: str, to: str, allow: Sequence[str]) -> MessagePolicyRule:
    return {"from": sender, "to": to, "allow": [*allow]}  # pyright: ignore[reportReturnType]


def billing(answer: str = "Paid.") -> "Agent[None, str]":
    """A host member that replies to the ask it took, then ends its turn idle. Its reply names
    an ask id that only exists at run time, so its answers are made from its own request."""

    def replying(request: str) -> "JsonValue":
        return reply_to("r1", request, answer)

    def done(_request: str) -> "JsonValue":
        return say("Answered.")

    return agent(name="billing", model=answers([replying, done]))


def support(script: "Sequence[JsonValue]") -> "Agent[None, str]":
    return agent(name="support", model=scripted_model({"responses": list(script)}))


def asking(question: str = "Is INV-1001 paid?") -> "Agent[None, str]":
    script = [use("ask", {"to": "billing", "question": question}), text("Billing answered.")]
    return support(script)


def serving(
    agents: "Mapping[str, Agent[None, str]]",
    members: Mapping[str, HostMemberOptions],
    policy: Sequence[MessagePolicyRule] = (),
) -> None:
    """A host built with these members: what the setup refusals raise from."""
    host(store=sqlite(":memory:"), agents=agents, message_policy=policy, members=members)


@contextlib.asynccontextmanager
async def served(
    agents: "Mapping[str, Agent[None, str]]",
    policy: Sequence[MessagePolicyRule],
    members: Mapping[str, HostMemberOptions],
    store: Store,
) -> AsyncGenerator[httpx.AsyncClient]:
    served_host = host(
        store=store, agents=agents, authenticate=bearer, message_policy=policy, members=members
    )
    async with served_host:
        transport = httpx.ASGITransport(app=served_host.asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
            yield client


async def ran(
    client: httpx.AsyncClient, name: str, prompt: str, who: str = "alice"
) -> tuple[str, str]:
    """Starts a run of `name` as `who`, follows it to its end, and returns (branch, status)."""
    body: JsonValue = {"agent": name, "input": prompt}
    receipt = _ids((await start(client, who, f"k-{name}-{who}", body)).json())
    follow = f"/v1/threads/{receipt['thread_id']}/runs/{receipt['run_id']}/events"
    last = sse(await client.get(follow, headers=as_(who)))[-1][1]
    assert isinstance(last, dict)
    result = last["result"]
    assert isinstance(result, dict), result
    status = result["status"]
    assert isinstance(status, str)
    return receipt["branch_id"], status


def _ids(body: object) -> dict[str, str]:
    return TypeAdapter(dict[str, str]).validate_python(body)


async def events_of(store: Store, branch: str, tenant: str = "acme") -> list[Event]:
    sq = await open_store(scoped(store, tenant))
    read = await sq.read(BranchId(branch), 0)
    assert isinstance(read, Ok), read
    return list(read.value.fold.events)
