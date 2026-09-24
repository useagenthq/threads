"""Render v1 bytes parsed back into typed lines: the one input every model adapter maps from.

The request an adapter sends is a function of these lines and the artifact bytes they name, so a
recorded request always maps to the same provider request (spec/schema/README.md, "Render v1").
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING

from threads._strict_model import StrictModel
from threads.log import (
    AdapterRef,
    ArtifactRef,
    CallId,
    InputPart,
    ModelRef,
    OutputPart,
    ParseError,
    ResultPart,
)
from threads.log.jcs import canonicalize
from threads.loop.model import ModelContext, Rejected, Unencodable
from threads.render.artifacts import AnyRef
from threads.result import Err


class ToolLine(StrictModel):
    """A tool as Render v1 shows it. A deferred tool is a stub without a schema."""

    name: str
    description: str
    input_schema: dict[str, JsonValue] | MISSING = MISSING
    deferred: Literal[True] | MISSING = MISSING

    def parameters(self) -> dict[str, JsonValue]:
        """The input schema; a deferred stub has none, so it is listed with an open object so
        tool_search can load it, and a call to it fails pre-effect with tool_not_loaded."""
        return {"type": "object"} if self.input_schema is MISSING else self.input_schema


class Head(StrictModel):
    """Line 0: the declared prefix of the request's settings epoch."""

    adapter: AdapterRef
    model: ModelRef
    params: dict[str, JsonValue]
    system: str
    tools: Sequence[ToolLine]


class UserLine(StrictModel):
    role: Literal["user"]
    content: Sequence[InputPart]


class ToolsLine(StrictModel):
    role: Literal["tools"]
    tools: Sequence[ToolLine]


class AssistantLine(StrictModel):
    role: Literal["assistant"]
    content: Sequence[OutputPart]


class ResultLine(StrictModel):
    role: Literal["tool"]
    call_id: CallId
    is_error: bool
    late: Literal[True] | MISSING = MISSING
    content: Sequence[ResultPart]


type Message = UserLine | AssistantLine | ResultLine

_LINE: TypeAdapter[UserLine | ToolsLine | AssistantLine | ResultLine] = TypeAdapter(
    Annotated[UserLine | ToolsLine | AssistantLine | ResultLine, Field(discriminator="role")]
)


@dataclass(frozen=True, slots=True)
class Request:
    head: Head
    tools: Sequence[ToolLine]
    """The tool set the model sees now: the last `tools` line, else line 0's."""
    messages: Sequence[Message]


class UnsupportedContentError(Exception):
    """A part this adapter can't send. Raised before any byte leaves:
    a part is never converted or dropped silently."""

    def __init__(self, code: Unencodable, what: str) -> None:
        super().__init__(f"{code}: {what}")
        self.code: Unencodable = code


def late_marker(call_id: str) -> str:
    """The text every adapter sends before a late tool result's content."""
    return f"[late tool result: call_id={call_id}]"


def parse(body: bytes) -> Request:
    """Raises on bytes that are not Render v1: the loop rendered them, so that is a bug."""
    first, *rest = body.decode("utf-8").splitlines()
    head = Head.model_validate_json(first)
    tools: Sequence[ToolLine] = head.tools
    messages: list[Message] = []
    for text in rest:
        line = _LINE.validate_json(text)
        if isinstance(line, ToolsLine):
            tools = line.tools
        else:
            messages.append(line)
    return Request(head, tools, tuple(messages))


def canonical(value: JsonValue) -> str:
    """RFC 8785 text: tool arguments and stored provider JSON are always this form."""
    text = canonicalize(value)
    if isinstance(text, Err):
        raise TypeError(text.error)
    return text.value


async def store_json(context: ModelContext, value: JsonValue) -> ArtifactRef:
    """Exact provider JSON (a reasoning block or item), durable before a part names it."""
    return await context.put(canonical(value).encode("utf-8"), "application/json")


async def prepare[T](
    body: bytes,
    adapter: str,
    context: ModelContext,
    build: Callable[[Request, ModelContext], Awaitable[T]],
) -> tuple[Request, T] | Rejected:
    """The provider request for Render v1 `body`, or the typed rejection of a request this
    adapter can't encode (spec/api.json Model.send returns.errors): nothing left, so it is never
    an unknown outcome that gets re-sent."""
    request = parse(body)
    try:
        check_adapter(request.head, adapter)
        return request, await build(request, context)
    except UnsupportedContentError as refused:
        return Rejected(refused.code)


def check_adapter(head: Head, name: str) -> None:
    """A request pinned to another adapter (a fallback epoch this model can't serve) is refused
    before dispatch, never re-mapped."""
    if head.adapter.name != name:
        raise UnsupportedContentError(
            "continuation_unsupported", f"the epoch pins adapter {head.adapter.name!r}"
        )


async def read(context: ModelContext, ref: AnyRef) -> bytes:
    """Artifact bytes Render v1 already verified before the request: a failure now is a broken
    invariant, never a reason to send without them."""
    plain = ArtifactRef(sha256=ref.sha256, bytes=ref.bytes, media_type=ref.media_type)
    got = await context.read(plain)
    if isinstance(got, Err):
        raise _UnreadableError(got.error)
    return got.value


class _UnreadableError(Exception):
    def __init__(self, error: ParseError) -> None:
        super().__init__(f"{error.code}: {error.message}")
