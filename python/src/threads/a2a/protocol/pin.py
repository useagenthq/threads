"""Fetching a card and choosing what to speak, once. Whoever pins the result (a member's config
bytes, or a thread's `remote_card` event) keeps the bytes, so a card that changes later never
moves a conversation that has already started. `pin_card` is separate from `fetch_card` so pinned
bytes can be verified with no network, which is what makes replay hermetic."""

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import AgentCard
from threads.a2a.protocol.card import is_bearer, scheme_kind
from threads.a2a.protocol.client import Sending, fetch_bytes
from threads.a2a.protocol.version import speaks_1_0
from threads.a2a.protocol.wire import Binding, Wire, binding_of
from threads.log.digest import sha256_hex

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


def _choose(card: AgentCard) -> Wire | None:
    """The first interface that speaks 1.0 in a binding we speak, preferring JSON-RPC."""
    for want in _PREFERRED:
        for offered in card.supportedInterfaces:
            if speaks_1_0(offered.protocolVersion) and binding_of(offered.protocolBinding) == want:
                return Wire(offered.url, want)
    return None


def _offered(card: AgentCard) -> str:
    return ", ".join(f"{i.protocolBinding} {i.protocolVersion}" for i in card.supportedInterfaces)


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
