"""`mcp()` end to end on the official SDK: pinned,
namespaced, checked tools; calls on the effect path; typed setup errors; the fence at the
transport; credentials only in the host's own requests."""

import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from mcp_kit import SEEN_HEADERS, TracedAsgi, server
from pydantic import JsonValue

from threads import Completed, ConfigError, Parked, agent, scripted_model, secret, sqlite
from threads.adapters.mcp.transport import FENCE_REFUSED, FencedTransport, FenceRefusedError
from threads.agents.results import Thread
from threads.log import Event, Permissions, ThreadStartedEvent, ToolResultEvent
from threads.mcp import McpServer, mcp
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
KIT = str(Path(__file__).resolve().parents[1] / "mcp_kit.py")
ALLOW = Permissions(
    mode="default",
    allow=["mcp__kit__echo", "mcp__kit__sendemail", "mcp__kit__crash"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


def use(name: str, args: JsonValue) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "call_1", "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


async def events(thread: Thread) -> Sequence[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def test_a_stdio_server_in_one_line_pins_sorted_namespaced_tools_and_records_calls() -> None:
    async def main() -> None:
        kit = mcp(name="kit", command=sys.executable, args=[KIT], tools={"deny": ["crash"]})
        script = [use("mcp__kit__echo", {"text": "hi"}), text("Done.")]
        bot = agent(model=scripted_model({"responses": script}), tools=[kit], permissions=ALLOW)
        result = await bot.run("say hi", store=sqlite(":memory:"))
        assert isinstance(result, Completed), result
        got = await events(result.thread)
        started = next(e for e in got if isinstance(e, ThreadStartedEvent))
        names = [t.name for t in started.data.tools]
        assert names == ["read_tool_result", "mcp__kit__echo", "mcp__kit__sendemail"]
        assert {t.effect_class for t in started.data.tools[1:]} == {"unguarded"}
        (done,) = [e for e in got if isinstance(e, ToolResultEvent)]
        assert 'untrusted="true"' in done.data.preview
        assert "echo: hi" in done.data.preview
        assert [e.type for e in got].count("effect_begin") == 1

    asyncio.run(main())


def test_arguments_that_fail_the_server_schema_never_dispatch() -> None:
    async def main() -> None:
        kit = mcp(name="kit", command=sys.executable, args=[KIT], tools={"allow": ["echo"]})
        script = [use("mcp__kit__echo", {"text": 5}), text("Oops.")]
        bot = agent(model=scripted_model({"responses": script}), tools=[kit], permissions=ALLOW)
        result = await bot.run("say hi", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        got = await events(result.thread)
        (bad,) = [e for e in got if isinstance(e, ToolResultEvent)]
        assert bad.data.origin == "not_executed"
        assert "invalid arguments" in bad.data.preview
        assert "effect_begin" not in [e.type for e in got]

    asyncio.run(main())


def test_a_call_that_fails_after_dispatch_parks_and_is_never_retried() -> None:
    async def main() -> None:
        kit = mcp(name="kit", command=sys.executable, args=[KIT])
        script = [use("mcp__kit__crash", {}), text("never")]
        bot = agent(model=scripted_model({"responses": script}), tools=[kit], permissions=ALLOW)
        result = await bot.run("crash it", store=sqlite(":memory:"))
        assert isinstance(result, Parked), result
        assert result.reason == "effect_unknown"
        got = await events(result.thread)
        assert [e.type for e in got].count("effect_begin") == 1

    asyncio.run(main())


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    "gone",
    [
        mcp(name="gone", command="/nonexistent/mcp-server"),
        replace(mcp(name="gone", url="http://127.0.0.1:9/mcp"), http=httpx.MockTransport(_refused)),
    ],
)
def test_an_unreachable_server_is_a_setup_error_that_names_it(gone: McpServer) -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": []}), tools=[gone])
        with pytest.raises(ConfigError, match="gone") as raised:
            await bot.run("hi", store=sqlite(":memory:"))
        assert raised.value.code == "mcp_unreachable"

    asyncio.run(main())


def test_http_sends_secret_headers_from_the_host_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIT_TOKEN", "s3cr3t-value")
    app = server()
    asgi = TracedAsgi(app)

    async def main() -> None:
        async with app.session_manager.run():
            kit = mcp(
                name="kit",
                url="http://127.0.0.1:8000/mcp",
                headers={"Authorization": secret("KIT_TOKEN")},
            )
            kit = replace(kit, http=asgi)
            script = [use("mcp__kit__echo", {"text": "hi"}), text("Done.")]
            bot = agent(model=scripted_model({"responses": script}), tools=[kit], permissions=ALLOW)
            store = sqlite(":memory:")
            result = await bot.run("say hi", store=store)
            assert isinstance(result, Completed), result
            assert any(h.get("authorization") == "s3cr3t-value" for h in SEEN_HEADERS)
            log = "".join(repr(e) for e in await events(result.thread))
            assert "s3cr3t-value" not in log

    asyncio.run(main())


def test_the_fence_refuses_at_the_transport_and_nothing_is_written() -> None:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    async def main() -> None:
        async def stale() -> bool:
            return False

        fenced = FencedTransport(httpx.MockTransport(record), stale)
        body = b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"x"}}'
        request = httpx.Request("POST", "http://kit.test/mcp", content=body)
        answered = await fenced.handle_async_request(request)
        assert answered.json()["error"]["code"] == FENCE_REFUSED
        assert answered.json()["id"] == 7  # noqa: PLR2004 - the refused request's id
        assert sent == []

        calls = iter((True, False))

        async def lost_in_the_pool() -> bool:
            return next(calls)

        late = FencedTransport(TracedAsgi(server()), lost_in_the_pool)
        with pytest.raises(FenceRefusedError):
            await late.handle_async_request(httpx.Request("POST", "http://127.0.0.1:8000/mcp"))

    asyncio.run(main())


def test_a_stdio_write_is_fenced_and_a_refused_request_is_never_written() -> None:
    async def main() -> None:
        async def stale() -> bool:
            return False

        kit = mcp(name="kit", command=sys.executable, args=[KIT])
        with pytest.raises(ConfigError, match="stale_epoch"):
            async with kit.connect(stale):
                pass

    asyncio.run(main())
