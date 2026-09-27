"""The partner card the pin tests read, and the interface builders that vary it.

A card is a trust boundary in both directions, so both pin test files read one card shape: a
difference between them would be a difference in what the tests prove, not in what they cover."""

import json
from typing import Final

CARD_URL: Final = "https://partner.example/.well-known/agent-card.json"

RPC: Final = {
    "url": "https://partner.example/a2a/refunds",
    "protocolBinding": "JSONRPC",
    "protocolVersion": "1.0",
}
REST: Final = {
    "url": "https://partner.example/a2a/json",
    "protocolBinding": "HTTP+JSON",
    "protocolVersion": "1.0",
}


def rpc_at(url: str) -> dict[str, str]:
    return {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}


def rest_at(url: str) -> dict[str, str]:
    return {"url": url, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}


def card_of(**more: object) -> dict[str, object]:
    """A card that pins, with any field replaced."""
    base: dict[str, object] = {
        "name": "refunds",
        "description": "The partner's refunds desk.",
        "supportedInterfaces": [REST, RPC],
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "refunds",
                "name": "refunds",
                "description": "The partner's refunds desk.",
                "tags": ["refunds"],
            }
        ],
    }
    return {**base, **more}


def as_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value).encode()
