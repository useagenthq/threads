"""`remote()` and `bearer()`: what an app writes to name a partner's A2A agent. Neither does any
I/O. The card is fetched and pinned later — when a member starts, or when a thread first calls the
remote's tools — so a card that changes never moves a conversation already under way, and a host
that starts with an unreachable partner still starts."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from threads.agents.config import ConfigError
from threads.secrets import Secret, credential
from threads.web.guard import Resolve
from threads.web.http import Transport

if TYPE_CHECKING:
    from threads.a2a.outbound.tools import RemoteTool

type Provenance = Literal["opaque", "none"]

DEFAULT_TIMEOUT_MS: Final = 120_000

_NAME: Final = re.compile(r"^[a-z][a-z0-9_]*$")
_HTTPS: Final = "https:"


@dataclass(frozen=True, slots=True)
class A2aAuth:
    """Sends `Authorization: Bearer <secret>`. The only auth helper."""

    kind: Literal["bearer"]
    reveal: Callable[[], str]
    """Resolved on the host at setup; the value never reaches the log, a prompt or a sandbox."""


def bearer(value: Secret | str) -> A2aAuth:
    """`bearer(secret("PARTNER_TOKEN"))`. To rotate the token, change the variable the secret names
    and restart the host: a secret is resolved at setup, not per request."""
    return A2aAuth("bearer", credential("bearer", "secret", value, "A2A_BEARER_TOKEN"))


@dataclass(frozen=True, slots=True)
class Remote:
    """A partner's A2A agent, named like a local one."""

    name: str
    """Shares the host's agent-name space, so a clash with a local agent is a setup error."""
    card_url: str
    auth: A2aAuth | None
    cost_per_message: int
    """What one message costs us, reserved against every covering budget before it is sent.
    Recorded as declared, never as measured: nothing here observes a partner's spend."""
    provenance: Provenance
    timeout_ms: int
    transport: Transport | None = None
    """The host network every call to this remote goes through; tests inject one."""
    resolve: Resolve | None = None
    """How a host name becomes the address the transport connects to; tests inject one. None: the
    system resolver behind the SSRF guard."""

    def tools(self, *, name: str, description: str) -> tuple["RemoteTool", "RemoteTool"]:
        """This remote as two pinned tools — `name` sends one message, `name_status` reads a task it
        created — so they spread next to the thread's own tools:
        `tools=[*refunds.tools(name=..., description=...), invoice_status]`."""
        # Imported here: the tools are built from a Remote, so a module-level import would be
        # a cycle. remote() itself stays free of the outbound layer.
        from threads.a2a.outbound.tools import remote_tools  # noqa: PLC0415

        return remote_tools(self, name, description)


def remote(  # noqa: PLR0913 - the remote's options
    name: str,
    card_url: str,
    *,
    auth: A2aAuth | None = None,
    cost_per_message: int | None = None,
    provenance: Provenance | None = None,
    timeout_ms: int | None = None,
    transport: Transport | None = None,
    resolve: Resolve | None = None,
) -> Remote:
    if _NAME.match(name) is None:
        raise ConfigError("invalid_config", f"remote name {name!r} must match [a-z][a-z0-9_]*")
    if not card_url.lower().startswith(_HTTPS):
        scheme = card_url.split(":", 1)[0] if ":" in card_url else card_url
        raise ConfigError(
            "invalid_config", f"remote {name}: a card is fetched over https, not {scheme}:"
        )
    deadline = DEFAULT_TIMEOUT_MS if timeout_ms is None else timeout_ms
    if isinstance(deadline, bool) or deadline <= 0:
        raise ConfigError("invalid_config", f"remote {name}: timeout_ms must be a positive integer")
    cost = 0 if cost_per_message is None else cost_per_message
    if isinstance(cost, bool) or cost < 0:
        raise ConfigError(
            "invalid_config",
            f"remote {name}: cost_per_message must be a whole number of nanos, as usd() gives",
        )
    return Remote(name, card_url, auth, cost, provenance or "opaque", deadline, transport, resolve)
