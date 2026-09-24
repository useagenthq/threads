"""One frame of a UI stream (spec/schema/ui/README.md): a protocol chunk (an AI SDK UI message
chunk or an AG-UI event) and, for a frame derived from a committed event, its SSE id
`<seq>:<k>`. Envelope and live frames carry no id."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeGuard

from pydantic import JsonValue

type Protocol = Literal["ai-sdk", "ag-ui"]
type Chunk = Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Frame:
    data: Chunk
    id: str | None = None


def is_protocol(value: str) -> TypeGuard[Protocol]:
    return value in ("ai-sdk", "ag-ui")


def bare(chunks: Iterable[Chunk]) -> list[Frame]:
    """The chunks as frames with no SSE id."""
    return [Frame(c) for c in chunks]
