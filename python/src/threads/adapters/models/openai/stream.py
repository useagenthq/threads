"""Responses API stream events to core chunks.

Text streams as deltas. Each finished output item becomes its parts at once: a message's text
(with URL citations after it), a completed function call, or a reasoning item stored whole,
encrypted content included, before its part names it. A function call the
response cut off is not `completed` and never exists.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import assert_never

from pydantic import JsonValue, TypeAdapter, ValidationError

from threads.adapters.models.openai.request import PROVIDER, REASONING_FORMAT
from threads.adapters.models.openai.wire import (
    ITEM,
    Counts,
    Event,
    Finished,
    FunctionCall,
    Incomplete,
    ItemDone,
    Message,
    OutputText,
    Reasoning,
    Refusal,
    StreamError,
    TextDelta,
    UrlCitation,
)
from threads.adapters.models.render import UnsupportedContentError, store_json
from threads.log import (
    CallId,
    CitationPart,
    OutputPart,
    ReasoningPart,
    TextPart,
    ToolUsePart,
    Usage,
)
from threads.loop.model import Delta, Done, ModelChunk, ModelContext, PartChunk, StopReason

_ARGS: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_INCOMPLETE: dict[str, StopReason] = {"max_output_tokens": "max_tokens"}
"""Any other incomplete reason (content_filter, max_messages, steered) is one the loop has no
name for: other, which ends the turn with error."""


class ProviderStreamError(Exception):
    """A `response.failed` or `error` event inside the stream."""

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class Assembler:
    """Folds one attempt's events into chunks."""

    context: ModelContext
    model: str
    called: bool = False
    refused: bool = False

    async def feed(self, event: Event) -> Sequence[ModelChunk]:
        match event:
            case TextDelta(delta=text):
                return (Delta(text),)
            case ItemDone(item=item):
                return [PartChunk(p) for p in await self._item(item)]
            case Finished(type="response.failed", response=response):
                failure = response.error
                raise ProviderStreamError(
                    failure.code if failure else None, failure.message if failure else ""
                )
            case Finished(type=kind, response=response):
                return (
                    Done(self._stop(kind, response.incomplete_details), _usage(response.usage)),
                )
            case StreamError(code=code, message=message):
                raise ProviderStreamError(code, message)
            case _:
                assert_never(event)

    async def _item(self, raw: dict[str, JsonValue]) -> list[OutputPart]:
        try:
            item = ITEM.validate_python(raw)
        except ValidationError as error:
            raise UnsupportedContentError(
                "content_unsupported", f"{raw.get('type')} item"
            ) from error
        match item:
            case Message(content=content):
                return [p for block in content for p in self._message(block)]
            case FunctionCall():
                return self._call(item)
            case Reasoning(summary=summary):
                return [await self._reasoning(raw, "\n\n".join(s.text for s in summary))]
            case _:
                assert_never(item)

    def _message(self, block: OutputText | Refusal) -> list[OutputPart]:
        match block:
            case OutputText(text=text, annotations=annotations):
                cites = [_citation(a) for a in annotations]
                return [TextPart(type="text", text=text), *cites]
            case Refusal(refusal=text):
                self.refused = True
                return [TextPart(type="text", text=text)]
            case _:
                assert_never(block)

    def _call(self, call: FunctionCall) -> list[OutputPart]:
        if call.status not in (None, "completed"):
            return []
        try:
            args = _ARGS.validate_json(call.arguments)
        except ValidationError:
            return []
        self.called = True
        return [
            ToolUsePart(type="tool_use", call_id=CallId(call.call_id), name=call.name, input=args)
        ]

    async def _reasoning(self, raw: dict[str, JsonValue], summary: str) -> ReasoningPart:
        ref = await store_json(self.context, raw)
        part = ReasoningPart(
            type="reasoning", provider=PROVIDER, model=self.model, format=REASONING_FORMAT, ref=ref
        )
        return part.model_copy(update={"summary": summary}) if summary else part

    def _stop(self, kind: str, incomplete: Incomplete | None) -> StopReason:
        if kind == "response.incomplete":
            return _INCOMPLETE.get((incomplete and incomplete.reason) or "", "other")
        if self.refused:
            return "refusal"
        return "tool_use" if self.called else "end_turn"


def _citation(raw: dict[str, JsonValue]) -> CitationPart:
    try:
        cite = UrlCitation.model_validate(raw)
    except ValidationError as error:
        raise UnsupportedContentError("content_unsupported", f"{raw.get('type')}") from error
    if cite.title is None:
        return CitationPart(type="citation", source_kind="web", source_id=cite.url)
    return CitationPart(type="citation", source_kind="web", source_id=cite.url, title=cite.title)


def _usage(counts: Counts | None) -> Usage:
    """OpenAI's input_tokens include cache reads and writes: they are split out, so input
    counts only uncached input. Reasoning tokens are part of output_tokens, as the core wants."""
    if counts is None:
        return Usage(
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            reasoning_tokens=None,
        )
    details = counts.input_tokens_details
    read = details.cached_tokens if details else None
    wrote = details.cache_write_tokens if details else None
    total = counts.input_tokens
    uncached = None if total is None or read is None else total - read - (wrote or 0)
    out = counts.output_tokens_details
    return Usage(
        input_tokens=uncached,
        output_tokens=counts.output_tokens,
        cache_read_tokens=read,
        cache_write_tokens=wrote,
        reasoning_tokens=out.reasoning_tokens if out else None,
    )
