"""Messages API stream events to core chunks. Every event is parsed at this boundary.

Text streams as deltas at once. Parts are held until the stop reason is known: at max_tokens
the block being generated was cut off, and a cut-off tool call or thinking block never exists.
Thinking blocks are stored whole, signature included, before their part names
them.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, assert_never

from pydantic import JsonValue, ValidationError

from threads.adapters.models.anthropic.caching import Ttl
from threads.adapters.models.anthropic.request import PROVIDER
from threads.adapters.models.anthropic.wire import (
    ARGS,
    CITATION,
    STOPS,
    BlockDelta,
    BlockStart,
    BlockStop,
    CitationsDelta,
    Counts,
    Event,
    JsonDelta,
    Location,
    MessageDelta,
    MessageStart,
    MessageStop,
    SignatureDelta,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolStart,
    Web,
)
from threads.adapters.models.render import UnsupportedContentError, store_json
from threads.log import (
    CallId,
    CitationPart,
    HostedToolPart,
    OutputPart,
    ReasoningPart,
    TextPart,
    ToolUsePart,
    Usage,
)
from threads.loop.model import Delta, Done, ModelChunk, ModelContext, PartChunk, StopReason


@dataclass
class _Block:
    start: dict[str, JsonValue]
    text: list[str] = field(default_factory=list[str])
    citations: list[CitationPart] = field(default_factory=list[CitationPart])
    signature: str = ""


@dataclass
class Assembler:
    """Folds one attempt's events into chunks."""

    context: ModelContext
    model: str
    documents: Sequence[str]
    ttl: Ttl | None
    """The pinned prompt_cache TTL: a cache write under any other is unknown."""
    open: dict[int, _Block] = field(default_factory=dict[int, _Block])
    parts: list[list[OutputPart]] = field(default_factory=list[list[OutputPart]])
    """Each finished block's parts, in block order."""
    usage: Counts = field(default_factory=Counts)
    stop: StopReason | None = None

    async def feed(self, event: Event) -> Sequence[ModelChunk]:
        match event:
            case MessageStart(message=message):
                self.usage = _merge(self.usage, message.usage)
            case BlockStart(index=index, content_block=block):
                self.open[index] = _Block(block)
            case BlockDelta(index=index, delta=delta):
                return self._delta(index, delta)
            case BlockStop(index=index):
                self.parts.append(await self._finish(self.open.pop(index)))
            case MessageDelta(delta=delta, usage=usage):
                self.usage = _merge(self.usage, usage)
                self.stop = STOPS.get(delta.stop_reason or "", "other")
            case MessageStop():
                return self._done()
            case StreamError(error=error):
                raise ProviderStreamError(error.type, error.message)
            case _:
                assert_never(event)
        return ()

    def _delta(
        self,
        index: int,
        delta: TextDelta | JsonDelta | ThinkingDelta | SignatureDelta | CitationsDelta,
    ) -> Sequence[ModelChunk]:
        block = self.open[index]
        match delta:
            case TextDelta(text=text):
                block.text.append(text)
                # Blocks close in order, so the text is the next part; with another block open
                # its index isn't known, and it arrives whole at commit.
                if len(self.open) > 1:
                    return ()
                return (Delta(sum(len(p) for p in self.parts), text),)
            case JsonDelta(partial_json=text) | ThinkingDelta(thinking=text):
                block.text.append(text)
            case SignatureDelta(signature=signature):
                block.signature += signature
            case CitationsDelta(citation=citation):
                block.citations.append(self._citation(citation))
            case _:
                assert_never(delta)
        return ()

    def _citation(self, raw: dict[str, JsonValue]) -> CitationPart:
        location = CITATION.validate_python(raw)
        match location:
            case Web(url=url, title=title, cited_text=cited):
                return _cite("web", url, title, cited)
            case Location(document_index=index, document_title=title, cited_text=cited):
                return _cite("document", self.documents[index], title, cited)
            case _:
                assert_never(location)

    async def _finish(self, block: _Block) -> list[OutputPart]:
        kind = block.start.get("type")
        text = "".join(block.text)
        match kind:
            case "text":
                return [TextPart(type="text", text=text), *block.citations]
            case "tool_use":
                return _tool_use(block.start, text)
            case "thinking":
                whole: JsonValue = {
                    "type": "thinking",
                    "thinking": text,
                    "signature": block.signature,
                }
                return [await self._reasoning("thinking", whole)]
            case "redacted_thinking":
                return [await self._reasoning("redacted_thinking", block.start)]
            case str():
                return [await self._hosted(kind, block, text)]
            case _:
                raise UnsupportedContentError("content_unsupported", f"{kind} block")

    async def _hosted(self, kind: str, block: _Block, text: str) -> HostedToolPart:
        """A server tool use or result block, kept whole and never dispatched.
        A server_tool_use streams its input as JSON deltas; result blocks arrive whole."""
        whole = dict(block.start)
        if text:
            whole["input"] = ARGS.validate_json(text)
        name = whole.get("name")
        ref = await store_json(self.context, whole)
        return HostedToolPart(
            type="hosted_tool",
            provider=PROVIDER,
            model=self.model,
            format=kind,
            name=name if isinstance(name, str) else kind,
            ref=ref,
        )

    async def _reasoning(self, form: str, block: JsonValue) -> ReasoningPart:
        ref = await store_json(self.context, block)
        return ReasoningPart(
            type="reasoning", provider=PROVIDER, model=self.model, format=form, ref=ref
        )

    def _done(self) -> Sequence[ModelChunk]:
        stop = self.stop or "other"
        blocks = self.parts
        # At max_tokens the last block was still being generated: only text survives a cut.
        if stop == "max_tokens" and blocks and not _is_text(blocks[-1]):
            blocks = blocks[:-1]
        chunks: list[ModelChunk] = [PartChunk(p) for parts in blocks for p in parts]
        chunks.append(Done(stop, _usage(self.usage, self.ttl)))
        return chunks


