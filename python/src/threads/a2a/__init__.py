"""`from threads.a2a import bearer, remote`: A2A 1.0, both directions.

`remote()` names a partner's agent; the protocol core is `threads.a2a.protocol`, and the exposed
side lives in `threads.host`, which serves a host agent from the same schemas. No runtime
dependency beyond core — the adapter owns the message id, the attempt record and the exact bytes,
which an SDK hides."""

from threads.a2a.protocol import (
    IDEMPOTENT_SEND,
    MAX_CARD_BYTES,
    PROVENANCE,
    A2aFault,
    PinFailure,
    PinnedCard,
    fetch_card,
    pin_card,
)
from threads.a2a.remote import (
    DEFAULT_TIMEOUT_MS,
    A2aAuth,
    Provenance,
    Remote,
    bearer,
    remote,
)

__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "IDEMPOTENT_SEND",
    "MAX_CARD_BYTES",
    "PROVENANCE",
    "A2aAuth",
    "A2aFault",
    "PinFailure",
    "PinnedCard",
    "Provenance",
    "Remote",
    "bearer",
    "fetch_card",
    "pin_card",
    "remote",
]
