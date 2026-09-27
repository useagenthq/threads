"""The exposed side's kit: a host that has been through ready(), one call helper per binding, and
the readers a test asserts an answer with. Every assertion goes through the served ASGI app, so a
test exercises the routes a partner would. The fixture agents are a2a_agents.py. The Python twin of
typescript/packages/host/test/a2a/kit.ts."""

import asyncio
import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import httpx
from pydantic import JsonValue

from threads import Agent, Store, sqlite
from threads.a2a.protocol import A2A_VERSION, VERSION_HEADER, error_by_code
from threads.agents.store import open_store, scoped
from threads.host import Host, host
from threads.host.a2a.config import A2aOptions
from threads.log import BranchId, Principal, UserInputEvent
from threads.result import Ok
from threads.store.conn import Conn

type Agents = Mapping[str, Agent[None, object]]
"""One host's agents, as `served` takes them."""

ALICE: Final = Principal(issuer="partner", tenant="acme", subject="refunds.partner.example")
BOB: Final = Principal(issuer="partner", tenant="acme", subject="billing.partner.example")
BASE_URL: Final = "https://host.test"
"""Where these tests' cards say partners reach the host. Defaulted for every test, because only the
card tests care what it is; a test that does care passes its own `base_url`."""

ONE: Final[A2aOptions] = {
    "base_url": BASE_URL,
    "expose": {"support": {"description": "Support."}},
}


@dataclass(frozen=True, slots=True)
class Served:
    """One host, and how to call it in either binding."""

    host: Host
    store: Store
    client: httpx.AsyncClient

    def _headers(
        self, who: Principal | None, version: str | None, extra: Mapping[str, str]
    ) -> dict[str, str]:
        headers = dict(extra)
        if who is not None:
            headers["x-principal"] = who.model_dump_json()
        if version is not None:
            headers[VERSION_HEADER] = version
        return headers

    async def raw(  # noqa: PLR0913 - one request and everything a test may vary about it
        self,
        verb: str,
        path: str,
        *,
        as_: Principal | None = None,
        body: JsonValue | None = None,
        raw_body: str | None = None,
        version: str | None = A2A_VERSION,
        headers: Mapping[str, str] = {},
    ) -> httpx.Response:
        """Any path on the host, with no A2A framing: for the cards and the negative routes."""
        sent = self._headers(as_, version, headers)
        content = raw_body if raw_body is not None else (None if body is None else json.dumps(body))
        if content is not None:
            sent["content-type"] = "application/json"
        return await self.client.request(verb, path, headers=sent, content=content)

    async def rpc(
        self,
        method: str,
        params: JsonValue,
        *,
        as_: Principal | None = None,
        version: str | None = A2A_VERSION,
        rpc_id: JsonValue = 1,
    ) -> httpx.Response:
        """POST /a2a/support: the JSON-RPC binding."""
        body: JsonValue = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        return await self.raw("POST", "/a2a/support", as_=as_, body=body, version=version)

    async def http(  # noqa: PLR0913 - one request and everything a test may vary about it
        self,
        verb: str,
        path: str,
        *,
        as_: Principal | None = None,
        body: JsonValue | None = None,
        version: str | None = A2A_VERSION,
        headers: Mapping[str, str] = {},
    ) -> httpx.Response:
        """The HTTP+JSON binding, at a path under /a2a/support."""
        return await self.raw(
            verb, f"/a2a/support{path}", as_=as_, body=body, version=version, headers=headers
        )


@asynccontextmanager
async def served(
    agents: Agents,
    a2a: A2aOptions = ONE,
    *,
    store: Store | None = None,
    with_auth: bool = True,
) -> AsyncGenerator[Served]:
    """A host through ready(), with an httpx client on its ASGI app. Without `with_auth` the host
    has no authenticate at all, so every principal route is 401."""
    from starlette.requests import Request  # noqa: PLC0415 - starlette only when a host is served

    async def authenticate(request: Request) -> Principal | None:
        header = request.headers.get("x-principal")
        return None if header is None else Principal.model_validate_json(header)

    kept = sqlite(":memory:") if store is None else store
    served_host = host(
        store=kept,
        agents=agents,
        a2a=a2a,
        authenticate=authenticate if with_auth else None,
    )
    async with served_host:
        transport = httpx.ASGITransport(app=served_host.asgi)
        async with httpx.AsyncClient(transport=transport, base_url="https://host.test") as client:
            yield Served(served_host, kept, client)


def message(message_id: str, text: str, **extra: JsonValue) -> JsonValue:
    """A SendMessage request message with one text part."""
    body: dict[str, JsonValue] = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    return {"message": {**body, **extra}}


def result(response: httpx.Response) -> JsonValue:
    """A JSON-RPC answer's result. Fails loudly when the answer was an error."""
    body = _json(response)
    assert isinstance(body, dict), body
    assert "error" not in body, f"expected a result, got {body['error']}"
    return body["result"]


