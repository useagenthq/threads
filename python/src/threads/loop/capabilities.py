"""The capability pre-check (spec/schema/README.md, "Capability pre-check"): a rendered request
the model can't take never becomes a `model_request`."""

from typing import Literal

from pydantic import JsonValue, TypeAdapter

from threads.loop.model import ModelInfo

type Unsupported = Literal["content_unsupported", "continuation_unsupported"]

_LINE: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_MEDIA = ("image_ref", "document_ref", "audio_ref")
_CONTINUATION = ("reasoning", "hosted_tool")


def mismatch(body: bytes, info: ModelInfo) -> Unsupported | None:
    """The first part of the request, in order, that the model doesn't declare it can take.
    The body is Render v1 the loop just rendered, so it parses."""
    for text in body.splitlines()[1:]:
        line = _LINE.validate_json(text)
        parts = line.get("content")
        for part in parts if isinstance(parts, list) else ():
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            media = line.get("role") in ("user", "tool") and kind in _MEDIA
            if media and kind not in info.accepts:
                return "content_unsupported"
            if kind in _CONTINUATION and part.get("provider") != info.model.provider:
                return "continuation_unsupported"
    return None
