"""A span while the walk builds it, and the closed span it becomes (spec/otel/README.md)."""

from dataclasses import dataclass, field
from typing import Final

from threads.log import Event

type AttrValue = str | int | bool | tuple[str, ...]
type Attrs = dict[str, AttrValue | None]

INTERNAL: Final = 1
CLIENT: Final = 3


@dataclass(frozen=True, slots=True)
class SpanEvent:
    time: int
    name: str
    attributes: Attrs


@dataclass(frozen=True, slots=True)
class Link:
    trace_id: str
    span_id: str


@dataclass(frozen=True, slots=True)
class Span:
    """A closed span: what `spans()` returns and the encoder writes. Times are ms."""

    trace_id: str
    span_id: str
    branch_id: str
    close_seq: int
    parent_span_id: str | None
    name: str
    kind: int
    start: int
    end: int
    attributes: Attrs
    events: tuple[SpanEvent, ...]
    links: tuple[Link, ...]
    status: str | None


@dataclass(frozen=True, slots=True)
class Context:
    """Where a span sits: its trace, the span it is a child of, and whether its parent thread was
    missing (a child's first turn)."""

    trace_id: str
    parent_span_id: str | None
    parent_missing: bool = False
    run_id: str | None = None
    """The user_input that opened the run, when a user_input opened it."""


@dataclass(slots=True)
class OpenSpan:
    """A span the walk has opened and not yet closed."""

    id: Link
    parent_span_id: str | None
    name: str
    kind: int
    opener: Event
    attrs: Attrs
    links: tuple[Link, ...] = ()
    events: list[SpanEvent] = field(default_factory=list[SpanEvent])

    def close(self, close: Event, branch_id: str, tenant: str, status: str | None) -> Span:
        """The span as `close` ends it, for the branch `branch_id` exports."""
        skew = close.time < self.opener.time
        attributes: Attrs = {
            **self.attrs,
            "threads.tenant": tenant,
            "threads.thread_id": self.opener.thread_id,
            "threads.branch_id": branch_id,
            "threads.seq.start": self.opener.seq,
            "threads.seq.end": close.seq,
            "threads.clock_skew": skew or None,
        }
        return Span(
            self.id.trace_id,
            self.id.span_id,
            branch_id,
            close.seq,
            self.parent_span_id,
            self.name,
            self.kind,
            self.opener.time,
            self.opener.time if skew else close.time,
            attributes,
            tuple(self.events),
            self.links,
            status,
        )
