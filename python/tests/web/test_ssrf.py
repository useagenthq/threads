"""The SSRF guard's edges: IPv4 carried inside IPv6, special prefixes,
userinfo, other ports, and a connection that goes only to the address that was checked."""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
from pydantic import TypeAdapter

from threads.git.forge import GitHub, Refused
from threads.result import Err, Ok
from threads.web.fetch import Moved, get
from threads.web.guard import Target, blocked, vet
from threads.web.http import Fence, Request, Response, StdlibTransport, WebError
from threads.web.search import SearchHit, admitted

PUBLIC = "93.184.216.34"


async def _public(_host: str, _port: int) -> Sequence[str]:
    return [PUBLIC]


async def _open() -> bool:
    return True


_VECTOR: Final = TypeAdapter(list[tuple[str, bool]]).validate_python(
    [
        (c["address"], c["public"])
        for c in json.loads(
            (Path(__file__).resolve().parents[3] / "spec/conformance/vectors/ssrf.json").read_text()
        )["cases"]
    ]
)


@pytest.mark.parametrize(("address", "public"), _VECTOR)
def test_the_shared_ssrf_vector(address: str, public: bool) -> None:
    assert blocked(address) is not public


def test_an_unparseable_address_is_blocked() -> None:
    assert blocked("not-an-ip")


def test_a_url_with_userinfo_is_refused() -> None:
    async def main() -> None:
        for url in ("https://user:pw@example.com/", "https://user@example.com/"):
            got = await vet(url, _public)
            assert isinstance(got, Err)
            assert got.error.startswith("permission_denied")

    asyncio.run(main())


class _Once:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.sent: list[Target] = []

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        self.sent.append(target)
        return Ok(self.responses.pop(0))


def test_a_redirect_to_another_port_is_another_origin() -> None:
    async def main() -> None:
        script = _Once(Response(302, {"location": "https://example.com:8443/x"}, b""))
        got = await get("https://example.com/", _public, script, _open)
        assert got == Ok(Moved("https://example.com:8443/x"))
        assert len(script.sent) == 1

    asyncio.run(main())


def test_vet_resolves_once_and_the_target_carries_the_checked_address() -> None:
    """DNS rebinding: a second answer would be private, but nothing asks for one."""
    answers = [[PUBLIC], ["10.0.0.1"]]

    async def rebinding(_host: str, _port: int) -> Sequence[str]:
        return answers.pop(0)

    async def main() -> None:
        got = await vet("https://rebind.test/", rebinding)
        assert isinstance(got, Ok)
        assert got.value.ip == PUBLIC
        assert answers == [["10.0.0.1"]]

    asyncio.run(main())


def test_the_transport_connects_to_the_target_address_not_the_host_name() -> None:
    """`.invalid` never resolves: the request only lands if the transport dials `ip`."""

    async def main() -> None:
        seen: list[bytes] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            seen.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            target = Target(
                f"http://rebind.invalid:{port}/", "http", "rebind.invalid", port, "/", "127.0.0.1"
            )
            got = await StdlibTransport().send(target, Request("GET"), _open, 10)
        assert isinstance(got, Ok)
        assert got.value.body == b"ok"
        assert seen[0].startswith(b"GET / HTTP/1.1\r\nHost: rebind.invalid:")

    asyncio.run(main())


def test_forge_calls_to_a_configured_api_url_are_guarded() -> None:
    async def internal(_host: str, _port: int) -> Sequence[str]:
        return ["10.0.0.7"]

    async def main() -> None:
        script = _Once()
        forge = GitHub("https://ghe.internal/api/v3", script, internal)
        got = await forge.find("acme/app", "feature", "main", "tok", _open)
        assert isinstance(got, Err)
        assert isinstance(got.error, Refused)
        assert got.error.message.startswith("permission_denied")
        assert script.sent == []

    asyncio.run(main())


def test_blocked_domains_hold_for_a_trailing_dot_host() -> None:
    hits = [SearchHit("https://evil.com./b", "dot", ""), SearchHit("https://a.evil.com./", "", "")]
    assert admitted(hits, (), ("evil.com",)) == []
    assert admitted(hits, ("evil.com.",), ()) == hits
