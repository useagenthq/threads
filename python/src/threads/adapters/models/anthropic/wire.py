"""The Messages API stream events, parsed at this boundary. The provider adds fields and event
types over time: unknown fields are ignored and unknown events skipped; known ones are strict."""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from threads.loop.model import StopReason


class Wire(BaseModel):
    # The provider adds fields over time: unknown ones are ignored, known ones are strict.
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True, strict=True)


class CacheCreation(Wire):
    """Cache writes split by TTL."""

    ephemeral_5m_input_tokens: int | None = None
    ephemeral_1h_input_tokens: int | None = None


class Counts(Wire):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation: CacheCreation | None = None


class Message(Wire):
    usage: Counts


class MessageStart(Wire):
    type: Literal["message_start"]
    message: Message


class BlockStart(Wire):
    type: Literal["content_block_start"]
    index: int
    content_block: dict[str, JsonValue]


class TextDelta(Wire):
    type: Literal["text_delta"]
    text: str


class JsonDelta(Wire):
    type: Literal["input_json_delta"]
    partial_json: str


class ThinkingDelta(Wire):
    type: Literal["thinking_delta"]
    thinking: str


class SignatureDelta(Wire):
    type: Literal["signature_delta"]
    signature: str


class CitationsDelta(Wire):
    type: Literal["citations_delta"]
    citation: dict[str, JsonValue]


type AnyDelta = TextDelta | JsonDelta | ThinkingDelta | SignatureDelta | CitationsDelta


class BlockDelta(Wire):
    type: Literal["content_block_delta"]
    index: int
    delta: Annotated[
        TextDelta | JsonDelta | ThinkingDelta | SignatureDelta | CitationsDelta,
        Field(discriminator="type"),
    ]


class BlockStop(Wire):
    type: Literal["content_block_stop"]
    index: int


class StopDelta(Wire):
    stop_reason: str | None = None


class MessageDelta(Wire):
    type: Literal["message_delta"]
    delta: StopDelta
    usage: Counts


class MessageStop(Wire):
    type: Literal["message_stop"]


class ErrorBody(Wire):
    type: str
    message: str


class StreamError(Wire):
    type: Literal["error"]
    error: ErrorBody


class Ping(Wire):
    type: Literal["ping"]


type Event = (
    MessageStart | BlockStart | BlockDelta | BlockStop | MessageDelta | MessageStop | StreamError
)
EVENT: TypeAdapter[Event | Ping] = TypeAdapter(
    Annotated[
        MessageStart
        | BlockStart
        | BlockDelta
        | BlockStop
        | MessageDelta
        | MessageStop
        | StreamError
        | Ping,
        Field(discriminator="type"),
    ]
)
KNOWN = frozenset(
    (
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
        "error",
        "ping",
    )
)


class Typed(Wire):
    type: str


class ToolStart(Wire):
    id: str
    name: str


class Location(Wire):
    type: Literal["char_location", "page_location", "content_block_location"]
    document_index: int
    document_title: str | None = None
    cited_text: str


class Web(Wire):
    type: Literal["web_search_result_location"]
    url: str
    title: str | None = None
    cited_text: str


CITATION: TypeAdapter[Location | Web] = TypeAdapter(
    Annotated[Location | Web, Field(discriminator="type")]
)
ARGS: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
STOPS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "refusal": "refusal",
    "pause_turn": "pause_turn",
    "model_context_window_exceeded": "context_window_exceeded",
}


def parse(data: str) -> Event | None:
    """One event, or None for a ping or an event type this adapter doesn't know yet."""
    if Typed.model_validate_json(data).type not in KNOWN:
        return None
    event = EVENT.validate_json(data)
    return None if isinstance(event, Ping) else event
