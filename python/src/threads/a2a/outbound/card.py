"""The card a thread calls a partner through, resolved for one attempt.

The card is pinned at the thread's FIRST call of a remote and read back from that pin afterwards, so
a card that changes never moves a conversation already under way and a re-dispatch in a new process
calls the same interface with the same declared extensions."""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from threads.a2a.outbound.log import pinned_card_of
from threads.a2a.protocol import (
    PinFailure,
    PinnedCard,
    Sending,
    Wire,
    fetch_card,
    pin_card,
)
from threads.a2a.remote import Remote
from threads.log import ArtifactRef
from threads.loop.tools import Invocation
from threads.store import Draft

if TYPE_CHECKING:
    from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class Resolved:
    card: PinnedCard
    ref: ArtifactRef
    draft: Draft | None
    """The `remote_card` to append with this attempt's begin, on the thread's first call."""


@dataclass(frozen=True, slots=True)
class Unresolved:
    why: str


async def resolve_card(remote: Remote, call: Invocation, sending: Sending) -> Resolved | Unresolved:
    pinned = pinned_card_of(call.events(), remote.name)
    if pinned is not None:
        return await _from_pin(remote, call, pinned.card_ref, pinned.wire)
    got = await fetch_card(remote.card_url, remote.auth is not None, sending)
    if isinstance(got, PinFailure):
        return Unresolved(f"{got.code}: {got.message}")
    ref = await call.put(got.bytes_, "application/json")
    data: dict[str, JsonValue] = {
        "remote": remote.name,
        "card_ref": ref.model_dump(mode="json"),
        "interface_url": got.wire.url,
        "binding": got.wire.binding,
    }
    return Resolved(got, ref, Draft("remote_card", data, {"kind": "host"}, True))


async def _from_pin(
    remote: Remote, call: Invocation, ref: ArtifactRef, wire: Wire
) -> Resolved | Unresolved:
    """The pinned bytes, parsed again. Which interface to call is the event's answer, not this
    parse's: a card whose stored bytes offer two interfaces must keep being called on the one the
    thread pinned, whatever a re-run of the choice would prefer today."""
    raw = await call.read(ref)
    if raw is None:
        return Unresolved(f"remote_unavailable: the card {remote.name} pinned could not be read")
    card = pin_card(raw, remote.card_url, remote.auth is not None)
    if isinstance(card, PinFailure):
        return Unresolved(f"{card.code}: {card.message}")
    return Resolved(replace(card, wire=wire), ref, None)
