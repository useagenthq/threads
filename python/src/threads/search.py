"""web_search backends (spec/api.json `SearchBackend`, ): `exa()`, `brave()` and
`tavily()`. Each takes its API key as a `secret()`, resolved on the host when a search is sent,
and sends through the host transport fenced by the run. No SDK: each is one JSON request."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Final
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from threads.result import Err, Ok
from threads.secrets import Secret, resolve
from threads.web.guard import Resolve, system_resolve, vet
from threads.web.http import Request, StdlibTransport, Transport
from threads.web.search import SearchError, SearchHit, admitted, bound_fence

MAX_RESULTS: Final = 10
_RESPONSE_BYTES: Final = 4 << 20


class _Wire(BaseModel):
    # Provider responses gain fields over time: unknown ones are ignored, known ones strict.
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True, strict=True)


class _Hit(_Wire):
    url: str
    title: str | None = None
    text: str | None = None
    description: str | None = None
    content: str | None = None


class _Results(_Wire):
    results: list[_Hit] = []


class _Brave(_Wire):
    web: _Results = _Results()


type _Build = Callable[[str, str, Sequence[str], Sequence[str]], tuple[str, Request]]
type _Parse = Callable[[bytes], list[_Hit]]


@dataclass(frozen=True, slots=True)
class HttpSearch:
    """One backend: how a query becomes a request, and the response its hits."""

    key: Secret
    build: _Build
    parse: _Parse
    transport: Transport = field(default_factory=StdlibTransport)
    resolver: Resolve = system_resolve

    async def search(
        self,
        query: str,
        *,
        allowed_domains: Sequence[str] = (),
        blocked_domains: Sequence[str] = (),
    ) -> Ok[Sequence[SearchHit]] | Err[SearchError]:
        url, request = self.build(query, resolve(self.key), allowed_domains, blocked_domains)
        target = await vet(url, self.resolver)
        if isinstance(target, Err):
            return Err(SearchError("unavailable", target.error))
        sent = await self.transport.send(target.value, request, bound_fence, _RESPONSE_BYTES)
        if isinstance(sent, Err):
            return Err(SearchError(sent.error.code, sent.error.message))
        response = sent.value
        if response.status != 200:  # noqa: PLR2004 - HTTP OK
            return Err(SearchError("unavailable", f"search answered {response.status}"))
        try:
            raw = self.parse(response.body)
        except (ValidationError, ValueError) as error:
            return Err(SearchError("unavailable", f"unreadable search response: {error}"))
        hits = [SearchHit(h.url, h.title or h.url, _snippet(h)) for h in raw]
        return Ok(admitted(hits, allowed_domains, blocked_domains)[:MAX_RESULTS])


def _snippet(hit: _Hit) -> str:
    return (hit.text or hit.description or hit.content or "").strip()[:500]


def _json(headers: Mapping[str, str], body: Mapping[str, JsonValue]) -> Request:
    data = json.dumps(body).encode("utf-8")
    return Request("POST", {**headers, "Content-Type": "application/json"}, data)


def exa(api_key: Secret) -> HttpSearch:
    def build(q: str, key: str, allow: Sequence[str], block: Sequence[str]) -> tuple[str, Request]:
        body: dict[str, JsonValue] = {
            "query": q,
            "numResults": MAX_RESULTS,
            "contents": {"text": True},
        }
        if allow:
            body["includeDomains"] = list(allow)
        if block:
            body["excludeDomains"] = list(block)
        return "https://api.exa.ai/search", _json({"x-api-key": key}, body)

    return HttpSearch(api_key, build, lambda b: _Results.model_validate_json(b).results)


def tavily(api_key: Secret) -> HttpSearch:
    def build(q: str, key: str, allow: Sequence[str], block: Sequence[str]) -> tuple[str, Request]:
        body: dict[str, JsonValue] = {"query": q, "max_results": MAX_RESULTS}
        if allow:
            body["include_domains"] = list(allow)
        if block:
            body["exclude_domains"] = list(block)
        auth = {"Authorization": f"Bearer {key}"}
        return "https://api.tavily.com/search", _json(auth, body)

    return HttpSearch(api_key, build, lambda b: _Results.model_validate_json(b).results)


def brave(api_key: Secret) -> HttpSearch:
    def build(q: str, key: str, allow: Sequence[str], block: Sequence[str]) -> tuple[str, Request]:
        # Brave has no domain parameters: its query operators carry them.
        terms = [q, *(f"site:{d}" for d in allow[:1]), *(f"-site:{d}" for d in block)]
        query = urlencode({"q": " ".join(terms), "count": MAX_RESULTS})
        headers = {"Accept": "application/json", "X-Subscription-Token": key}
        return f"https://api.search.brave.com/res/v1/web/search?{query}", Request("GET", headers)

    return HttpSearch(api_key, build, lambda b: _Brave.model_validate_json(b).web.results)


__all__ = ["HttpSearch", "SearchHit", "brave", "exa", "tavily"]
