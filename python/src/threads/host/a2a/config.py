"""`host(a2a=...)`: which host agents are served as A2A agents, and what a card may say about them.
Validated at ready(), beside the channel and schedule checks, because every refusal here names a fix
the operator makes in the config, never something a caller can trigger."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, NotRequired, TypedDict

from pydantic import JsonValue

from threads.agents.usd import usd
from threads.log import Budget


class A2aExposure(TypedDict):
    """One exposed agent's card entry and ceiling."""

    description: str
    """Required: the card requires one, and an agent's instructions must never be published.
    Instructions, tool names and model names never appear in the card."""
    budget: NotRequired[Mapping[str, JsonValue]]
    """What one exposed task may spend. Default: $1.00 and ten minutes of wall clock."""


class A2aOptions(TypedDict):
    """`host(a2a=...)`."""

    expose: Mapping[str, A2aExposure]
    base_url: str
    """Where partners reach this host, as a scheme and authority:
    `https://agents.acme.example`. The card's interface URLs are built from it, and it is required
    because there is no safe way to guess it. A request's own URL comes from the request line and
    the `Host` header, so behind a reverse proxy - the normal deployment - a card derived from a
    request advertises the internal origin and no partner can reach us; pointed at an attacker, it
    is an origin a caller chooses for a document whose whole job is to say where to send work."""
    security_schemes: NotRequired[Mapping[str, JsonValue]]
    """Replaces the default bearer scheme for OAuth2, OpenID Connect, an API key or mTLS. The card
    only declares the scheme; the host's `authenticate` is what checks a caller."""


def default_budget() -> Budget:
    """spec/api.json: stated here, not only in code, because a card's reader cannot see it."""
    return Budget.model_validate({"max_cost_nanos": usd(1), "max_wall_ms": 600_000})


DEFAULT_SCHEMES: Final[Mapping[str, JsonValue]] = {
    "bearer": {"httpAuthSecurityScheme": {"scheme": "bearer"}}
}


@dataclass(frozen=True, slots=True)
class ExposedAgent:
    name: str
    description: str
    budget: Budget
    config_hash: str
    """The agent's pinned config hash: the card's version, so a card changes when the agent does."""


@dataclass(frozen=True, slots=True)
class Exposed:
    agents: Mapping[str, ExposedAgent]
    security_schemes: Mapping[str, JsonValue]
    base_url: str
    """The checked `base_url`, as its origin: what every card's interface URLs are built from."""
