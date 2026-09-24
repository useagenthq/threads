"""The shared E2B wire vector (spec/conformance/vectors/e2b-wire/cases.json), replayed against this
adapter's control plane and envd clients over their fenced transports. The TypeScript adapter
replays the same file (typescript/packages/e2b/test/wire.test.ts), so both speak one wire."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import cast

import httpx
import pytest
from e2b.api import AsyncApiClient
from e2b.connection_config import ConnectionConfig
from e2b_fake import TracedTransport
from pydantic import JsonValue
from pyqwest.testing import ASGITransport
from sandbox_backend import FakeBackend
from sandbox_kit import OPEN

from threads.adapters.sandboxes.e2b import wire
from threads.adapters.sandboxes.e2b.control import MAX_PAGES, Control
from threads.adapters.sandboxes.e2b.envd import Envd, Transports
from threads.adapters.sandboxes.e2b.session import call
from threads.adapters.sandboxes.e2b.transport import FencedHttpx
from threads.adapters.sandboxes.streams import StreamLostError
from threads.result import Err

VECTOR_PATH = Path(__file__).resolve().parents[4] / "spec/conformance/vectors/e2b-wire/cases.json"
type Obj = dict[str, JsonValue]
VECTOR: Obj = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))


def _obj(value: JsonValue) -> Obj:
    assert isinstance(value, dict)
    return value


def _str(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


CASES = [_obj(c) for c in cast("list[JsonValue]", VECTOR["cases"])]
API_KEY = _str(VECTOR["api_key"])
DOMAIN = _str(VECTOR["domain"])


class Replay:
    """The case's exchanges in order: each request is checked, then its answer replayed."""

    def __init__(self, exchanges: JsonValue) -> None:
        assert isinstance(exchanges, list)
        self._left = [_obj(e) for e in exchanges]

    def answer(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> Obj:
        assert self._left, f"sent more than the vector: {method} {url}"
        exchange = self._left.pop(0)
        _expect_request(_obj(exchange["request"]), method, url, headers, body)
        return _obj(exchange["response"])

    def done(self) -> None:
        assert not self._left, "sent less than the vector"


def _expect_request(
    want: Obj, method: str, url: str, headers: Mapping[str, str], body: bytes
) -> None:
    assert (method, url) == (want["method"], want["url"])
    for name, value in _obj(want["headers"]).items():
        assert headers.get(name) == value, name
    if not url.startswith(f"https://api.{DOMAIN}"):
        assert all(API_KEY not in v for v in headers.values())
    if "json" in want:
        assert json.loads(body) == want["json"]
    elif "envelope" in want:
        assert body[0] == 0
        assert int.from_bytes(body[1:5]) == len(body) - 5
        assert json.loads(body[5:]) == want["envelope"]
    elif "multipart" in want:
        part = _obj(want["multipart"])
        boundary = headers["content-type"].split("boundary=")[1].encode()
        head, data = body.split(b"--" + boundary)[1].split(b"\r\n\r\n", 1)
        assert f'name="{part["name"]}"; filename="{part["filename"]}"'.encode() in head
        assert data.removesuffix(b"\r\n") == _str(part["text"]).encode()
    else:
        assert body == b""


def _content(response: Obj) -> bytes:
    if "hex" in response:
        return bytes.fromhex(_str(response["hex"]))
    return _str(response["text"]).encode()


def _http(replay: Replay) -> httpx.AsyncBaseTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        answer = replay.answer(request.method, str(request.url), request.headers, request.content)
        headers = {k: _str(v) for k, v in _obj(answer["headers"]).items()}
        return httpx.Response(
            cast("int", answer["status"]), headers=headers, content=_content(answer)
        )

    return TracedTransport(FakeBackend.scripted(), handle)


def _rpc(replay: Replay) -> ASGITransport:
    async def app(
        scope: Mapping[str, object],
        receive: Callable[[], Awaitable[Mapping[str, object]]],
        send: Callable[[Mapping[str, object]], Awaitable[None]],
    ) -> None:
        body = b""
        while True:
            message = await receive()
            body += cast("bytes", message.get("body", b""))
            if not message.get("more_body"):
                break
        pairs = cast("list[tuple[bytes, bytes]]", scope["headers"])
        headers = {k.decode(): v.decode() for k, v in pairs}
        query = cast("bytes", scope["query_string"]).decode()
        url = f"https://{headers['host']}{scope['path']}" + (f"?{query}" if query else "")
        answer = replay.answer(cast("str", scope["method"]), url, headers, body)
        out = [(k.encode(), _str(v).encode()) for k, v in _obj(answer["headers"]).items()]
        await send({"type": "http.response.start", "status": answer["status"], "headers": out})
        await send({"type": "http.response.body", "body": _content(answer)})

    return ASGITransport(app)


async def _exec(envd: Envd, op: Obj) -> JsonValue:
    argv = [_str(a) for a in cast("list[JsonValue]", op["argv"])]
    env = {k: _str(v) for k, v in _obj(op["env"]).items()}
    tag = op["tag"]
    started = await call(
        OPEN, lambda: envd.start(argv, env, _str(op["cwd"]), tag if isinstance(tag, str) else None)
    )
    if isinstance(started, Err):
        return {"error": started.error.code}
    output = started.value
    stdout = b"".join([c async for c in output.stdout])
    stderr = b"".join([c async for c in output.stderr])
    got: Obj = {"stdout": stdout.decode(), "stderr": stderr.decode()}
    try:
        return {"ok": {**got, "exit": await output.exit_code}}
    except StreamLostError:
        return {"ok": {**got, "exit_error": "unavailable"}}


def _value(value: object) -> JsonValue:
    """An operation's value, as the vector writes it."""
    if isinstance(value, wire.Sandbox):
        return {
            "sandbox_id": value.sandbox_id,
            "envd_access_token": value.envd_access_token,
            "domain": value.domain,
        }
    if isinstance(value, bytes):
        return value.decode()
    assert value is None or isinstance(value, bool | list)
    return cast("JsonValue", value)


async def _run(case: Obj) -> JsonValue:
    replay = Replay(case["exchanges"])
    transports = Transports(_http(replay), _rpc(replay))
    config = ConnectionConfig(
        api_key=API_KEY, domain=DOMAIN, api_url=f"https://api.{DOMAIN}", retries=0
    )
    client = AsyncApiClient(config, transport=FencedHttpx(transports.http))
    control = Control(client)
    sandbox = wire.Sandbox.model_validate(VECTOR["envd_sandbox"])
    url = config.get_sandbox_url(sandbox.sandbox_id, sandbox.domain or DOMAIN)
    envd = Envd(sandbox, url, transports, set())
    op = _obj(case["call"])
    ops: dict[str, Callable[[], Awaitable[object]]] = {
        "create": lambda: control.create(
            _str(op["template"]),
            _str(op["key"]),
            timeout_s=cast("int", op["timeout_s"]),
            internet=op["internet"] is True,
        ),
        "find": lambda: control.find(_str(op["key"])),
        "describe": lambda: control.describe(_str(op["id"])),
        "kill": lambda: control.kill(_str(op["id"])),
        "signal": lambda: envd.signal(_str(op["tag"])),
        "upload": lambda: envd.upload(_str(op["path"]), _str(op["text"]).encode()),
        "download": lambda: envd.download(_str(op["path"])),
    }
    got: JsonValue
    if op["op"] == "start":
        got = await _exec(envd, op)
    else:
        result = await call(OPEN, ops[_str(op["op"])])
        got = (
            {"error": result.error.code}
            if isinstance(result, Err)
            else {"ok": _value(result.value)}
        )
    await envd.aclose()
    await client.get_async_httpx_client().aclose()
    replay.done()
    return got


@pytest.mark.parametrize("case", CASES, ids=lambda c: _str(c["name"]))
def test_wire(case: Obj) -> None:
    assert asyncio.run(_run(case)) == case["expect"]


def test_envd_urls() -> None:
    for row in cast("list[JsonValue]", VECTOR["envd_urls"]):
        entry = _obj(row)
        config = ConnectionConfig(api_key=API_KEY, domain=DOMAIN)
        assert (
            config.get_sandbox_url(_str(entry["sandbox_id"]), _str(entry["domain"])) == entry["url"]
        )


def test_a_listing_that_never_ends_is_an_error_not_absence() -> None:
    """A page token that never runs out stops at the page limit as a failure, so a lookup
    answers unknown rather than a false not-found."""
    pages: list[str] = []

    def endless(request: httpx.Request) -> httpx.Response:
        pages.append(str(request.url))
        return httpx.Response(200, headers={"x-next-token": "again"}, json=[])

    async def main() -> None:
        config = ConnectionConfig(api_key=API_KEY, domain=DOMAIN, retries=0)
        transport = TracedTransport(FakeBackend.scripted(), endless)
        client = AsyncApiClient(config, transport=FencedHttpx(transport))
        found = await call(OPEN, lambda: Control(client).find("op"))
        assert isinstance(found, Err)
        assert found.error.code == "unavailable"
        await client.get_async_httpx_client().aclose()

    asyncio.run(main())
    assert len(pages) == MAX_PAGES
