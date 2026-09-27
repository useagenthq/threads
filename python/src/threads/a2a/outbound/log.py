"""What an outbound call reads back out of its own thread's log.

Process memory is not allowed to decide any of it: a re-dispatch after a crash runs in a new
process, and it must pin the same card, send the same bytes and reconcile against the same context
as the attempt it follows."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING

from threads.a2a.protocol import Wire
from threads.log import (
    ArtifactRef,
    EffectCommitEvent,
    Event,
    RemoteCallEvent,
    RemoteCardEvent,
    RemoteTaskStateEvent,
    UserInputEvent,
)


@dataclass(frozen=True, slots=True)
class Pinned:
    """The card a thread pinned for one remote, and the one interface it therefore calls."""

    card_ref: ArtifactRef
    wire: Wire


def pinned_card_of(events: Sequence[Event], remote: str) -> Pinned | None:
    found = [e for e in events if isinstance(e, RemoteCardEvent) and e.data.remote == remote]
    if not found:
        return None
    data = found[-1].data
    return Pinned(data.card_ref, Wire(data.interface_url, data.binding))


@dataclass(frozen=True, slots=True)
class Called:
    """The `remote_call` of one call id: the stored bytes and ids every attempt of it reuses."""

    remote: str
    message_id: str
    context_id: str
    task_id: str | None
    request_ref: ArtifactRef


def called_in(events: Sequence[Event], call_id: str) -> Called | None:
    for e in events:
        if isinstance(e, RemoteCallEvent) and e.data.call_id == call_id:
            d = e.data
            task = None if d.task_id is MISSING else d.task_id
            return Called(d.remote, d.message_id, d.context_id, task, d.request_ref)
    return None


def _own_calls(events: Sequence[Event], remote: str) -> set[str]:
    return {
        e.data.call_id for e in events if isinstance(e, RemoteCallEvent) and e.data.remote == remote
    }


def owns_task(events: Sequence[Event], remote: str, task_id: str) -> bool:
    """Whether **this thread's own log** created `task_id` with `remote`. All tenants' calls share
    one host credential, so without this check the partner would happily show any caller any
    task."""
    mine = _own_calls(events, remote)
    for e in events:
        if isinstance(e, RemoteCallEvent) and e.data.remote == remote and e.data.task_id == task_id:
            return True
        if isinstance(e, RemoteTaskStateEvent | EffectCommitEvent) and e.data.call_id in mine:
            named = (
                e.data.task_id if isinstance(e, RemoteTaskStateEvent) else e.data.provider_receipt
            )
            if named == task_id:
                return True
    return False


def task_owner(events: Sequence[Event], remote: str, task_id: str) -> str | None:
    """The call whose `remote_call` created `task_id`: the call an observation of it belongs to."""
    mine = _own_calls(events, remote)
    for e in events:
        if (
            isinstance(e, EffectCommitEvent)
            and e.data.provider_receipt == task_id
            and e.data.call_id in mine
        ):
            return e.data.call_id
    return None


def observed(events: Sequence[Event], call_id: str) -> frozenset[str]:
    """The states already recorded for a call, so the same state and bytes are not appended
    twice."""
    seen: set[str] = set()
    for e in events:
        if isinstance(e, RemoteTaskStateEvent) and e.data.call_id == call_id:
            ref = "" if e.data.status_ref is MISSING else e.data.status_ref.sha256
            seen.add(f"{e.data.state}:{ref}")
    return frozenset(seen)


def tenant_of(events: Sequence[Event]) -> str:
    """The tenant this thread belongs to, read from its own log rather than from the running
    process, so the bytes a re-dispatch sends are a function of the log alone."""
    for e in events:
        if isinstance(e, UserInputEvent):
            return e.actor.principal.tenant
    return ""


@dataclass(frozen=True, slots=True)
class InboundClaim:
    """The provenance this thread itself arrived under, when it came in over A2A."""

    request: str | None
    hops: int


def inbound_claim(events: Sequence[Event]) -> InboundClaim:
    """One request's calls share a provenance id across hops. The claim is untrusted, so only its
    shape is read: it grants no authority and decides nothing but this number."""
    for e in events:
        if not isinstance(e, UserInputEvent) or e.data.a2a is MISSING:
            continue
        claims = e.data.a2a.claims
        if claims is MISSING:
            return InboundClaim(None, 0)
        request = claims.get("request")
        hops = claims.get("hops")
        return InboundClaim(
            request if isinstance(request, str) else None,
            hops if isinstance(hops, int) and not isinstance(hops, bool) and hops >= 0 else 0,
        )
    return InboundClaim(None, 0)
