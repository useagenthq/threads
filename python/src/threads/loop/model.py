"""The model adapter protocol (spec/api.json `Model`, `ModelChunk`, `LookupResult`).

An adapter makes exactly one transport attempt per `send` (SDK retries off, ).
A provider rejection before any content is a `Rejected` chunk, never a raised error. Anything
that goes wrong after the attempt may have reached the provider is uncertainty: the loop records
the attempt abandoned as unknown.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import JsonValue

from threads.log import AdapterRef, ModelRef, OutputPart, Usage
from threads.log import Model as ModelLimits

type StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "other"]
type RejectReason = Literal[
    "rate_limited", "overloaded", "server_error", "prompt_too_long", "provider_error"
]
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
    provider_request_id: str | None = None
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


class Model(Protocol):
    """spec/api.json `Model`. `lookup` is present when `info.lookup` is not none."""

    @property
    def info(self) -> ModelInfo: ...

    def send(self, request: ModelRequest) -> AsyncIterator[ModelChunk]: ...

    async def lookup(self, request_id: str) -> LookupResult[ModelResponse]: ...
