"""Controls on a subagent's thread through the host (spec/schema/README.md, "Subagent
cancellation and parking"): a run whose child parks is parked on {kind: child}; the child's
challenge is answered on the child's thread by the root agent's approvers, and the host then
resumes the root thread, which finishes."""

import asyncio
from http import HTTPStatus

import httpx
import pytest
from pydantic import BaseModel, JsonValue
from starlette.requests import Request

from threads import RunContext, agent, scripted_model, sqlite, tool
from threads.host import host
from threads.log import ParkAddress, Principal

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALICE = Principal(issuer="api", tenant="acme", subject="alice")
BOB = Principal(issuer="api", tenant="acme", subject="bob")


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "call_1", "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def bearer(request: Request) -> Principal | None:
    return {"Bearer alice": ALICE, "Bearer bob": BOB}.get(request.headers.get("authorization", ""))


class Note(BaseModel):
    text: str


@pytest.mark.parametrize("approvers", [[ALICE], None])
def test_a_childs_approval_is_answered_on_its_thread_and_the_root_resumes(
    approvers: list[Principal] | None,
) -> None:
    """Configured or not, the root agent's policy governs the child's challenge: with none
    configured, the root run's requester (alice) may answer it and bob may not."""
    sent: list[str] = []

    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        return "sent"

    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    worker = agent(
        name="worker",
        model=scripted_model({"responses": [use("send", {"text": "x"}), text("Sent.")]}),
        tools=[send_tool],
    )
    spawn = use("spawn_agent", {"agent": "worker", "prompt": "Send it."})
    model = scripted_model({"responses": [spawn, text("All done.")]})
    lead = (
        agent(model=model, tools=[send_tool], subagents=[worker])
        if approvers is None
        else agent(model=model, tools=[send_tool], subagents=[worker], approvers=approvers)
    )
    auth = {"authorization": "Bearer alice"}

    async def main() -> None:
        served = host(store=sqlite(":memory:"), agents={"lead": lead}, authenticate=bearer)
        async with served:
            transport = httpx.ASGITransport(app=served.asgi)
            async with httpx.AsyncClient(transport=transport, base_url="http://h") as client:
                body: JsonValue = {"agent": "lead", "input": "go"}
                receipt = (
                    await client.post(
                        "/v1/runs", json=body, headers=auth | {"idempotency-key": "k"}
                    )
                ).json()
                follow = f"/v1/threads/{receipt['thread_id']}/runs/{receipt['run_id']}/events"
                parked = _result(await client.get(follow, headers=auth))
                assert parked.status == "parked", parked
                (address,) = parked.pending
                assert address.kind == "child"
                child = f"/v1/threads/{address.id}"
                (challenge,) = (await client.get(f"{child}/approvals", headers=auth)).json()
                decide = f"{child}/approvals/{challenge['challenge_id']}"
                bob = {"authorization": "Bearer bob"}
                refused = await client.post(decide, json={"decision": "grant"}, headers=bob)
                assert refused.status_code == HTTPStatus.FORBIDDEN, refused.text
                granted = await client.post(decide, json={"decision": "grant"}, headers=auth)
                assert granted.status_code == HTTPStatus.OK, granted.text
                # The root resumes in the background: until its run records the child's
                # result, a subscriber still reads the park.
                done = parked
                for _ in range(100):
                    done = _result(await client.get(follow, headers=auth))
                    if done.status != "parked":
                        break
                    await asyncio.sleep(0.01)
                assert (done.status, done.output) == ("completed", "All done."), done

    asyncio.run(main())
    assert sent == ["x"]


class _Result(BaseModel):
    status: str
    output: str | None = None
    pending: list[ParkAddress] = []


class _Message(BaseModel):
    result: _Result


def _result(response: httpx.Response) -> _Result:
    """The run's result: the stream's last SSE message."""
    last = response.text.strip().split("\n\n")[-1]
    data = next(line for line in last.splitlines() if line.startswith("data: "))
    return _Message.model_validate_json(data.removeprefix("data: ")).result
