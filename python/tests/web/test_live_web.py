"""Live gates for the host gateway tools (F1.16): real DNS
and a real page through the SSRF guard, a real search backend, and the metadata address refused
after resolution. Skipped unless THREADS_LIVE=1 (and the backend's key for search).

    THREADS_LIVE=1 [EXA_API_KEY=... | TAVILY_API_KEY=... | BRAVE_API_KEY=...] \\
    uv run pytest -m live tests/web
"""

import asyncio
import os

import pytest

from threads.log import ArtifactRef, CitationPart
from threads.log.digest import sha256_hex
from threads.memory.fence import bound
from threads.result import Err, Ok
from threads.search import HttpSearch, brave, exa, tavily
from threads.secrets import secret
from threads.web.fetch import Page, get
from threads.web.guard import system_resolve
from threads.web.http import StdlibTransport
from threads.web.results import page_output

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def live_gate() -> None:
    if os.environ.get("THREADS_LIVE") != "1":
        pytest.skip("live gate: set THREADS_LIVE=1 to reach the web")


async def _open() -> bool:
    return True


async def _put(data: bytes, media_type: str) -> ArtifactRef:
    return ArtifactRef(sha256=sha256_hex(data), bytes=len(data), media_type=media_type)


def test_web_fetch_ssrf_and_record() -> None:
    async def main() -> None:
        got = await get("https://example.com/", system_resolve, StdlibTransport(), _open)
        assert isinstance(got, Ok)
        page = got.value
        assert isinstance(page, Page)
        out = await page_output(page, _put)
        assert isinstance(out.content[1], CitationPart)
        assert "Example Domain" in out.text
        for url in ("http://169.254.169.254/latest/meta-data/", "http://localhost:80/"):
            refused = await get(url, system_resolve, StdlibTransport(), _open)
            assert isinstance(refused, Err)
            assert isinstance(refused.error, str)
            assert refused.error.startswith("permission_denied")

    asyncio.run(main())


BACKENDS = {"EXA_API_KEY": exa, "TAVILY_API_KEY": tavily, "BRAVE_API_KEY": brave}


@pytest.mark.parametrize("key", sorted(BACKENDS))
def test_web_search_citations(key: str) -> None:
    if not os.environ.get(key):
        pytest.skip(f"live gate: {key} not set")
    backend: HttpSearch = BACKENDS[key](secret(key))

    async def main() -> None:
        with bound(_open):
            got = await backend.search("python asyncio", allowed_domains=["python.org"])
        assert isinstance(got, Ok), got
        assert got.value
        assert all("python.org" in hit.url for hit in got.value)

    asyncio.run(main())
