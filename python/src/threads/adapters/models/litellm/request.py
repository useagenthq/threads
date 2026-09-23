"""Render v1 to LiteLLM `acompletion` arguments (OpenAI Chat Completions shape).

- Line 0 `params` are LiteLLM completion fields, sent as pinned; the model name is the LiteLLM
  route (`provider/model`); `system` is the first message.
- Assistant text and tool calls of one line form one assistant message. A tool call's
  arguments are its recorded input as RFC 8785 JSON.
- A tool result is a `tool` message with its text; an error result is prefixed `Error: `.
  Chat Completions tool messages carry text only, so media in a result is refused.
- This bridge carries no provider continuation: a recorded reasoning or hosted tool part is
  refused before dispatch (use a first-party adapter for those). Citation parts annotate the
  model's own text and are not sent back.
"""

import base64
from dataclasses import dataclass, field
from typing import assert_never

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.adapters.models.render import (
    AssistantLine,
    Request,
    ResultLine,
    ToolLine,
    UnsupportedContentError,
    UserLine,
    read,
)
from threads.log import (
    AudioPart,
    CitationPart,
    DocumentPart,
    HostedToolPart,
    ImagePart,
    ReasoningPart,
    TextPart,
    ToolUsePart,
)
from threads.log.jcs import canonicalize
from threads.loop.model import ModelContext
from threads.result import Ok

type Message = dict[str, JsonValue]


@dataclass
class _Builder:
    context: ModelContext
    messages: list[JsonValue] = field(default_factory=list[JsonValue])

    async def user(self, line: UserLine) -> None:
        content = [await self.part(p) for p in line.content]
        self.messages.append({"role": "user", "content": list[JsonValue](content)})

    def assistant(self, line: AssistantLine) -> None:
        texts: list[str] = []
        calls: list[JsonValue] = []
        for part in line.content:
            match part:
                case TextPart(text=text):
                    texts.append(text)
                case ToolUsePart(call_id=call_id, name=name, input=args):
                    function: Message = {"name": name, "arguments": _canonical(dict(args))}
                    calls.append({"id": call_id, "type": "function", "function": function})
                case CitationPart():
                    pass
                case ReasoningPart() | HostedToolPart() | ImagePart():
                    raise UnsupportedContentError("continuation_unsupported", part.type)
                case _:
                    assert_never(part)
        said: Message = {"role": "assistant", "content": "".join(texts) if texts else None}
        if calls:
            said["tool_calls"] = calls
        self.messages.append(said)

    def result(self, line: ResultLine) -> None:
        texts: list[str] = ["Error: "] if line.is_error else []
        for part in line.content:
            match part:
                case TextPart(text=text):
                    texts.append(text)
                case CitationPart():
                    pass
                case ImagePart() | DocumentPart():
                    raise UnsupportedContentError("content_unsupported", f"{part.type} in a result")
                case _:
                    assert_never(part)
        if line.late is MISSING:
            self.messages.append(
                {"role": "tool", "tool_call_id": line.call_id, "content": "".join(texts)}
            )
            return
        late = f"Late result of tool call {line.call_id}:\n" + "".join(texts)
        self.messages.append({"role": "user", "content": late})

    async def part(self, part: TextPart | ImagePart | DocumentPart | AudioPart) -> Message:
        match part:
            case TextPart(text=text):
                return {"type": "text", "text": text}
            case ImagePart():
                return {"type": "image_url", "image_url": {"url": await self.data_url(part)}}
            case DocumentPart(ref=ref):
                name = part.title if part.title is not MISSING else ref.sha256
                file: Message = {"file_data": await self.data_url(part), "filename": name}
                return {"type": "file", "file": file}
            case AudioPart():
                raise UnsupportedContentError("content_unsupported", "audio_ref")
            case _:
                assert_never(part)

    async def data_url(self, part: ImagePart | DocumentPart) -> str:
        data = base64.b64encode(await read(self.context, part.ref)).decode("ascii")
        return f"data:{part.ref.media_type};base64,{data}"


def _canonical(value: JsonValue) -> str:
    text = canonicalize(value)
    if not isinstance(text, Ok):
        raise TypeError(text.error)
    return text.value


def _tool(tool: ToolLine) -> JsonValue:
    # A deferred stub has no schema: it is listed so tool_search can load it, and a call to it
    # fails pre-effect with tool_not_loaded.
    schema: Message = {"type": "object"} if tool.input_schema is MISSING else tool.input_schema
    function: Message = {"name": tool.name, "description": tool.description}
    function["parameters"] = schema
    return {"type": "function", "function": function}


async def build(request: Request, context: ModelContext) -> dict[str, JsonValue]:
    head = request.head
    builder = _Builder(context)
    if head.system:
        builder.messages.append({"role": "system", "content": head.system})
    for line in request.messages:
        match line:
            case UserLine():
                await builder.user(line)
            case AssistantLine():
                builder.assistant(line)
            case ResultLine():
                builder.result(line)
            case _:
                assert_never(line)
    body: dict[str, JsonValue] = {
        **head.params,
        "model": head.model.name,
        "messages": builder.messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        body["tools"] = [_tool(t) for t in request.tools]
    return body
