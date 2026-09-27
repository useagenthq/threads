"""Fetching a card and choosing what to speak, once. Whoever pins the result (a member's config
bytes, or a thread's `remote_card` event) keeps the bytes, so a card that changes later never
moves a conversation that has already started. `pin_card` is separate from `fetch_card` so pinned
bytes can be verified with no network, which is what makes replay hermetic."""

import ipaddress
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threadsai._generated.a2a_v1 import AgentCard
from threadsai.a2a.protocol.card import is_bearer, scheme_kind
from threadsai.a2a.protocol.client import Sending, fetch_bytes
from threadsai.a2a.protocol.version import speaks_1_0
from threadsai.a2a.protocol.wire import Binding, Wire, binding_of
from threadsai.log.digest import sha256_hex
from threadsai.web.guard import blocked, origin

IDEMPOTENT_SEND: Final = "https://threadsai.dev/a2a/ext/idempotent-send/v1"
PROVENANCE: Final = "https://threadsai.dev/a2a/provenance/v1"

MAX_CARD_BYTES: Final = 256 * 1024
"""A card over this many bytes is refused before it is parsed."""

_PREFERRED: Final[tuple[Binding, ...]] = ("JSONRPC", "HTTP+JSON")
"""The bindings we speak, in the order we prefer them when a card offers both."""

type PinFailureCode = Literal["remote_unavailable", "remote_unsupported", "remote_auth_unsupported"]


@dataclass(frozen=True, slots=True)
class PinFailure:
    """Why a remote cannot be used. A value, never a raise, and each message names the fix."""

    code: PinFailureCode
    message: str


@dataclass(frozen=True, slots=True)
class PinnedCard:
    bytes_: bytes
    sha256: str
    card: AgentCard
    wire: Wire
    dedup_window_ms: int | None
    """`window_ms` from our idempotent-send extension, when the card declares it."""
    streaming: bool


async def fetch_card(card_url: str, has_bearer: bool, sending: Sending) -> PinnedCard | PinFailure:
    got = await fetch_bytes(card_url, MAX_CARD_BYTES, sending)
    if got.bytes_ is None:
        return PinFailure("remote_unavailable", f"{card_url} could not be read: {got.why}")
    return pin_card(got.bytes_, card_url, has_bearer)


def pin_card(raw: bytes, card_url: str, has_bearer: bool) -> PinnedCard | PinFailure:
    """A card's exact bytes, parsed and checked."""
    try:
        card = AgentCard.model_validate_json(raw)
    except (ValidationError, ValueError) as error:
        return PinFailure("remote_unavailable", f"{card_url} is not an A2A 1.0 agent card: {error}")
    chosen = _choose(card)
    if chosen is None:
        return PinFailure(
            "remote_unsupported",
            f"{card.name} declares no 1.0 interface in a binding we speak ({_offered(card)})",
        )
    refused = _satisfiable(card, has_bearer=has_bearer)
    if refused is not None:
        return refused
    return PinnedCard(
        raw,
        sha256_hex(raw),
        card,
        chosen,
        _window_of(card),
        card.capabilities.streaming is True,
    )


def _unusable(url: str) -> str | None:
    """Why an interface URL is one we could never call, or None.

    The send-time guard would catch all of this, but by then the interface is pinned and every call
    on the remote answers `not_sent` forever, so a URL we cannot use must lose the selection rather
    than win it and fail later. `origin` and `blocked` are the guard's own, so there is one list of
    non-public ranges and one rule about credentials in a URL.

    A DNS name is deliberately not resolved here: a card is pinned once and used for a long time, so
    judging a name against one moment's DNS would pin the wrong answer. `vet` decides names at send
    time, when their addresses are actually known."""
    found = origin(url)
    if found is None:
        return f"{url} is not a URL we can call (https only, and no credentials in the URL)"
    scheme, host, _port = found
    if scheme != "https":
        return f"a remote is called over https, not {scheme}"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return f"{host} is not a public address" if blocked(host) else None


def _choose(card: AgentCard) -> Wire | None:
    """The first interface that speaks 1.0 in a binding we speak and a URL we could call, preferring
    JSON-RPC. Every candidate is vetted inside the loop, so a card that offers an unusable URL first
    and a good one second pins the good one."""
    for want in _PREFERRED:
        for offered in card.supportedInterfaces:
            if (
                speaks_1_0(offered.protocolVersion)
                and binding_of(offered.protocolBinding) == want
                and _unusable(offered.url) is None
            ):
                return Wire(offered.url, want)
    return None


def _offered(card: AgentCard) -> str:
    """Every interface a card offers, with the reason we passed over one where there is one."""
    return ", ".join(
        f"{i.protocolBinding} {i.protocolVersion}"
        + (f" ({why})" if (why := _unusable(i.url)) is not None else "")
        for i in card.supportedInterfaces
    )


def _satisfiable(card: AgentCard, *, has_bearer: bool) -> PinFailure | None:
    """Whether the credential we hold can satisfy the card. A card that declares no scheme asks for
    nothing, so an unauthenticated call is what it wants. Otherwise one scheme must be an HTTP
    `bearer`, because `bearer` is the only auth helper."""
    declared = {} if card.securitySchemes is MISSING else dict(card.securitySchemes)
    if not declared:
        return None
    bearer = any(is_bearer(scheme) for scheme in declared.values())
    if bearer and has_bearer:
        return None
    if bearer:
        return PinFailure(
            "remote_auth_unsupported",
            f'{card.name} requires a bearer token; pass auth=bearer(secret("..."))',
        )
    names = ", ".join(
        f"{name} ({scheme_kind(scheme) or 'unrecognised'})" for name, scheme in declared.items()
    )
    return PinFailure(
        "remote_auth_unsupported",
        f"{card.name} declares only {names}; bearer is the only scheme this adapter sends",
    )


def _window_of(card: AgentCard) -> int | None:
    """`window_ms` from our extension, when the card declares it with a usable value."""
    extensions = () if card.capabilities.extensions is MISSING else card.capabilities.extensions
    for extension in extensions:
        if extension.uri != IDEMPOTENT_SEND or extension.params is MISSING:
            continue
        raw = extension.params.get("window_ms")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
    return None
