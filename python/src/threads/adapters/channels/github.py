"""`github()` (extra `github`, ): issue and pull-request comments.

A webhook is verified by `X-Hub-Signature-256` with the webhook secret; the App installation is
the installation and `github:<installation id>` the tenant, and the sender is named by its
numeric id (a login can be renamed and taken). GitHub signs no timestamp and does not sign
`X-GitHub-Delivery`, so the delivery id is the hash of the signed bytes: a replayed body is the
same inbox item whatever header it comes with. A new comment by a person becomes one item keyed
`<delivery>#0`; bots' comments (the app's own included) and threads' own marked comments are
ignored. A reply is a comment carrying the effect key in a hidden marker, which lookup finds by
listing that issue's comments. The channel declares its lookup nonfinal, so an uncertain send
it can't find parks. GitHub publishes no official Python SDK; the REST API is called through
the fenced httpx client.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import httpx
from pydantic import JsonValue, RootModel, ValidationError

from threads.adapters.channels.common import (
    Loose,
    body_of,
    client,
    hub_signature,
    refused,
    render_ops,
    send,
    unverified,
)
from threads.host.channel import (
    ChannelCapabilities,
    Decision,
    DeliveryError,
    DeliveryOutcome,
    Ignore,
    Inbound,
    Message,
    RawRequest,
    RawResponse,
    Sent,
    VerifiedDelivery,
)
from threads.log import ApprovalRequestedEvent, Event, JsonObject, ParseError, Principal
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFound
from threads.memory.fence import check
from threads.result import Err, Ok
from threads.secrets import Secret, resolve

API: Final = "https://api.github.com"
_PER_PAGE: Final = 100
_PAGES: Final = 10
_DECISION: Final = re.compile(
    r"/(?P<verb>approve|deny) "
    r"(?P<challenge>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
"""A reply that answers an approval card: the verb and the challenge id, nothing else."""
_ACCEPT: Final = {"accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"}


class _Installation(Loose):
    id: int


class _User(Loose):
    id: int
    type: str = "User"


class _Repository(Loose):
    full_name: str


class _Issue(Loose):
    number: int


class _Comment(Loose):
    body: str = ""


class _Posted(Loose):
    id: int
    body: str = ""


class _Comments(RootModel[tuple[_Posted, ...]]):
    pass


class _Hook(Loose):
    action: str | None = None
    installation: _Installation | None = None
    sender: _User | None = None
    repository: _Repository | None = None
    issue: _Issue | None = None
    comment: _Comment | None = None


_MARK: Final = "<!-- threads:effect_key="


def marker(effect_key: str) -> str:
    return f"{_MARK}{effect_key} -->"


@dataclass(frozen=True, slots=True)
class GitHubChannel:
    webhook_secret: Secret
    token: Secret
    agent: str
    api: str = API
    transport: httpx.AsyncBaseTransport | None = None
    capabilities: ChannelCapabilities = field(
        default_factory=lambda: ChannelCapabilities("nonfinal", False, True, False, False)
    )
    limits: Mapping[str, int] = field(default_factory=lambda: {"message_bytes": 65_536})

    @property
    def secrets(self) -> Mapping[str, Secret]:
        return {"token": self.token}

    def verify(self, raw: RawRequest) -> Ok[VerifiedDelivery] | Err[ParseError]:
        header = raw.headers.get("x-hub-signature-256")
        if not hub_signature(resolve(self.webhook_secret), raw.body, header):
            return unverified("the GitHub signature does not match")
        hook = _hook(raw)
        if hook is None or hook.installation is None:
            return unverified("not a GitHub App delivery")
        installation = str(hook.installation.id)
        delivery = hashlib.sha256(raw.body).hexdigest()
        return Ok(VerifiedDelivery(f"github:{installation}", installation, delivery))

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]:
        hook = _hook(raw)
        verified = self.verify(raw)
        if hook is None or isinstance(verified, Err):
            return Err(ParseError("invalid", "not a verified GitHub delivery"))
        return Ok((_item(raw, hook, verified.value),))

    def ack(self, raw: RawRequest) -> RawResponse:
        return RawResponse(200, {}, b"")

    def render(self, event: Event) -> Sequence[JsonObject]:
        if isinstance(event, ApprovalRequestedEvent):
            # No buttons: the text fallback, still bound to the challenge id.
            answer = event.data.challenge_id
            return render_ops(event, f" Reply `/approve {answer}` or `/deny {answer}`.")
        return render_ops(event)

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        repo, _, number = str(op["address"]).rpartition("#")
        body: JsonValue = {"body": f"{op['text']}\n\n{marker(effect_key)}"}
        headers = _ACCEPT | {"authorization": f"Bearer {credentials['token']}"}
        url = f"{self.api}/repos/{repo}/issues/{number}/comments"
        async with client(self.transport) as http:
            response = await send(http, url, headers, body)
        if isinstance(response, DeliveryError):
            return response
        failed = refused(response)
        if failed is not None:
            return failed
        created = body_of(response).get("id")
        return Sent(str(created) if isinstance(created, int) else "")

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        """The comment on the op's issue that carries the key's marker. The channel declares
        nonfinal, so a not_found parks the send rather than proving it absent."""
        await check()
        repo, _, number = str(op["address"]).rpartition("#")
        headers = _ACCEPT | {"authorization": f"Bearer {resolve(self.token)}"}
        url = f"{self.api}/repos/{repo}/issues/{number}/comments"
        wanted = marker(effect_key)
        async with client(self.transport) as http:
            for page in range(1, _PAGES + 1):
                try:
                    response = await http.get(url, headers=headers, params=_page(page))
                except httpx.HTTPError as error:
                    return LookupUnknown(f"listing failed: {type(error).__name__}")
                if refused(response) is not None:
                    return LookupUnknown(f"listing answered {response.status_code}")
                comments = _Comments.model_validate_json(response.content or b"[]").root
                found = next((c for c in comments if wanted in c.body), None)
                if found is not None:
                    return Found(str(found.id))
                if len(comments) < _PER_PAGE:
                    return NotFound()
        return LookupUnknown(f"more than {_PAGES * _PER_PAGE} comments")


def _page(page: int) -> dict[str, str]:
    return {"per_page": str(_PER_PAGE), "page": str(page)}


def _hook(raw: RawRequest) -> _Hook | None:
    try:
        return _Hook.model_validate_json(raw.body)
    except ValidationError:
        return None


def _item(raw: RawRequest, hook: _Hook, delivery: VerifiedDelivery) -> Inbound:
    event = raw.headers.get("x-github-event")
    person = hook.sender
    if event != "issue_comment" or hook.action != "created" or person is None:
        return Ignore(kind="ignore")
    if person.type == "Bot" or hook.repository is None or hook.issue is None:
        return Ignore(kind="ignore")
    if hook.comment is None or not hook.comment.body.strip() or _MARK in hook.comment.body:
        return Ignore(kind="ignore")
    issuer = f"github:{delivery.installation_id}"
    who = Principal(issuer=issuer, tenant=delivery.tenant, subject=str(person.id))
    address = f"{hook.repository.full_name}#{hook.issue.number}"
    key = f"{delivery.delivery_id}#0"
    answer = _DECISION.fullmatch(hook.comment.body.strip())
    if answer is not None:
        return Decision(
            kind="decision",
            principal=who,
            address=address,
            item_key=key,
            challenge_id=answer["challenge"],
            decision="grant" if answer["verb"] == "approve" else "deny",
        )
    return Message(
        kind="message", principal=who, address=address, item_key=key, content=hook.comment.body
    )


def github(
    *,
    webhook_secret: Secret,
    token: Secret,
    agent: str,
    api: str = API,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GitHubChannel:
    """A GitHub channel for host(channels=...). `token` is an installation or fine-grained token
    with issues write; secrets are resolved on the host at use."""
    return GitHubChannel(webhook_secret, token, agent, api, transport)
