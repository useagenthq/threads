"""web_fetch's transfer: vet, send, follow same-host redirects. Every hop is
vetted again, so a redirect to a private address is refused like a direct request. A redirect to
another host ends the call with the new URL: the model decides whether to fetch it."""

from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin

from threads.result import Err, Ok
from threads.web.guard import Resolve, origin, vet
from threads.web.http import Fence, Request, Transport, WebError

MAX_REDIRECTS: Final = 5
MAX_BYTES: Final = 10 << 20
"""ponytail: a page past 10 MiB is cut there; stream to the artifact store if that bites."""
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class Page:
    url: str
    """The final URL, after same-host redirects."""
    status: int
    media_type: str
    charset: str | None
    body: bytes
    truncated: bool


@dataclass(frozen=True, slots=True)
class Moved:
    """A redirect to another host: the call ends with its URL."""

    url: str


async def get(
    url: str, resolve: Resolve, transport: Transport, fence: Fence
) -> Ok[Page | Moved] | Err[str | WebError]:
    """A refusal is a message the model sees; a `WebError` is a transfer that failed."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        target = await vet(current, resolve)
        if isinstance(target, Err):
            return target
        request = Request("GET", {"Accept": "text/html, text/markdown, text/plain, */*;q=0.5"})
        sent = await transport.send(target.value, request, fence, MAX_BYTES)
        if isinstance(sent, Err):
            return sent
        response = sent.value
        location = response.headers.get("location")
        if response.status not in _REDIRECTS or not location:
            kind, _, params = response.headers.get("content-type", "").partition(";")
            return Ok(
                Page(
                    current,
                    response.status,
                    kind.strip().lower() or "application/octet-stream",
                    _charset(params),
                    response.body,
                    response.truncated,
                )
            )
        following = urljoin(current, location)
        if _host(following) != _host(current):
            return Ok(Moved(following))
        current = following
    return Err(f"too_many_redirects: more than {MAX_REDIRECTS} redirects from {url}")


def _host(url: str) -> tuple[str, int | None] | None:
    """Host and non-default port, as TS's `URL.host` compares them: another port is another
    host, and an http to https upgrade on the default ports is not."""
    found = origin(url)
    if found is None:
        return None
    scheme, host, port = found
    return host, None if port == (443 if scheme == "https" else 80) else port


def _charset(params: str) -> str | None:
    for param in params.split(";"):
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"')
    return None
