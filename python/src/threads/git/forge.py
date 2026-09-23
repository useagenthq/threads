"""The forge API behind open_pull_request: GitHub's REST API over the host's
fenced transport, with the credential in a header on the host only. A pull request is looked up
by its head and base branches in every state, which is also how a lost create is reconciled
(spec/schema/README.md, Git gateway: open_pull_request)."""

import json
from dataclasses import dataclass, field
from http import HTTPStatus
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
    state: str


_PULLS: Final = TypeAdapter(list[_Pull])


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    state: str | None = None
    """The forge's state for one that already existed; None for one this call created."""

    def text(self) -> str:
        state = "" if self.state is None else f" ({self.state})"
        return f"pull request #{self.number}{state}: {self.url}"


@dataclass(frozen=True, slots=True)
class Refused:
    """The forge answered and did not act (a 4xx): a definite failure the model sees."""

    message: str
    status: int = 0
    body: bytes = b""


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
            body = sent.value.body
            return Err(Refused(f"the forge answered {status}: {body[:500]!r}", status, body))
        return sent

    async def find(
        self, repo: str, head: str, base: str, token: str, fence: Fence
    ) -> Ok[PullRequest | None] | Err[WebError | Refused]:
        """The newest pull request in any state from `head` in the repo's own owner into
        `base` (the forge lists newest first)."""
        owner = repo.split("/", 1)[0]
        query = urlencode({"state": "all", "head": f"{owner}:{head}", "base": base})
        got = await self._call(f"/repos/{repo}/pulls?{query}", token, fence)
        if isinstance(got, Err):
            return got
        try:
            pulls = _PULLS.validate_json(got.value.body)
        except ValidationError as error:
            return Err(WebError("unavailable", f"unreadable forge answer: {error}", sent=True))
        return Ok(
            PullRequest(pulls[0].number, pulls[0].html_url, pulls[0].state) if pulls else None
        )

    async def open(  # noqa: PLR0913 - the pull request's fields
        self, repo: str, *, head: str, base: str, title: str, body: str, token: str, fence: Fence
    ) -> Ok[PullRequest] | Err[WebError | Refused]:
        data = json.dumps({"head": head, "base": base, "title": title, "body": body}).encode()
        got = await self._call(f"/repos/{repo}/pulls", token, fence, data)
        if isinstance(got, Err):
            if not _exists(got.error):
                return got
            # The forge says one exists for the head: the lookup names it.
            found = await self.find(repo, head, base, token, fence)
            if isinstance(found, Err) or found.value is None:
                return got
            return Ok(found.value)
        try:
            pull = _Pull.model_validate_json(got.value.body)
        except ValidationError as error:
            return Err(WebError("unavailable", f"unreadable forge answer: {error}", sent=True))
        return Ok(PullRequest(pull.number, pull.html_url))


def _exists(error: WebError | Refused) -> bool:
    """A 422 is "already exists" only when the forge says a pull request for the head exists;
    any other 422 is an error result."""
    return (
        isinstance(error, Refused)
        and error.status == HTTPStatus.UNPROCESSABLE_ENTITY
        and b"pull request already exists" in error.body.lower()
    )
