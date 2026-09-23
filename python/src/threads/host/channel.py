"""The channel adapter contract (spec/api.json `ChannelAdapter`).

An adapter (slack(), whatsapp(), github()) turns a provider's webhook into verified, keyed
inbound items and performs outbound ops. verify, parse, ack and render are pure; perform and
lookup reach the provider and report what is known about delivery.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field

from threads._generated.host_api_v1 import Input
from threads._strict_model import StrictModel
from threads.log import Event, JsonObject, ParseError, Principal
from threads.loop.model import LookupResult
from threads.result import Err, Ok
from threads.secrets import Secret

type LookupCapability = Literal["none", "nonfinal", "final"]


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    lookup: LookupCapability
    """Delivery lookup by effect key; `final` lets a not_found prove nothing was sent."""
    buttons: bool
    edits: bool
    files: bool
    direct_messages: bool
    dedup_window_ms: int | None = None
    """The provider deduplicates a re-send under the same key inside this window."""
    delivery: Literal["reliable", "best_effort"] = "reliable"


@dataclass(frozen=True, slots=True)
class RawRequest:
    headers: Mapping[str, str]
    """Header names lowercased."""
    body: bytes


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class VerifiedDelivery:
    """What the provider's signature proves: whose workspace or installation sent this."""

    tenant: str
    installation_id: str
    delivery_id: str


class Message(StrictModel):
    kind: Literal["message"]
    principal: Principal
    address: str = Field(min_length=1)
    """The conversation key; the host maps it to a thread."""
    item_key: str = Field(min_length=1)
    content: Input


class Decision(StrictModel):
    """An approval answer: a button carries only the challenge id."""

    kind: Literal["decision"]
    principal: Principal
    address: str = Field(min_length=1)
    item_key: str = Field(min_length=1)
    challenge_id: str = Field(min_length=1)
    decision: Literal["grant", "deny"]


class Control(StrictModel):
    kind: Literal["control"]
    principal: Principal
    address: str = Field(min_length=1)
    item_key: str = Field(min_length=1)
    command: Literal["cancel", "stop_when_idle"]


class Ignore(StrictModel):
    """Delivered but not for the agent: the bot's own message, a reaction, a status update."""

    kind: Literal["ignore"]


type Inbound = Annotated[Message | Decision | Control | Ignore, Field(discriminator="kind")]
"""An item of a verified batch. Every item but ignore carries the sender as a principal, the
conversation, and an item key: the provider's per-item id, else `<delivery_id>#<index>`."""


@dataclass(frozen=True, slots=True)
class Sent:
    platform_ref: str
    status: Literal["sent"] = "sent"


@dataclass(frozen=True, slots=True)
class DeliveryError:
    kind: Literal["rate_limited", "transient", "permanent"]
    sent: Literal["definite_not_sent", "outcome_unknown"]
    """definite_not_sent only when nothing can have reached the provider: a failure before the
    request was written, or a response saying the provider rejected it."""
    status: Literal["delivery_error"] = "delivery_error"


type DeliveryOutcome = Sent | DeliveryError


class ChannelAdapter(Protocol):
    """spec/api.json `ChannelAdapter`."""

    @property
    def agent(self) -> str:
        """The host agent key this channel routes to."""
        ...

    @property
    def capabilities(self) -> ChannelCapabilities: ...

    @property
    def limits(self) -> Mapping[str, int]:
        """Provider limits (message bytes, rate)."""
        ...

    @property
    def secrets(self) -> Mapping[str, Secret]:
        """The credentials perform needs, by name. The host resolves them at ready()
        (missing_secret) and passes their values as perform's credentials; they never reach a
        sandbox, the log or a prompt."""
        ...

    def verify(self, raw: RawRequest) -> Ok[VerifiedDelivery] | Err[ParseError]:
        """The provider's signature over the raw bytes; unverified otherwise."""
        ...

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]: ...

    def ack(self, raw: RawRequest) -> RawResponse:
        """The synchronous reply: a 200, or the provider's URL challenge."""
        ...

    def render(self, event: Event) -> Sequence[JsonObject]:
        """Outbound ops for a final response: `{"text": ...}` objects, the send tool's input."""
        ...

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        """Sends one op to `op["address"]`, embedding the effect key where the platform allows,
        through a transport that checks the run's fence at its send point."""
        ...

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        """found carries the platform_ref. `op` is what perform was given for this key (its
        recorded tool_call), since a platform lookup is scoped to its conversation."""
        ...


@runtime_checkable
class Challenged(Protocol):
    """The optional `challenge` of a ChannelAdapter: a provider's GET subscription check on the
    webhook URL (WhatsApp's hub.challenge). Pure; unverified answers 401."""

    def challenge(self, query: Mapping[str, str]) -> Ok[RawResponse] | Err[ParseError]: ...
