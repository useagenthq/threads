"""web_fetch and web_search as a host tool runner: read_only, run on the host
through the fenced transport, so stub mode answers them like any mediated operation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import WebFetchInput, WebSearchInput
from threads.log import ArtifactRef, JsonObject, ToolSpec
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.tools import Dispatched, Invocation, Output, Termination
from threads.memory.fence import bound
from threads.result import Err
from threads.tools.runner import parse
from threads.web.fetch import Moved, get
from threads.web.guard import Resolve, system_resolve
from threads.web.http import Fence, StdlibTransport, Transport
from threads.web.results import failed, hits_output, moved_output, page_output
from threads.web.search import SearchBackend


@dataclass(frozen=True, slots=True)
class WebTools:
    fence: Fence
    put: Callable[[bytes, str], Awaitable[ArtifactRef]]
    search: SearchBackend | None = None
    transport: Transport = field(default_factory=StdlibTransport)
    resolve: Resolve = system_resolve

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        parsed = parse(spec.name, input)
        return parsed.error if isinstance(parsed, Err) else None

    async def dispatch(self, call: Invocation) -> Dispatched:
        parsed = parse(call.spec.name, call.input)
        match parsed.value if not isinstance(parsed, Err) else None:
            case WebFetchInput(url=url):
                return await self._fetch(url)
            case WebSearchInput() as args:
                return await self._search(args)
            case _:
                raise AssertionError(f"a dispatched {call.spec.name} call was parsed first")

    async def _fetch(self, url: str) -> Dispatched:
        got = await get(url, self.resolve, self.transport, self.fence)
        if isinstance(got, Err):
            return failed(got.error)
        if isinstance(got.value, Moved):
            return moved_output(got.value)
        return await page_output(got.value, self.put)

    async def _search(self, args: WebSearchInput) -> Dispatched:
        if self.search is None:
            return Output("unavailable: no search backend is configured", True)
        allowed = () if args.allowed_domains is MISSING else tuple(args.allowed_domains)
        blocked = () if args.blocked_domains is MISSING else tuple(args.blocked_domains)
        with bound(self.fence):
            found = await self.search.search(
                args.query, allowed_domains=allowed, blocked_domains=blocked
            )
        return (
            failed(found.error) if isinstance(found, Err) else hits_output(args.query, found.value)
        )

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return LookupUnknown(f"{call.spec.name} has no lookup")

    async def terminate(self, call: Invocation) -> Termination:
        return "unknown"

    def provider_now(self) -> int | None:
        return None
