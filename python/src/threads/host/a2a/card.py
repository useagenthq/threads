"""The agent card, a pure function of the config and the request's own origin. A card is what we
publish about an agent, so it is the one place where saying too much is the failure: no
instructions, no tool names, no model name, ever. The interface URLs come from the origin the card
was asked for, so there is no base-URL option to get wrong."""

from typing import Final
from urllib.parse import quote

from pydantic import JsonValue

from threads._generated.a2a_v1 import AgentCard
from threads.a2a.protocol import A2A_VERSION, IDEMPOTENT_SEND
from threads.host.a2a.config import Exposed, ExposedAgent
from threads.log.jcs import canonicalize
from threads.result import Err

DEDUP_WINDOW_MS: Final = 7 * 24 * 60 * 60 * 1000
"""The dedup window our extension declares. A receipt row is durable and never swept, so this is a
floor we can always keep rather than a limit we enforce; a peer may safely resend inside it."""

MODES: Final[tuple[str, ...]] = ("text/plain", "application/json")
"""Text and JSON only: a file part is refused where it arrives (ContentTypeNotSupportedError)."""

_EXTENSION_DOC: Final = (
    "A SendMessage that repeats a messageId from the same authenticated caller returns the "
    "original task and starts nothing."
)


def _card(exposed: Exposed, name: str, agent: ExposedAgent, origin: str) -> dict[str, JsonValue]:
    url = f"{origin}/a2a/{quote(name, safe='')}"
    interfaces: JsonValue = [
        {"url": url, "protocolBinding": binding, "protocolVersion": A2A_VERSION}
        for binding in ("JSONRPC", "HTTP+JSON")
    ]
    card: dict[str, JsonValue] = {
        "name": agent.name,
        "description": agent.description,
        "supportedInterfaces": interfaces,
        # The agent's pinned config hash: a card's version changes exactly when its agent does, and
        # a hash publishes nothing about the configuration it names.
        "version": agent.config_hash,
        "capabilities": {
            "streaming": True,
            # The cuts, stated in the card and not only in the docs.
            "pushNotifications": False,
            "extendedAgentCard": False,
            "extensions": [
                {
                    "uri": IDEMPOTENT_SEND,
                    "description": _EXTENSION_DOC,
                    "params": {"window_ms": DEDUP_WINDOW_MS},
                }
            ],
        },
        "securitySchemes": dict(exposed.security_schemes),
        "defaultInputModes": list(MODES),
        "defaultOutputModes": list(MODES),
        "skills": [
            {
                "id": name,
                "name": agent.name,
                "description": agent.description,
                "tags": ["text"],
            }
        ],
    }
    return card


def card_bytes(exposed: Exposed, name: str, agent: ExposedAgent, origin: str) -> bytes:
    """The card's bytes, canonical so the same config and origin always publish the same card."""
    built = _card(exposed, name, agent, origin)
    # Parsed, not trusted: the card crosses a trust boundary outwards, so a card we cannot parse is
    # a bug in this function and fails here rather than at a partner.
    AgentCard.model_validate(built)
    text = canonicalize(built)
    if isinstance(text, Err):
        raise ValueError(f"an agent card is JSON: {text.error}")
    return text.value.encode()
