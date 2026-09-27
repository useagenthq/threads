"""Responses API stream events, parsed at this boundary. The provider adds fields and event
types over time: unknown fields are ignored and unknown events skipped; known ones are strict."""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter


class Wire(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True, strict=True)


class InputDetails(Wire):
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None


class OutputDetails(Wire):
    reasoning_tokens: int | None = None


class Counts(Wire):
    input_tokens: int | None = None
    input_tokens_details: InputDetails | None = None
    output_tokens: int | None = None
    output_tokens_details: OutputDetails | None = None


class Incomplete(Wire):
    reason: str | None = None


class Failure(Wire):
    code: str | None = None
    message: str = ""


class Response(Wire):
    usage: Counts | None = None
    incomplete_details: Incomplete | None = None
    error: Failure | None = None


class TextDelta(Wire):
    type: Literal["response.output_text.delta"]
    content_index: int
    delta: str


class ItemDone(Wire):
    type: Literal["response.output_item.done"]
    item: dict[str, JsonValue]


class Finished(Wire):
    type: Literal["response.completed", "response.incomplete", "response.failed"]
    response: Response


class StreamError(Wire):
    type: Literal["error"]
    code: str | None = None
    message: str = ""


type Event = TextDelta | ItemDone | Finished | StreamError

_EVENT: TypeAdapter[Event] = TypeAdapter(
    Annotated[TextDelta | ItemDone | Finished | StreamError, Field(discriminator="type")]
)
_KNOWN = frozenset(
    (
        "response.output_text.delta",
        "response.output_item.done",
        "response.completed",
        "response.incomplete",
        "response.failed",
        "error",
    )
)


class _Typed(Wire):
    type: str


def parse(data: str) -> Event | None:
    """One event, or None for an event this adapter doesn't read (progress, done echoes)."""
    if _Typed.model_validate_json(data).type not in _KNOWN:
        return None
    return _EVENT.validate_json(data)


class OutputText(Wire):
    type: Literal["output_text"]
    text: str
    annotations: list[dict[str, JsonValue]] = Field(default_factory=list[dict[str, JsonValue]])


class Refusal(Wire):
    type: Literal["refusal"]
    refusal: str


class Message(Wire):
    type: Literal["message"]
    content: list[Annotated[OutputText | Refusal, Field(discriminator="type")]]


class FunctionCall(Wire):
    type: Literal["function_call"]
    call_id: str
    name: str
    arguments: str
    status: str | None = None


class SummaryText(Wire):
    text: str


class Reasoning(Wire):
    type: Literal["reasoning"]
    summary: list[SummaryText] = Field(default_factory=list[SummaryText])


class UrlCitation(Wire):
    type: Literal["url_citation"]
    url: str
    title: str | None = None


ITEM: TypeAdapter[Message | FunctionCall | Reasoning] = TypeAdapter(
    Annotated[Message | FunctionCall | Reasoning, Field(discriminator="type")]
)
