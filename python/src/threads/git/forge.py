"""The forge API behind open_pull_request: GitHub's REST API over the host's
fenced transport, with the credential in a header on the host only. A pull request is looked up
by its head branch, which is also how a lost create is reconciled."""

import json
from dataclasses import dataclass, field
from typing import ClassVar, Final
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from threads.result import Err, Ok
from threads.web.guard import Resolve, system_resolve, vet
from threads.web.http import Fence, Request, Response, StdlibTransport, Transport, WebError

_BYTES: Final = 1 << 20


class _Wire(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True, strict=True)


class _Pull(_Wire):
    number: int
    html_url: str


_PULLS: Final = TypeAdapter(list[_Pull])


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str

    def text(self) -> str:
        return f"pull request #{self.number}: {self.url}"


@dataclass(frozen=True, slots=True)
class Refused:
    """The forge answered and did not act (a 4xx): a definite failure the model sees."""

    message: str


@dataclass(frozen=True, slots=True)
class GitHub:
    api: str = "https://api.github.com"
    transport: Transport = field(default_factory=StdlibTransport)
    resolve: Resolve = system_resolve

    async def _call(
        self, path: str, token: str, fence: Fence, body: bytes | None = None
    ) -> Ok[Response] | Err[WebError | Refused]:
        target = await vet(f"{self.api}{path}", self.resolve)
        if isinstance(target, Err):
            return Err(Refused(target.error))
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request("GET" if body is None else "POST", headers, body)
        sent = await self.transport.send(target.value, request, fence, _BYTES)
        if isinstance(sent, Err):
            return sent
        status = sent.value.status
        if status >= 500:  # noqa: PLR2004 - a server error may follow a create
            return Err(WebError("unavailable", f"the forge answered {status}", sent=True))
        if status >= 400:  # noqa: PLR2004 - the forge refused
            return Err(Refused(f"the forge answered {status}: {sent.value.body[:500]!r}"))
        return sent

    async def find(
        self, repo: str, head: str, token: str, fence: Fence
    ) -> Ok[PullRequest | None] | Err[WebError | Refused]:
        """The open pull request whose head is `head` in the repo's own owner."""
        owner = repo.split("/", 1)[0]
        query = urlencode({"state": "open", "head": f"{owner}:{head}"})
        got = await self._call(f"/repos/{repo}/pulls?{query}", token, fence)
        if isinstance(got, Err):
            return got
        try:
            pulls = _PULLS.validate_json(got.value.body)
        except ValidationError as error:
            return Err(WebError("unavailable", f"unreadable forge answer: {error}", sent=True))
        return Ok(PullRequest(pulls[0].number, pulls[0].html_url) if pulls else None)

    async def open(  # noqa: PLR0913 - the pull request's fields
        self, repo: str, head: str, base: str, title: str, body: str, token: str, fence: Fence
    ) -> Ok[PullRequest] | Err[WebError | Refused]:
        data = json.dumps({"head": head, "base": base, "title": title, "body": body}).encode()
        got = await self._call(f"/repos/{repo}/pulls", token, fence, data)
        if isinstance(got, Err):
            return got
        try:
            pull = _Pull.model_validate_json(got.value.body)
        except ValidationError as error:
            return Err(WebError("unavailable", f"unreadable forge answer: {error}", sent=True))
        return Ok(PullRequest(pull.number, pull.html_url))
