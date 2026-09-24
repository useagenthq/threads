# pyright: strict
"""Short forms of the expected spans a case names: a turn in its opener's trace, a chat or tool
span under a turn, and a continuation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import obj, text
from .otel_expect import EVENT_FIELDS, Ctx, Shape, Span, event, root_of, span_id
from .otel_kinds import call_span, chat, turn

if TYPE_CHECKING:
    from .jcs import Obj
    from .log import Log


def sid(e: Obj, call_id: str | None = None) -> str:
    """The span id of the span `e` opens, hashed with the branch of its own segment."""
    return span_id(text(e["branch_id"]), text(e["event_id"]), call_id)


def turn_of(  # noqa: PLR0913 - a turn's parts
    ctx: Ctx,
    opener: Obj,
    close: Obj,
    events: tuple[Obj, ...] = (),
    *,
    trace: str | None = None,
    parent: str | None = None,
    links: tuple[Span, ...] = (),
    run_id: str | None = None,
    parent_missing: bool = False,
) -> Span:
    """A turn span. Default trace: the opener's own root; default run: the opener, when it is a
    user_input."""
    run = run_id if run_id is not None or opener["type"] != "user_input" else opener["event_id"]
    shape = Shape(sid(opener), trace or root_of(opener), parent, events, links)
    return turn(ctx, shape, (opener, close), None if run is None else text(run), parent_missing)


def chat_of(ctx: Ctx, t: Span, req: Obj, close: Obj, events: tuple[Obj, ...] = ()) -> Span:
    return chat(ctx, Shape(sid(req), t.trace, t.id, events), (req, close))


def call_events(log: Log, call_id: str, after: int, before: int) -> tuple[Obj, ...]:
    """The span events of a call: its events with after < seq < before."""
    return tuple(
        event(e)
        for e in log.events[after : before - 1]
        if obj(e["data"]).get("call_id") == call_id and e["type"] in EVENT_FIELDS
    )


def tool_of(  # noqa: PLR0913, PLR0917 - a tool span's parts
    ctx: Ctx, t: Span, log: Log, call_seq: int, close_seq: int, eclass: str | None
) -> Span:
    """The tool span of the tool_call at `call_seq`, closed at `close_seq`, carrying every event
    of its call in between."""
    call = log.events[call_seq - 1]
    cid = text(obj(call["data"])["call_id"])
    shape = Shape(sid(call), t.trace, t.id, call_events(log, cid, call_seq, close_seq))
    return call_span(ctx, shape, call, (call, log.events[close_seq - 1]), eclass)


def continuation_of(  # noqa: PLR0913, PLR0917 - a continuation's parts
    ctx: Ctx,
    t: Span,
    log: Log,
    call_seq: int,
    resumed_seq: int,
    close_seq: int,
    previous: Span,
    eclass: str | None,
) -> Span:
    """The continuation a `resumed` opens for a call, linked to the call's previous span."""
    call, resumed = log.events[call_seq - 1], log.events[resumed_seq - 1]
    cid = text(obj(call["data"])["call_id"])
    shape = Shape(
        sid(resumed, cid),
        t.trace,
        t.id,
        call_events(log, cid, resumed_seq, close_seq),
        (previous,),
    )
    return call_span(ctx, shape, call, (resumed, log.events[close_seq - 1]), eclass, resumed=True)
