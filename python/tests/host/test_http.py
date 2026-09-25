"""The host HTTP API (spec/schema/host-api/openapi.json) end to end on the scripted model: the
authenticated principal on every record, tenant scoping, the idempotent 202 receipt, the SSE
stream bound to its run, and single-use approvals that resume the run."""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from http import HTTPStatus

import httpx
from pydantic import BaseModel, JsonValue
from starlette.requests import Request

from threads import Agent, RunContext, Store, agent, scripted_model, sqlite, tool
from threads.agents.store import now_ms, open_store, scoped
from threads.host import Host, host
from threads.log import BranchId, Principal, UserInputEvent
from threads.result import Ok
from threads.store.conn import Conn

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALICE = Principal(issuer="api", tenant="acme", subject="alice")
BOB = Principal(issuer="api", tenant="acme", subject="bob")
EVE = Principal(issuer="api", tenant="other", subject="eve")
TOKENS = {"alice": ALICE, "bob": BOB, "eve": EVE}


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "call_1", "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def bearer(request: Request) -> Principal | None:
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    return TOKENS.get(token)


class Note(BaseModel):
    text: str


def sender(sent: list[str]) -> Agent[None, str]:
    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        return "sent"

    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    script: JsonValue = {"responses": [use("send", {"text": "x"}), text("Sent it.")]}
    return agent(model=scripted_model(script), tools=[send_tool], approvers=[ALICE])


