"""The ids an outbound call derives rather than generates, and the provenance claim it may carry.

Nothing here is random: a crash and a re-dispatch must arrive at the same messageId, or a peer that
deduplicates would see two messages (30-a2a decision H30-1)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from threads.a2a.protocol import PROVENANCE
from threads.derive import derived_id

_MESSAGE: Final = "threads/a2a-message"
_CONTEXT: Final = "threads/a2a-context"
_REQUEST: Final = "threads/a2a-request"


def message_id_of(branch_id: str, call_id: str) -> str:
    """From `(branch_id, call_id)` **only**, never from the attempt: every attempt of one call
    carries this same id, which is what makes a peer's deduplication see one message."""
    return derived_id(_MESSAGE, [branch_id, call_id])


def context_id_of(thread_id: str, remote: str) -> str:
    """One A2A conversation per thread and remote. A tool never sends a context the model chose: a
    model that could name a context could reach another conversation with the same partner."""
    return derived_id(_CONTEXT, [thread_id, remote])


@dataclass(frozen=True, slots=True)
class Claim:
    """`provenance: "opaque"`: linkable across the calls of one request, and anonymous."""

    request: str
    hops: int


def request_id_of(tenant: str, root: str) -> str:
    """The digest's first 16 bytes as hex: enough to group one request's calls, too little to
    undo."""
    return derived_id(_REQUEST, [tenant, root]).replace("-", "")


def claim_of(tenant: str, root: str, hops: int) -> dict[str, JsonValue]:
    """The metadata an opaque call carries. `root` is the request id this thread itself arrived
    under when it came in over A2A, so one request's calls share an id across hops; otherwise the
    thread id. **No principal, tenant or subject is ever sent** — only a digest that includes the
    tenant."""
    claim: JsonValue = {"request": request_id_of(tenant, root), "hops": hops}
    return {PROVENANCE: claim}


def joined(parts: Sequence[str]) -> str:
    """The fields of a derivation, for a message that names them."""
    return ", ".join(parts)