def task(response: httpx.Response) -> Mapping[str, JsonValue]:
    """The task a SendMessage answered, in either binding."""
    body = _json(response)
    value = body["result"] if isinstance(body, dict) and "result" in body else body
    if isinstance(value, dict) and "task" in value:
        value = value["task"]
    assert isinstance(value, dict), value
    assert "id" in value, value
    assert "status" in value, value
    return value


def state_of(found: Mapping[str, JsonValue]) -> str:
    status = found["status"]
    assert isinstance(status, dict), status
    assert isinstance(status["state"], str), status
    return status["state"]


def text_of(found: Mapping[str, JsonValue]) -> str:
    """The status message's text, joined; "" when the task carries no status message."""
    status = found.get("status")
    body = status.get("message") if isinstance(status, dict) else None
    parts = body.get("parts") if isinstance(body, dict) else None
    if not isinstance(parts, list):
        return ""
    said = [p.get("text") for p in parts if isinstance(p, dict)]
    return "".join(t for t in said if isinstance(t, str))


def artifacts_of(found: Mapping[str, JsonValue]) -> list[JsonValue]:
    artifacts = found.get("artifacts")
    return artifacts if isinstance(artifacts, list) else []


def fault_name(response: httpx.Response) -> str:
    """The A2A error name an answer carries, read from its code, in either binding."""
    body = _json(response)
    assert isinstance(body, dict), body
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else body.get("code")
    assert isinstance(code, int), body
    return error_by_code(code) or f"unknown code {code}"


def _json(response: httpx.Response) -> JsonValue:
    parsed: JsonValue = json.loads(response.text)
    return parsed


async def reaches(
    on: Served, who: Principal, task_id: str, wanted: Sequence[str], tries: int = 400
) -> Mapping[str, JsonValue]:
    """Polls GetTask until the task reaches one of `wanted`, and fails the test with the states it
    did see if it never does. Never skips: a run that does not get there is a failure, not a
    pass."""
    seen: list[str] = []
    for _ in range(tries):
        found = task(await on.rpc("GetTask", {"id": task_id}, as_=who))
        state = state_of(found)
        if not seen or seen[-1] != state:
            seen.append(state)
        if state in wanted:
            return found
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"task {task_id} never reached {' or '.join(wanted)}; it went {' -> '.join(seen)}"
    )


async def says(
    on: Served, who: Principal, task_id: str, text: str, tries: int = 400
) -> Mapping[str, JsonValue]:
    """Polls GetTask until the status message says `text`, reporting what it said instead."""
    seen: list[str] = []
    for _ in range(tries):
        found = task(await on.rpc("GetTask", {"id": task_id}, as_=who))
        said = f"{state_of(found)} {text_of(found)}".strip()
        if not seen or seen[-1] != said:
            seen.append(said)
        if text_of(found) == text:
            return found
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} never said {text!r}; it said {' -> '.join(seen)}")


def frames(response: httpx.Response) -> list[tuple[str | None, JsonValue]]:
    """(id, data) per SSE message, read to the end."""
    out: list[tuple[str | None, JsonValue]] = []
    for block in response.text.strip().split("\n\n"):
        if "data: " not in block:
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((fields.get("id"), json.loads(fields["data"])))
    return out


def item_of(data: JsonValue) -> Mapping[str, JsonValue]:
    """A frame's StreamResponse, out of the JSON-RPC envelope when it is in one."""
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    assert isinstance(data, dict), data
    return data


def shape(read: Sequence[tuple[str | None, JsonValue]]) -> list[str]:
    """Each frame's kind and, for a status update, its state: the stream's shape without its ids."""
    kinds: list[str] = []
    for _, data in read:
        item = item_of(data)
        status = item.get("statusUpdate")
        if "task" in item:
            kinds.append("task")
        elif isinstance(status, dict):
            inner = status.get("status")
            state = inner.get("state") if isinstance(inner, dict) else None
            kinds.append(f"status:{state}")
        elif "artifactUpdate" in item:
            kinds.append("artifact")
        else:
            kinds.append("other")
    return kinds


def _count(conn: Conn) -> int:
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'user_input'", ()).fetchone()
    assert row is not None
    found = row[0]
    assert isinstance(found, int)
    return found


async def user_inputs(store: Store, tenant: str = ALICE.tenant) -> int:
    """Every user_input in the store: how many runs really started. Counting rows rather than
    comparing task ids is what would actually catch a duplicate delivery."""
    sq = await open_store(scoped(store, tenant))
    return await sq.run(_count, read_only=True)


def _branches(conn: Conn) -> list[str]:
    rows = conn.execute("SELECT DISTINCT branch_id FROM events", ()).fetchall()
    return [str(r[0]) for r in rows]


async def recorded_inputs(store: Store, tenant: str = ALICE.tenant) -> list[UserInputEvent]:
    """Every user_input in the store, parsed: what a run really committed."""
    sq = await open_store(scoped(store, tenant))
    found: list[UserInputEvent] = []
    for branch in await sq.run(_branches, read_only=True):
        read = await sq.read(BranchId(branch), 0)
        if isinstance(read, Ok):
            found.extend(e for e in read.value.fold.events if isinstance(e, UserInputEvent))
    return found
