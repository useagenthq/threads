"""The model adapter protocol (spec/api.json `Model`, `ModelChunk`, `LookupResult`).

An adapter makes exactly one transport attempt per `send` (SDK retries off).
A provider rejection before any content is a `Rejected` chunk, never a raised error. Anything
that goes wrong after the attempt may have reached the provider is uncertainty: the loop records
the attempt abandoned as unknown.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import JsonValue

from threads.log import AdapterRef, ArtifactRef, BranchId, ModelRef, OutputPart, ParseError, Usage
from threads.log import Model as ModelLimits
from threads.result import Err, Ok

type StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "pause_turn",
    "context_window_exceeded",
    "other",
]
type ProviderRejection = Literal[
    "rate_limited", "overloaded", "server_error", "prompt_too_long", "provider_error"
]
"""A provider's rejection before any content: recorded as a failed attempt."""
type Unencodable = Literal["content_unsupported", "continuation_unsupported"]
"""A rendered part the adapter can't encode: nothing left, and nothing is re-sent."""
type Refused = Unencodable | Literal["transport_fence_unsupported"]
"""A send the adapter refused before anything left: recorded not_sent, ends the turn with its
code, never re-sent. transport_fence_unsupported: the model's transport bypasses the fence."""
type RejectReason = ProviderRejection | Literal["stale_epoch"] | Refused
"""spec/api.json Model.send returns.errors. stale_epoch: the fence refused at the send point."""
type LookupCapability = Literal["none", "nonfinal", "final"]


@dataclass(frozen=True, slots=True)
class ModelInfo:
    model: ModelRef
    adapter: AdapterRef
    params: Mapping[str, JsonValue]
    limits: ModelLimits
    """Window, output cap, billing bound and price."""
    lookup: LookupCapability
    """Response lookup by client request id."""
    accepts: tuple[Literal["text", "image_ref", "document_ref", "audio_ref"], ...] = ("text",)
    hosted_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelRequest:
    request_id: str
    """The client request id, `<branch_id>:<model_request event_id>`."""
    body: bytes
    """Render v1 bytes, exactly what `request_ref` holds."""


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: Sequence[OutputPart]
    stop_reason: StopReason
    usage: Usage
    provider_request_id: str | None
    """The provider's id for the request, or None when it gives none. A found lookup carries it:
    model_response_recovered records it."""


@dataclass(frozen=True, slots=True)
class Delta:
    text: str
    kind: Literal["delta"] = "delta"


@dataclass(frozen=True, slots=True)
class PartChunk:
    part: OutputPart
    kind: Literal["part"] = "part"


@dataclass(frozen=True, slots=True)
class Done:
    stop_reason: StopReason
    usage: Usage
    kind: Literal["done"] = "done"


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: RejectReason
    http_status: int | None = None
    retry_after_ms: int | None = None
    billing: Literal["not_billed", "unknown"] | None = None
    kind: Literal["rejected"] = "rejected"


type ModelChunk = Delta | PartChunk | Done | Rejected


@dataclass(frozen=True, slots=True)
class Found[T]:
    value: T
    status: Literal["found"] = "found"


@dataclass(frozen=True, slots=True)
class NotFound:
    """Final: the provider's contract proves nothing was created or sent."""

    status: Literal["not_found"] = "not_found"


@dataclass(frozen=True, slots=True)
class NotFoundNonfinal:
    status: Literal["not_found_nonfinal"] = "not_found_nonfinal"


@dataclass(frozen=True, slots=True)
class LookupUnknown:
    reason: str
    status: Literal["unknown"] = "unknown"


type LookupResult[T] = Found[T] | NotFound | NotFoundNonfinal | LookupUnknown


class ModelContext(Protocol):
    """spec/api.json `ModelContext`: what the loop hands an adapter for one attempt."""

    @property
    def branch_id(self) -> BranchId: ...

    @property
    def epoch(self) -> int:
        """The lease epoch this attempt is dispatched under."""
        ...

    async def fence(self) -> Ok[None] | Err[ParseError]:
        """Re-checks the lease. The adapter awaits it at its real network send point, after any
        SDK queueing, and sends nothing when it fails."""
        ...

    async def read(self, ref: ArtifactRef) -> Ok[bytes] | Err[ParseError]:
        """The verified bytes of an artifact a Render v1 line references."""
        ...

    async def put(self, data: bytes, media_type: str) -> ArtifactRef:
        """Stores exact provider bytes; returns their ref once durable."""
        ...


@dataclass(frozen=True, slots=True)
class StaleEpoch:
    """spec/api.json `Model.lookup` returns.errors: the fence refused at the lookup's send point,
    so nothing was asked. Recovery ends the run `branch_busy`."""

    message: str
    code: Literal["stale_epoch"] = "stale_epoch"


class Model(Protocol):
    """spec/api.json `Model`. A model whose `info.lookup` is not none also implements
    `LooksUp`; check() and the first run refuse one that doesn't (capability_missing)."""

    @property
    def info(self) -> ModelInfo: ...

    def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]: ...


@runtime_checkable
class LooksUp(Protocol):
    """spec/api.json `Model.lookup`, the optional capability of a `Model`: recover a response
    after a crash by client request id. `lookup` must be a method: setup checks that it is
    callable, since an `isinstance` check against this protocol only sees the name."""

    async def lookup(
        self, request_id: str, context: ModelContext
    ) -> Ok[LookupResult[ModelResponse]] | Err[StaleEpoch]:
        """Awaits `context.fence()` at its real network send point, like `send`; a refused
        fence is `Err(StaleEpoch)`."""
        ...