def _is_text(parts: Sequence[OutputPart]) -> bool:
    return bool(parts) and isinstance(parts[0], TextPart)


class ProviderStreamError(Exception):
    """An `error` event inside the stream."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


def _tool_use(start: dict[str, JsonValue], text: str) -> list[OutputPart]:
    """A call whose input is not a whole JSON object was cut off: it never exists."""
    head = ToolStart.model_validate(start)
    try:
        args = ARGS.validate_json(text) if text else ARGS.validate_python(start.get("input"))
    except ValidationError:
        return []
    return [ToolUsePart(type="tool_use", call_id=CallId(head.id), name=head.name, input=args)]


def _cite(
    kind: Literal["web", "document"], source: str, title: str | None, cited: str
) -> CitationPart:
    if title is None:
        return CitationPart(type="citation", source_kind=kind, source_id=source, cited_text=cited)
    return CitationPart(
        type="citation", source_kind=kind, source_id=source, title=title, cited_text=cited
    )


def _merge(old: Counts, new: Counts) -> Counts:
    """message_delta repeats usage as running totals: the latest known value of each wins."""

    def pick(a: int | None, b: int | None) -> int | None:
        return a if b is None else b

    return Counts(
        input_tokens=pick(old.input_tokens, new.input_tokens),
        output_tokens=pick(old.output_tokens, new.output_tokens),
        cache_creation_input_tokens=pick(
            old.cache_creation_input_tokens, new.cache_creation_input_tokens
        ),
        cache_read_input_tokens=pick(old.cache_read_input_tokens, new.cache_read_input_tokens),
        cache_creation=old.cache_creation if new.cache_creation is None else new.cache_creation,
    )


def _usage(u: Counts, ttl: Ttl | None) -> Usage:
    """Anthropic's input_tokens already exclude cache reads and writes; its usage profile has
    both cache categories, so they are present, and None when they didn't arrive."""
    return Usage(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=u.cache_read_input_tokens,
        cache_write_tokens=_priced_writes(u, ttl),
    )


def _priced_writes(u: Counts, ttl: Ttl | None) -> int | None:
    """Cache writes all billed at the pinned TTL's price, or None (unknown) when the provider
    reports any under the other TTL: a number priced at the wrong rate would understate cost."""
    split = u.cache_creation
    other = None
    if split is not None and ttl is not None:
        other = split.ephemeral_5m_input_tokens if ttl == "1h" else split.ephemeral_1h_input_tokens
    return None if (other or 0) > 0 else u.cache_creation_input_tokens