@asynccontextmanager
async def served(
    bot: Agent[None, str], *, auth: bool = True, store: Store | None = None
) -> AsyncGenerator[httpx.AsyncClient]:
    served_host = host(
        store=sqlite(":memory:") if store is None else store,
        agents={"support": bot},
        authenticate=bearer if auth else None,
    )
    async with served_host:
        transport = httpx.ASGITransport(app=served_host.asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
            yield client


def as_(who: str) -> dict[str, str]:
    return {"authorization": f"Bearer {who}"}


async def start(client: httpx.AsyncClient, who: str, key: str, body: JsonValue) -> httpx.Response:
    headers = as_(who) | {"idempotency-key": key}
    return await client.post("/v1/runs", json=body, headers=headers)


def sse(response: httpx.Response) -> list[tuple[str | None, JsonValue]]:
    messages: list[tuple[str | None, JsonValue]] = []
    for block in response.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        messages.append((fields.get("id"), json.loads(fields["data"])))
    return messages


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def test_without_authenticate_every_v1_route_is_401() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": [text("Hi.")]}))
        async with served(bot, auth=False) as client:
            answered = await start(client, "alice", "k1", {"agent": "support", "input": "hi"})
            assert answered.status_code == HTTPStatus.UNAUTHORIZED
            assert answered.json()["error"]["code"] == "unauthenticated"

    run(main)


def test_a_run_is_accepted_once_per_key_and_bound_to_its_principal() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": [text("Hello!")]}))
        body: JsonValue = {"agent": "support", "input": "hi"}
        async with served(bot) as client:
            assert (
                await client.post("/v1/runs", json=body, headers=as_("alice"))
            ).status_code == HTTPStatus.BAD_REQUEST
            first = await start(client, "alice", "k1", body)
            assert first.status_code == HTTPStatus.ACCEPTED
            receipt = first.json()
            assert set(receipt) == {"thread_id", "branch_id", "run_id"}
            replay = await start(client, "alice", "k1", body)
            assert (replay.status_code, replay.json()) == (HTTPStatus.ACCEPTED, receipt)
            reused = await start(client, "alice", "k1", {"agent": "support", "input": "other"})
            assert (reused.status_code, reused.json()["error"]["code"]) == (
                HTTPStatus.CONFLICT,
                "idempotency_key_reused",
            )
            stranger = await start(client, "bob", "k1", body)
            assert stranger.status_code == HTTPStatus.CONFLICT
            assert stranger.json() == {
                "error": {
                    "code": "idempotency_key_principal_mismatch",
                    "message": "the key belongs to another principal",
                }
            }
            events = await client.get(
                f"/v1/threads/{receipt['thread_id']}/runs/{receipt['run_id']}/events",
                headers=as_("alice"),
            )
            messages = sse(events)
            ids = [m[0] for m in messages[:-1]]
            assert all(i is not None and i.isdigit() for i in ids)
            assert messages[0][1]["event"]["event_id"] == receipt["run_id"]  # type: ignore[index] - JSON
            assert messages[-1] == (
                None,
                {
                    "kind": "result",
                    "run_id": receipt["run_id"],
                    "result": {
                        "status": "completed",
                        "thread_id": receipt["thread_id"],
                        "branch_id": receipt["branch_id"],
                        "output": "Hello!",
                    },
                },
            )
            thread = receipt["thread_id"]
            foreign = await client.get(f"/v1/threads/{thread}/timeline", headers=as_("eve"))
            assert foreign.status_code == HTTPStatus.NOT_FOUND
            timeline = await client.get(f"/v1/threads/{thread}/timeline", headers=as_("alice"))
            inputs = [
                e["event"] for e in timeline.json()["entries"] if e["event"]["type"] == "user_input"
            ]
            assert [i["actor"]["principal"] for i in inputs] == [ALICE.model_dump()]

    run(main)


def test_an_approval_over_http_is_single_use_and_resumes_the_run() -> None:
    sent: list[str] = []

    async def main() -> None:
        async with served(sender(sent)) as client:
            receipt = (
                await start(client, "alice", "k", {"agent": "support", "input": "go"})
            ).json()
            thread, run_id = receipt["thread_id"], receipt["run_id"]
            follow = f"/v1/threads/{thread}/runs/{run_id}/events"
            parked = sse(await client.get(follow, headers=as_("alice")))[-1][1]
            assert parked["result"]["status"] == "parked"  # type: ignore[index] - JSON
            listed = (
                await client.get(f"/v1/threads/{thread}/approvals", headers=as_("alice"))
            ).json()
            (challenge,) = listed
            decide = f"/v1/threads/{thread}/approvals/{challenge['challenge_id']}"
            grant: JsonValue = {"decision": "grant"}
            refused = await client.post(decide, json=grant, headers=as_("bob"))
            assert (refused.status_code, refused.json()["error"]["code"]) == (
                HTTPStatus.FORBIDDEN,
                "forbidden",
            )
            granted = await client.post(decide, json=grant, headers=as_("alice"))
            assert granted.status_code == HTTPStatus.OK
            assert set(granted.json()) == {"event_id"}
            again = await client.post(decide, json=grant, headers=as_("alice"))
            assert (again.status_code, again.json()["error"]["code"]) == (
                HTTPStatus.CONFLICT,
                "approval_duplicate",
            )
            done = sse(await client.get(follow, headers=as_("alice")))[-1][1]
            assert done["result"]["status"] == "completed"  # type: ignore[index] - JSON

    run(main)
    assert sent == ["x"]


def test_the_log_records_the_authenticated_principal_never_the_local_operator() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": [text("Hi.")]}))
        store = sqlite(":memory:")
        served_host: Host = host(store=store, agents={"support": bot}, authenticate=bearer)
        async with served_host:
            request_body = {"agent": "support", "input": "hi"}
            from threads._generated.host_api_v1 import StartRunRequest  # noqa: PLC0415

            accepted = await served_host.start_run(
                StartRunRequest.model_validate(request_body), principal=BOB, idempotency_key="k"
            )
            assert isinstance(accepted, Ok)
            sq = await open_store(scoped(store, "acme"))
            read = await sq.read(accepted.value.branch_id, 0)
            assert isinstance(read, Ok)
            inputs: Sequence[UserInputEvent] = [
                e for e in read.value.fold.events if isinstance(e, UserInputEvent)
            ]
            assert [i.actor.principal for i in inputs] == [BOB]

    run(main)


def test_thread_routes_are_scoped_parsed_and_recorded() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": [text("Hi.")]}))
        async with served(bot) as client:
            receipt = (
                await start(client, "alice", "k", {"agent": "support", "input": "hi"})
            ).json()
            thread, run_id = receipt["thread_id"], receipt["run_id"]
            sse(
                await client.get(f"/v1/threads/{thread}/runs/{run_id}/events", headers=as_("alice"))
            )
            base = f"/v1/threads/{thread}"
            foreign = await client.get(f"{base}/runs/{run_id}/events", headers=as_("eve"))
            assert foreign.status_code == HTTPStatus.NOT_FOUND
            bad = await client.post(f"{base}/mode", json={"mode": "loud"}, headers=as_("alice"))
            assert (bad.status_code, bad.json()["error"]["code"]) == (
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
            )
            moved = await client.post(f"{base}/mode", json={"mode": "plan"}, headers=as_("alice"))
            assert moved.status_code == HTTPStatus.OK
            stopped = await client.post(f"{base}/cancel", headers=as_("bob"))
            assert stopped.status_code == HTTPStatus.OK
            listed = await client.get(f"{base}/branches", headers=as_("alice"))
            (branch,) = listed.json()
            assert (branch["branch_id"], branch["runnable"]) == (receipt["branch_id"], True)
            wrong = await client.get(
                f"{base}/timeline", params={"branch_id": run_id}, headers=as_("alice")
            )
            assert wrong.status_code == HTTPStatus.NOT_FOUND
            timeline = (await client.get(f"{base}/timeline", headers=as_("alice"))).json()
            last = timeline["entries"][-1]["event"]
            assert (last["type"], last["actor"]["principal"]["subject"]) == (
                "cancel_requested",
                "bob",
            )

    run(main)


def test_a_control_on_a_branch_another_process_holds_is_branch_busy() -> None:
    """The branch's lease is held elsewhere (another host process): every control route that
    appends answers 409 branch_busy and records nothing; the challenge stays open."""

    async def main() -> None:
        store = sqlite(":memory:")
        served_host = host(store=store, agents={"support": sender([])}, authenticate=bearer)
        async with served_host:
            transport = httpx.ASGITransport(app=served_host.asgi)
            async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
                receipt = (
                    await start(client, "alice", "k", {"agent": "support", "input": "hi"})
                ).json()
                base = f"/v1/threads/{receipt['thread_id']}"
                events = f"{base}/runs/{receipt['run_id']}/events"
                sse(await client.get(events, headers=as_("alice")))
                (challenge,) = (await client.get(f"{base}/approvals", headers=as_("alice"))).json()
                sq = await open_store(scoped(store, "acme"))
                held = await sq.acquire(BranchId(receipt["branch_id"]), "elsewhere", now_ms)
                assert isinstance(held, Ok)
                before = len(held.value.fold.events)
                routes: list[tuple[str, JsonValue]] = [
                    ("/cancel", None),
                    ("/mode", {"mode": "plan"}),
                    ("/settings", {"model": {"provider": "scripted", "name": "other"}}),
                    (f"/approvals/{challenge['challenge_id']}", {"decision": "grant"}),
                    (f"/approvals/{challenge['challenge_id']}", {"decision": "deny"}),
                    ("/questions/call_1/answer", {"answer": "yes"}),
                    ("/parked/b:call_1/resolve", {"resolution": "assume_done"}),
                ]
                for path, payload in routes:
                    answered = await client.post(base + path, json=payload, headers=as_("alice"))
                    got = (answered.status_code, answered.json()["error"]["code"])
                    assert got == (HTTPStatus.CONFLICT, "branch_busy"), path
                read = await sq.read(held.value.branch_id, now_ms())
                assert isinstance(read, Ok)
                assert len(read.value.fold.events) == before
                still = (await client.get(f"{base}/approvals", headers=as_("alice"))).json()
                assert still == [challenge]

    run(main)


def test_a_log_from_a_newer_writer_answers_unsupported_critical_event_not_not_found() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model({"responses": [text("Hi.")]}))
        async with served(bot, store=store) as client:
            receipt = (
                await start(client, "alice", "k", {"agent": "support", "input": "hi"})
            ).json()
            thread, run_id = receipt["thread_id"], receipt["run_id"]
            sse(
                await client.get(f"/v1/threads/{thread}/runs/{run_id}/events", headers=as_("alice"))
            )
            sql = (
                "UPDATE events SET line = CAST(replace(CAST(line AS TEXT), ?, ?) AS BLOB)"
                " WHERE branch_id = ?"
            )
            newer = ('"type":"turn_completed"', '"type":"approval_quorum"', receipt["branch_id"])

            def tamper(c: Conn) -> None:
                c.execute(sql, newer)

            await (await open_store(store)).run(tamper)
            timeline = await client.get(f"/v1/threads/{thread}/timeline", headers=as_("alice"))
            assert (timeline.status_code, timeline.json()["error"]["code"]) == (
                HTTPStatus.CONFLICT,
                "unsupported_critical_event",
            )

    run(main)
