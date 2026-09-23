"""The host web tools' transfer: the SSRF guard, the fenced stdlib transport
against a loopback server, redirects, markdown, and search results with citations."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

import pytest

from threads.log import ArtifactRef, CitationPart, TextPart
from threads.loop.tools import NotSent, Output, Uncertain
from threads.memory.fence import bound
from threads.result import Err, Ok
from threads.search import brave, exa, tavily
from threads.secrets import secret
from threads.web.fetch import MAX_REDIRECTS, Moved, Page, get
from threads.web.guard import Target, blocked, vet
from threads.web.http import Fence, Request, Response, StdlibTransport, WebError
from threads.web.markdown import to_markdown
from threads.web.results import failed, hits_output, page_output
from threads.web.search import SearchHit, admitted

PUBLIC = "93.184.216.34"
CHUNKED = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"


async def _public(_host: str, _port: int) -> Sequence[str]:
    return [PUBLIC]


def _resolver(table: Mapping[str, Sequence[str]]) -> Callable[[str, int], Awaitable[Sequence[str]]]:
    async def resolve(host: str, _port: int) -> Sequence[str]:
        return table[host]

    return resolve


async def _open() -> bool:
    return True


async def _closed() -> bool:
    return False


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104 - an address under test, not a bind
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
    ],
)
def test_non_public_addresses_are_blocked(address: str) -> None:
    assert blocked(address)


def test_public_addresses_pass() -> None:
    assert not blocked(PUBLIC)
    assert not blocked("2606:2800:220:1:248:1893:25c8:1946")


def test_vet_refuses_schemes_and_any_private_answer() -> None:
    async def main() -> None:
        for url in ("file:///etc/passwd", "ftp://example.com/", "gopher://x", "http://"):
            got = await vet(url, _public)
            assert isinstance(got, Err)
            assert got.error.startswith("permission_denied")
        # One private address among the answers is enough to refuse.
        mixed = _resolver({"evil.test": [PUBLIC, "10.0.0.5"]})
        assert isinstance(await vet("https://evil.test/", mixed), Err)
        ok = await vet("https://Example.COM:8443/a/b?q=1#frag", _public)
        assert isinstance(ok, Ok)
        target = ok.value
        assert (target.host, target.port, target.path, target.ip) == (
            "example.com",
            8443,
            "/a/b?q=1",
            PUBLIC,
        )

    asyncio.run(main())


class _Script:
    """A transport answering from a script of responses, recording what it was sent."""

    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.sent: list[Target] = []

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        if not await fence():
            return Err(WebError("stale_epoch", "lost", sent=False))
        self.sent.append(target)
        return Ok(self.responses.pop(0))


def _html(body: str, status: int = 200) -> Response:
    return Response(status, {"content-type": "text/html; charset=utf-8"}, body.encode())


def _redirect(to: str) -> Response:
    return Response(302, {"location": to}, b"")


def test_same_host_redirects_are_followed_and_capped() -> None:
    async def main() -> None:
        script = _Script(_redirect("/next"), _html("<p>hi</p>"))
        got = await get("https://example.com/start", _public, script, _open)
        assert isinstance(got, Ok)
        assert isinstance(got.value, Page)
        assert got.value.url == "https://example.com/next"
        loop = _Script(*[_redirect("/again")] * (MAX_REDIRECTS + 1))
        capped = await get("https://example.com/", _public, loop, _open)
        assert isinstance(capped, Err)
        assert len(loop.sent) == MAX_REDIRECTS + 1

    asyncio.run(main())


def test_a_cross_host_redirect_ends_the_call_with_its_url() -> None:
    async def main() -> None:
        script = _Script(_redirect("https://other.example.org/x"))
        got = await get("https://example.com/", _public, script, _open)
        assert isinstance(got, Ok)
        assert got.value == Moved("https://other.example.org/x")

    asyncio.run(main())


def test_a_redirect_to_a_private_address_is_refused() -> None:
    async def main() -> None:
        table = _resolver({"example.com": [PUBLIC], "169.254.169.254": ["169.254.169.254"]})
        metadata = _Script(_redirect("http://169.254.169.254/latest/meta-data/"))
        moved = await get("http://example.com/", table, metadata, _open)
        # Another host: the model gets the URL, and a web_fetch of it is refused by the guard.
        assert isinstance(moved, Ok)
        assert isinstance(moved.value, Moved)
        refused = await get(moved.value.url, table, _Script(), _open)
        assert isinstance(refused, Err)
        assert isinstance(refused.error, str)
        assert refused.error.startswith("permission_denied")

    asyncio.run(main())


def test_a_stale_fence_sends_nothing() -> None:
    async def main() -> None:
        script = _Script(_html("x"))
        got = await get("https://example.com/", _public, script, _closed)
        assert isinstance(got, Err)
        assert not script.sent
        assert isinstance(failed(got.error), NotSent)

    asyncio.run(main())


def test_failures_map_to_outcomes() -> None:
    assert isinstance(failed(WebError("timeout", "slow", sent=True)), Uncertain)
    assert failed(WebError("unavailable", "reset", sent=True)) == Uncertain("transport_error")
    refused = failed(WebError("unavailable", "no route", sent=False))
    assert isinstance(refused, Output)
    assert refused.is_error


def test_markdown_keeps_structure_and_drops_scripts() -> None:
    html = (
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<h1>Title</h1><p>Some <b>bold</b> and <a href='/doc'>a link</a>.</p>"
        "<script>alert(1)</script><ul><li>one</li><li>two</li></ul>"
        "<pre>x = 1\n  y = 2</pre></body></html>"
    )
    md = to_markdown(html, "https://example.com/a/")
    assert md == (
        "# Title\n\nSome **bold** and [a link](https://example.com/doc).\n\n"
        "- one\n- two\n\n```\nx = 1\n  y = 2\n```"
    )


def test_a_page_is_text_plus_a_web_citation_naming_the_artifact() -> None:
    stored: list[tuple[bytes, str]] = []

    async def put(data: bytes, media_type: str) -> ArtifactRef:
        stored.append((data, media_type))
        return ArtifactRef(sha256="a" * 64, bytes=len(data), media_type=media_type)

    async def main() -> None:
        page = Page("https://example.com/", 200, "text/html", "utf-8", b"<p>Hello</p>", False)
        out = await page_output(page, put)
        assert stored == [(b"<p>Hello</p>", "text/html")]
        assert not out.is_error
        text, cite = out.content
        assert isinstance(text, TextPart)
        assert text.text.endswith("\n\nHello")
        assert "SHA-256: " + "a" * 64 in text.text
        assert isinstance(cite, CitationPart)
        assert (cite.source_kind, cite.source_id) == ("web", "https://example.com/")
        missing = Page("https://example.com/x", 404, "text/plain", "bogus-charset", b"no", False)
        assert (await page_output(missing, put)).is_error

    asyncio.run(main())


def test_search_hits_are_text_parts_each_followed_by_its_citation() -> None:
    hits = [SearchHit("https://a.example/1", "A", "first"), SearchHit("https://b.example", "B", "")]
    out = hits_output("q", hits)
    kinds = [p.type for p in out.content]
    assert kinds == ["text", "citation", "text", "citation"]
    assert out.text.startswith("1. A\n   https://a.example/1\n   first")
    assert hits_output("q", []).content == ()


def test_domain_filters_apply_to_hits() -> None:
    hits = [
        SearchHit("https://docs.python.org/3/", "py", ""),
        SearchHit("https://evil.python.org.attacker.test/", "x", ""),
        SearchHit("https://pypi.org/", "pypi", ""),
    ]
    assert [h.title for h in admitted(hits, ["python.org"], [])] == ["py"]
    assert [h.title for h in admitted(hits, [], ["python.org"])] == ["x", "pypi"]


async def _serve(answer: bytes) -> tuple[asyncio.Server, int, list[bytes]]:
    seen: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(answer)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], seen


def test_the_stdlib_transport_reads_lengths_chunks_and_caps() -> None:
    async def main() -> None:
        answers = {
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nX-A: 1\r\n\r\nhello": (b"hello", False),
            CHUNKED: (
                b"abcde",
                False,
            ),
            b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 50: (b"x" * 10, True),
        }
        for answer, (body, cut) in answers.items():
            server, port, seen = await _serve(answer)
            async with server:
                target = Target(
                    "http://example.com/p", "http", "example.com", port, "/p?q", "127.0.0.1"
                )
                got = await StdlibTransport().send(target, Request("GET"), _open, 10)
            assert isinstance(got, Ok)
            assert (got.value.status, got.value.body, got.value.truncated) == (200, body, cut)
            assert seen[0].startswith(b"GET /p?q HTTP/1.1\r\nHost: example.com:")

    asyncio.run(main())


def test_the_stdlib_transport_checks_the_fence_before_writing() -> None:
    async def main() -> None:
        server, port, seen = await _serve(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        async with server:
            target = Target("http://example.com/", "http", "example.com", port, "/", "127.0.0.1")
            got = await StdlibTransport().send(target, Request("GET"), _closed, 10)
            await asyncio.sleep(0.05)
        assert isinstance(got, Err)
        assert got.error.code == "stale_epoch"
        assert not got.error.sent
        assert seen == []

    asyncio.run(main())


class _Canned:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[tuple[Target, Request]] = []

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        if not await fence():
            return Err(WebError("stale_epoch", "lost", sent=False))
        self.requests.append((target, request))
        return Ok(Response(200, {}, self.body))


def test_search_backends_send_the_key_on_the_host_and_parse_hits() -> None:
    env_key = "THREADS_TEST_SEARCH_KEY"
    results = b'{"results": [{"url": "https://a.example/", "title": "A", "text": "t", "extra": 1}]}'
    brave_body = (
        b'{"web": {"results": [{"url": "https://a.example/", "title": "A", "description": "d"}]}}'
    )

    async def main() -> None:
        for make, body, host in (
            (exa, results, "api.exa.ai"),
            (tavily, results, "api.tavily.com"),
            (brave, brave_body, "api.search.brave.com"),
        ):
            canned = _Canned(body)
            backend = make(secret(env_key))
            backend = type(backend)(backend.key, backend.build, backend.parse, canned, _public)
            with bound(_open):
                got = await backend.search("q", blocked_domains=["b.example"])
            assert isinstance(got, Ok)
            assert [h.url for h in got.value] == ["https://a.example/"]
            target, request = canned.requests[0]
            assert target.host == host
            assert "sk-test" in repr(request.headers) or b"sk-test" in (request.body or b"")
            # Outside a bound fence nothing is sent.
            assert isinstance(await backend.search("q"), Err)
            assert len(canned.requests) == 1

    monkey = pytest.MonkeyPatch()
    monkey.setenv(env_key, "sk-test")
    try:
        asyncio.run(main())
    finally:
        monkey.undo()
