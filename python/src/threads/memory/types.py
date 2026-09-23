"""Memory and knowledge values (spec/api.json `Scope` ... `Doc`, 6 and 8).

What a provider returns crosses a trust boundary, so these are strict, frozen models: a provider
adapter parses its SDK's response into them, and the framework parses whatever a provider
hands back (`parse_hits`) before it looks at a single field.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from threads._strict_model import StrictModel
from threads.log import ArtifactRef, EventId, Span, ThreadId
from threads.result import Err, Ok

type Origin = Literal["user", "tool_output", "model", "host"]
type ProviderErrorCode = Literal[
    "unavailable", "timeout", "invalid", "scope_violation", "not_found"
]


class Scope(StrictModel):
    """From host config and the verified principal; never a tool argument."""

    tenant_id: str
    agent: str
    scope: str


class Binding(StrictModel):
    """Issued by the host; the provider stores it opaquely."""

    namespace: str
    record_id: str


class Provenance(StrictModel):
    thread_id: ThreadId
    event_ids: tuple[EventId, ...]


class MemoryRecord(StrictModel):
    text: str
    origin: Origin
    provenance: Provenance
    binding: Binding


class RecordRef(StrictModel):
    id: str
    version: str


class MemoryHit(StrictModel):
    id: str
    version: str
    text: str
    score: float
    origin: Origin
    binding: Binding


class KnowledgeSource(StrictModel):
    source_id: str
    media_type: str
    content: bytes
    location: str | None = None
    binding: Binding


class DocVersion(StrictModel):
    doc_id: str
    version: str
    content_sha256: str
    revision: int
    """The store's monotonic revision after this ingest."""


class KnowledgeHit(StrictModel):
    doc_id: str
    version: str
    span: Span
    text: str
    score: float
    binding: Binding


class Doc(StrictModel):
    doc_id: str
    version: str
    media_type: str
    content_ref: ArtifactRef
    binding: Binding


@dataclass(frozen=True, slots=True)
class ProviderError:
    """A provider failure is a value; it never crashes a run."""

    code: ProviderErrorCode
    message: str
    sent: bool = True
    """False only when the fence refused at the transport: nothing was written."""


type Outcome[T] = Ok[T] | Err[ProviderError]

_MEMORY_HITS: TypeAdapter[tuple[MemoryHit, ...]] = TypeAdapter(tuple[MemoryHit, ...])
_KNOWLEDGE_HITS: TypeAdapter[tuple[KnowledgeHit, ...]] = TypeAdapter(tuple[KnowledgeHit, ...])


def memory_hits(value: object) -> Outcome[tuple[MemoryHit, ...]]:
    """A provider's recall, parsed: third-party code is a boundary even in process."""
    return _parse(_MEMORY_HITS, value)


def knowledge_hits(value: object) -> Outcome[tuple[KnowledgeHit, ...]]:
    return _parse(_KNOWLEDGE_HITS, value)


def _parse[T](adapter: TypeAdapter[T], value: object) -> Outcome[T]:
    try:
        return Ok(adapter.validate_python(value))
    except ValidationError as error:
        return Err(ProviderError("invalid", f"malformed answer: {error.error_count()} error(s)"))
