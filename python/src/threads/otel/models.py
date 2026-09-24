"""Model call spans (chat) and where a retry_scheduled goes: the next attempt of its turn that
is not a compaction side request, else the turn (spec/otel/README.md, "Span events")."""

from threads.log import (
    Event,
    ModelAttemptAbandonedEvent,
    ModelRef,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    RetryScheduledEvent,
)
from threads.otel.attrs import chat_attrs, response_attrs, span_event
from threads.otel.ids import span_id
from threads.otel.span import CLIENT, Link, OpenSpan, Span
from threads.otel.turns import Turn

type Closing = ModelResponseEvent | ModelResponseRecoveredEvent | ModelAttemptAbandonedEvent


class Models:
    def __init__(self, content: bool) -> None:
        self.content = content
        self.model: ModelRef = ModelRef(provider="unknown", name="unknown")
        self._open: dict[str, OpenSpan] = {}
        self._ids: dict[str, str] = {}
        """Every request's span id, so a retry names the attempt that failed."""
        self._retry: RetryScheduledEvent | None = None

    def request(self, e: ModelRequestEvent, turn: Turn) -> None:
        sid = span_id(e.branch_id, e.event_id)
        self._ids[e.event_id] = sid
        span = OpenSpan(
            Link(turn.context.trace_id, sid),
            turn.span.id.span_id,
            f"chat {self.model.name}",
            CLIENT,
            e,
            chat_attrs(self.model, e),
        )
        retry = self._retry
        if retry is not None and e.data.purpose != "compaction":
            span.events.append(span_event(retry, self._ids.get(retry.data.request_event_id)))
            self._retry = None
        self._open[e.event_id] = span

    def close(self, e: Closing, branch_id: str, tenant: str) -> Span | None:
        """A response, recovered response or abandonment closes its request's span."""
        span = self._open.pop(e.data.request_event_id, None)
        if span is None:
            return None
        if isinstance(e, ModelAttemptAbandonedEvent):
            span.attrs["error.type"] = e.data.reason
            return span.close(e, branch_id, tenant, e.data.reason)
        span.attrs.update(response_attrs(e, self.content))
        return span.close(e, branch_id, tenant, None)

    def retry(self, e: RetryScheduledEvent) -> None:
        """Held for the next attempt; a turn that closes first takes it."""
        self._retry = e

    def end_all(self, e: Event, turn: Turn, branch_id: str, tenant: str) -> list[Span]:
        """A turn's close: model spans still open are cut, and a held retry goes to the turn."""
        retry = self._retry
        if retry is not None:
            turn.span.events.append(span_event(retry, self._ids.get(retry.data.request_event_id)))
        self._retry = None
        cut: list[Span] = []
        for span in self._open.values():
            span.attrs["threads.cut"] = True
            cut.append(span.close(e, branch_id, tenant, "cut"))
        self._open.clear()
        return cut
