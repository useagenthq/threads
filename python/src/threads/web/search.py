"""spec/api.json `SearchBackend` and `SearchHit`: web_search's backend adapter.
The framework binds the run's fence around each search (`threads.memory.fence.bound`) and a
backend's transport checks it at its send point. Domain filters are passed to the backend and
enforced again on its hits, since a backend may treat them as hints."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

from threads.memory.fence import FenceRefusedError, check
from threads.result import Err, Ok


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class SearchError:
    code: Literal["unavailable", "timeout", "stale_epoch"]
    message: str


class SearchBackend(Protocol):
    async def search(
        self,
        query: str,
        *,
        allowed_domains: Sequence[str] = (),
        blocked_domains: Sequence[str] = (),
    ) -> Ok[Sequence[SearchHit]] | Err[SearchError]: ...


def in_domain(url: str, domain: str) -> bool:
    """The URL's host is the domain or a subdomain of it."""
    host = (urlsplit(url).hostname or "").lower()
    domain = domain.lower().removeprefix("*.").strip(".")
    return host == domain or host.endswith(f".{domain}")


def admitted(
    hits: Sequence[SearchHit], allowed: Sequence[str], blocked: Sequence[str]
) -> list[SearchHit]:
    return [
        h
        for h in hits
        if (not allowed or any(in_domain(h.url, d) for d in allowed))
        and not any(in_domain(h.url, d) for d in blocked)
    ]


async def bound_fence() -> bool:
    """The fence the framework bound around this search, for a transport's send point."""
    try:
        await check()
    except FenceRefusedError:
        return False
    return True
