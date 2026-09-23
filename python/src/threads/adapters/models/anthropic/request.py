"""Render v1 to a Messages API request body: a pure function of the lines and artifact bytes.

- Line 0 `params` are Messages API fields, sent as pinned; `system` and the current tool set
  come from the render. Adapter setting `citations: true` enables citations on documents.
- Consecutive lines of one role merge into one message; in a user message, tool results come
  first (the API requires it), everything else keeps its recorded order.
- Recorded thinking blocks go back byte for byte from their artifact. Citation parts annotate
  the model's own text and are not sent back. Anything this API can't carry raises
  `UnsupportedContentError` before dispatch.
"""

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import assert_never

from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING

from threads.adapters.models.hosted import pinned
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
from threads.loop.model import ModelContext

PROVIDER = "anthropic"
REASONING_FORMATS = ("thinking", "redacted_thinking")
_BLOCK: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

type Block = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Body:
    json: dict[str, JsonValue]
    documents: Sequence[str]
    """The sha256 of each document block, in request order: citations name them by index."""


@dataclass
class _Builder:
    context: ModelContext
    citations: bool
    messages: list[tuple[str, list[Block]]] = field(default_factory=list[tuple[str, list[Block]]])
    documents: list[str] = field(default_factory=list[str])

    def add(self, role: str, blocks: list[Block]) -> None:
        if self.messages and self.messages[-1][0] == role:
            self.messages[-1][1].extend(blocks)
        else:
            self.messages.append((role, blocks))

    async def user(self, line: UserLine) -> None:
        self.add("user", [await self.part(p) for p in line.content])

    async def assistant(self, line: AssistantLine) -> None:
        blocks: list[Block] = []
        for part in line.content:
            match part:
                case TextPart(text=text):
                    blocks.append({"type": "text", "text": text})
                case ToolUsePart(call_id=call_id, name=name, input=args):
                    use: Block = {"type": "tool_use", "id": call_id, "name": name}
                    use["input"] = dict(args)
                    blocks.append(use)
                case ReasoningPart():
                    blocks.append(await self.reasoning(part))
                case CitationPart():
                    pass
                case HostedToolPart():
                    blocks.append(await self.hosted(part))
                case ImagePart():
                    raise UnsupportedContentError("continuation_unsupported", part.type)
                case _:
                    assert_never(part)
        self.add("assistant", blocks)

    async def result(self, line: ResultLine) -> None:
        content = [await self.part(p) for p in line.content if not isinstance(p, CitationPart)]
        if line.late is MISSING:
            block: Block = {
                "type": "tool_result",
                "tool_use_id": line.call_id,
                "is_error": line.is_error,
                "content": list[JsonValue](content),
            }
            self.add("user", [block])
            return
        head: Block = {"type": "text", "text": f"Late result of tool call {line.call_id}:"}
        self.add("user", [head, *content])

    async def reasoning(self, part: ReasoningPart) -> Block:
        if part.provider != PROVIDER or part.format not in REASONING_FORMATS:
            raise UnsupportedContentError("continuation_unsupported", f"{part.provider} reasoning")
        return _BLOCK.validate_json(await read(self.context, part.ref))

    async def hosted(self, part: HostedToolPart) -> Block:
        """A server tool block goes back exactly as recorded."""
        if part.provider != PROVIDER:
            raise UnsupportedContentError(
                "continuation_unsupported", f"{part.provider} hosted tool"
            )
        return _BLOCK.validate_json(await read(self.context, part.ref))

    async def part(self, part: TextPart | ImagePart | DocumentPart | AudioPart) -> Block:
        match part:
            case TextPart(text=text):
                return {"type": "text", "text": text}
            case ImagePart(ref=ref):
                data = base64.b64encode(await read(self.context, ref)).decode("ascii")
                source: Block = {"type": "base64", "media_type": ref.media_type, "data": data}
                return {"type": "image", "source": source}
            case DocumentPart():
                return await self.document(part)
            case AudioPart():
                raise UnsupportedContentError("content_unsupported", "audio_ref")
            case _:
                assert_never(part)

    async def document(self, part: DocumentPart) -> Block:
        raw = await read(self.context, part.ref)
        if part.ref.media_type == "application/pdf":
            data = base64.b64encode(raw).decode("ascii")
            source: Block = {"type": "base64", "media_type": "application/pdf", "data": data}
        else:
            source = {"type": "text", "media_type": "text/plain", "data": raw.decode("utf-8")}
        block: Block = {"type": "document", "source": source}
        if part.title is not MISSING:
            block["title"] = part.title
        if self.citations:
            block["citations"] = {"enabled": True}
        self.documents.append(part.ref.sha256)
        return block


def _tool(tool: ToolLine) -> JsonValue:
    schema = tool.parameters()
    return {"name": tool.name, "description": tool.description, "input_schema": schema}


def _ordered(role: str, blocks: list[Block]) -> JsonValue:
    if role == "user":
        blocks = sorted(blocks, key=lambda b: b.get("type") != "tool_result")
    return {"role": role, "content": list[JsonValue](blocks)}


async def build(request: Request, context: ModelContext) -> Body:
    head = request.head
    builder = _Builder(context, head.adapter.settings.get("citations") is True)
    for line in request.messages:
        match line:
            case UserLine():
                await builder.user(line)
            case AssistantLine():
                await builder.assistant(line)
            case ResultLine():
                await builder.result(line)
            case _:
                assert_never(line)
    body: dict[str, JsonValue] = {
        **head.params,
        "model": head.model.name,
        "messages": [_ordered(role, blocks) for role, blocks in builder.messages],
        "stream": True,
    }
    if head.system:
        body["system"] = head.system
    tools = [*(_tool(t) for t in request.tools), *pinned(head.adapter.settings)]
    if tools:
        body["tools"] = tools
    return Body(body, tuple(builder.documents))
