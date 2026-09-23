"""LiteLLM stream chunks (OpenAI Chat Completions shape) to core chunks.

LiteLLM's chunk objects are loosely typed, so each one is parsed here into strict models.
Text streams as deltas; tool calls are assembled by index and emitted with the stop reason,
and a call cut off by `length` never exists.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from threads.log import CallId, OutputPart, TextPart, ToolUsePart, Usage
from threads.loop.model import Delta, Done, ModelChunk, PartChunk, StopReason


class _Wire(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True, strict=True)


class _Function(_Wire):
    name: str | None = None
    arguments: str | None = None


class _Call(_Wire):
    index: int
    id: str | None = None
    function: _Function | None = None


class _Delta(_Wire):
    content: str | None = None
    tool_calls: list[_Call] | None = None


class _Choice(_Wire):
    delta: _Delta | None = None
    finish_reason: str | None = None


class _PromptDetails(_Wire):
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None


class _CompletionDetails(_Wire):
    reasoning_tokens: int | None = None


class _Counts(_Wire):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_tokens_details: _PromptDetails | None = None
    completion_tokens_details: _CompletionDetails | None = None


class Chunk(_Wire):
    choices: list[_Choice] = Field(default_factory=list[_Choice])
    usage: _Counts | None = None


_ARGS: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_STOPS: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
}


@dataclass
class _Open:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list[str])


@dataclass
class Assembler:
    """Folds one attempt's chunks; `finish` closes it once the stream ends."""

    text: list[str] = field(default_factory=list[str])
    calls: dict[int, _Open] = field(default_factory=dict[int, _Open])
    stop: StopReason | None = None
    usage: _Counts | None = None

    def feed(self, raw: object) -> Sequence[ModelChunk]:
        """`raw` is LiteLLM's chunk object: its JSON form is parsed, never its attributes."""
        chunk = Chunk.model_validate(
            raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
        )
        if chunk.usage is not None:
            self.usage = chunk.usage
        out: list[ModelChunk] = []
        for choice in chunk.choices[:1]:
            if choice.finish_reason is not None:
                self.stop = _STOPS.get(choice.finish_reason, "other")
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                self.text.append(delta.content)
                out.append(Delta(delta.content))
            for call in delta.tool_calls or ():
                self._call(call)
        return out

    def _call(self, call: _Call) -> None:
        open_ = self.calls.setdefault(call.index, _Open())
        open_.id = call.id or open_.id
        if call.function is not None:
            open_.name = call.function.name or open_.name
            open_.arguments.append(call.function.arguments or "")

    def finish(self) -> Sequence[ModelChunk]:
        """The parts and Done, or nothing when the stream ended without a finish reason."""
        if self.stop is None:
            return ()
        parts: list[OutputPart] = []
        if self.text:
            parts.append(TextPart(type="text", text="".join(self.text)))
        calls = [call for _, call in sorted(self.calls.items())]
        if self.stop == "max_tokens":
            calls = calls[:-1]  # the call being generated when the cap hit
        for call in calls:
            try:
                args = _ARGS.validate_json("".join(call.arguments) or "{}")
            except ValidationError:
                continue
            use = ToolUsePart(type="tool_use", call_id=CallId(call.id), name=call.name, input=args)
            parts.append(use)
        return [*(PartChunk(p) for p in parts), Done(self.stop, _usage(self.usage))]


def _usage(counts: _Counts | None) -> Usage:
    """LiteLLM normalizes to OpenAI usage: prompt_tokens include cache reads and writes, which
    are split out when reported; with no cache details, prompt_tokens are the input.
    completion_tokens include reasoning. Absent is None, never 0."""
    if counts is None:
        return Usage(
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            reasoning_tokens=None,
        )
    details = counts.prompt_tokens_details
    read = details.cached_tokens if details else None
    wrote = details.cache_write_tokens if details else None
    total = counts.prompt_tokens
    uncached = None if total is None or read is None else total - read - (wrote or 0)
    out = counts.completion_tokens_details
    return Usage(
        input_tokens=uncached if details else total,
        output_tokens=counts.completion_tokens,
        cache_read_tokens=read,
        cache_write_tokens=wrote,
        reasoning_tokens=out.reasoning_tokens if out else None,
    )
