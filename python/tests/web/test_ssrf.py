"""The SSRF guard's edges: IPv4 carried inside IPv6, special prefixes,
userinfo, other ports, and a connection that goes only to the address that was checked."""

import asyncio
from collections.abc import Sequence

import pytest

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


@pytest.mark.parametrize(
    "address",
    [
        "::127.0.0.1",  # IPv4-compatible
        "::7f00:1",
        "::a9fe:a9fe",
        "::ffff:169.254.169.254",  # IPv4-mapped
        "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 wrapping the metadata address
        "64:ff9b:1::a9fe:a9fe",  # local-use NAT64
        "2002:a9fe:a9fe::1",  # 6to4 wrapping the metadata address
        "2002:7f00:1::1",
        "2001::1",  # Teredo
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "100::1",  # discard-only
        "2001:db8::1",
        "2001:10::1",
        "3fff::1",
        "fec0::1",  # deprecated site-local
        "5f00::1",  # SRv6 SIDs, not ordinary unicast
        "192.88.99.1",  # deprecated 6to4 relay anycast
        "192.0.0.170",
        "240.0.0.1",
        "255.255.255.255",
        "::",
    ],
)
def test_special_and_wrapped_addresses_are_blocked(address: str) -> None:
    assert blocked(address)


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "2606:4700::1111", "64:ff9b::808:808", "2002:808:808::1"]
)
def test_public_addresses_and_public_wrapped_ones_pass(address: str) -> None:
    assert not blocked(address)


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


def test_blocked_domains_hold_for_a_trailing_dot_host() -> None:
    hits = [SearchHit("https://evil.com./b", "dot", ""), SearchHit("https://a.evil.com./", "", "")]
    assert admitted(hits, (), ("evil.com",)) == []
    assert admitted(hits, ("evil.com.",), ()) == hits
