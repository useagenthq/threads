"""Render v1 to a Responses API request body: a pure function of the lines and artifact bytes.

- Line 0 `params` are Responses API fields, sent as pinned; `system` is `instructions`. The
  request is stateless (`store: false`) and asks for encrypted reasoning, so a recorded
  reasoning item carries the whole continuation and goes back byte for byte.
- Each assistant part is its own input item in recorded order. A tool call's arguments are its
  recorded input as RFC 8785 JSON.
- A tool result is a `function_call_output`: one text part as a string, otherwise a list of
  parts. The Responses API has no error flag, so an error result is prefixed `Error: `.
- Citation parts annotate the model's own text and are not sent back. Anything this API can't
  carry raises `UnsupportedContentError` before dispatch.
"""

import base64
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
    canonical,
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

PROVIDER = "openai"
REASONING_FORMAT = "openai_reasoning"
_ITEM: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

type Item = dict[str, JsonValue]


@dataclass
class _Builder:
    context: ModelContext
    items: list[JsonValue] = field(default_factory=list[JsonValue])

    async def user(self, line: UserLine) -> None:
        content = [await self.part(p) for p in line.content]
        self.items.append({"role": "user", "content": list[JsonValue](content)})

    async def assistant(self, line: AssistantLine) -> None:
        for part in line.content:
            match part:
                case TextPart(text=text):
                    said: Item = {"type": "output_text", "text": text, "annotations": []}
                    self.items.append({"role": "assistant", "content": [said]})
                case ToolUsePart(call_id=call_id, name=name, input=args):
                    call: Item = {"type": "function_call", "call_id": call_id, "name": name}
                    call["arguments"] = canonical(dict(args))
                    self.items.append(call)
                case ReasoningPart():
                    self.items.append(await self.reasoning(part))
                case CitationPart():
                    pass
                case HostedToolPart():
                    self.items.append(await self.hosted(part))
                case ImagePart():
                    raise UnsupportedContentError("continuation_unsupported", part.type)
                case _:
                    assert_never(part)

    async def result(self, line: ResultLine) -> None:
        parts = [await self.part(p) for p in line.content if not isinstance(p, CitationPart)]
        if line.is_error:
            parts.insert(0, {"type": "input_text", "text": "Error: "})
        if line.late is not MISSING:
            head: Item = {"type": "input_text", "text": f"Late result of tool call {line.call_id}:"}
            self.items.append({"role": "user", "content": [head, *parts]})
            return
        output: JsonValue = list[JsonValue](parts)
        if all(p.get("type") == "input_text" for p in parts):
            output = "".join(str(p.get("text")) for p in parts)
        self.items.append(
            {"type": "function_call_output", "call_id": line.call_id, "output": output}
        )

    async def reasoning(self, part: ReasoningPart) -> Item:
        if part.provider != PROVIDER or part.format != REASONING_FORMAT:
            raise UnsupportedContentError("continuation_unsupported", f"{part.provider} reasoning")
        return _ITEM.validate_json(await read(self.context, part.ref))

    async def hosted(self, part: HostedToolPart) -> Item:
        """A hosted tool item goes back exactly as recorded."""
        if part.provider != PROVIDER:
            raise UnsupportedContentError(
                "continuation_unsupported", f"{part.provider} hosted tool"
            )
        return _ITEM.validate_json(await read(self.context, part.ref))

    async def part(self, part: TextPart | ImagePart | DocumentPart | AudioPart) -> Item:
        match part:
            case TextPart(text=text):
                return {"type": "input_text", "text": text}
            case ImagePart(ref=ref):
                return {
                    "type": "input_image",
                    "image_url": await self.data_url(part),
                }
            case DocumentPart(ref=ref):
                file: Item = {
                    "type": "input_file",
                    "file_data": await self.data_url(part),
                }
                file["filename"] = part.title if part.title is not MISSING else ref.sha256
                return file
            case AudioPart():
                raise UnsupportedContentError("content_unsupported", "audio_ref")
            case _:
                assert_never(part)

    async def data_url(self, part: ImagePart | DocumentPart) -> str:
        data = base64.b64encode(await read(self.context, part.ref)).decode("ascii")
        return f"data:{part.ref.media_type};base64,{data}"


def _tool(tool: ToolLine) -> JsonValue:
    schema = tool.parameters()
    spec: Item = {"type": "function", "name": tool.name, "description": tool.description}
    spec["parameters"] = schema
    spec["strict"] = False
    return spec


async def build(request: Request, context: ModelContext) -> dict[str, JsonValue]:
    head = request.head
    builder = _Builder(context)
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
    # Line 0 pins the cap under the provider-neutral max_tokens; the Responses API names it
    # max_output_tokens.
    params = {k: v for k, v in head.params.items() if k != "max_tokens"}
    if "max_tokens" in head.params:
        params["max_output_tokens"] = head.params["max_tokens"]
    body: dict[str, JsonValue] = {
        "store": False,
        "include": ["reasoning.encrypted_content"],
        **params,
        "model": head.model.name,
        "input": builder.items,
        "stream": True,
    }
    if head.system:
        body["instructions"] = head.system
    tools = [*(_tool(t) for t in request.tools), *pinned(head.adapter.settings)]
    if tools:
        body["tools"] = tools
    return body
