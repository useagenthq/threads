"""The parts of a model response a UI shows (spec/schema/ui/README.md, "Mapping"): text with
text, reasoning with a summary, and calls, each with its index in the response's content."""

from dataclasses import dataclass
from typing import Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    JsonObject,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ReasoningPart,
    TextPart,
    ToolUsePart,
)


@dataclass(frozen=True, slots=True)
class Written:
    """A text or reasoning part: its text, or its reasoning summary."""

    kind: Literal["text", "reasoning"]
    index: int
    text: str


@dataclass(frozen=True, slots=True)
class Called:
    index: int
    call_id: str
    name: str
    input: JsonObject


type Shown = Written | Called


def part_id(request_id: str, index: int) -> str:
    """A text or reasoning part's id: the model_request's event id and the part's index."""
    return f"{request_id}:{index}"


def shown_parts(e: ModelResponseEvent | ModelResponseRecoveredEvent) -> list[Shown]:
    out: list[Shown] = []
    for index, part in enumerate(e.data.content):
        match part:
            case TextPart(text=text) if text:
                out.append(Written("text", index, text))
            case ReasoningPart(summary=summary) if summary is not MISSING and summary:
                out.append(Written("reasoning", index, summary))
            case ToolUsePart(call_id=call_id, name=name, input=input):
                out.append(Called(index, call_id, name, input))
            case _:
                pass
    return out
