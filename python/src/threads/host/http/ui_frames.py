"""GET /v1/threads/{thread_id}/runs/{run_id}/ui/{protocol}: a run's committed frames for custom
clients, resumed by cursor (spec/schema/ui/README.md, "Cursors"). No live text, so it is exact.
AI SDK resumes frame by frame; AG-UI at the event boundary, as a new AG-UI run that opens with a
snapshot through the cursor's event."""

import re
from collections.abc import Sequence
from typing import Final

from starlette.requests import Request
from starlette.responses import Response

from threads.agents.store import now_ms, open_store
from threads.host.app import Host
from threads.host.http.common import Handler, authenticated, error
from threads.host.http.ui_common import streamed
from threads.host.outcome import run_start
from threads.host.ui.closing import RunIds
from threads.host.ui.connection import Cursor, event_frames
from threads.host.ui.facts import RunFacts
from threads.host.ui.frame import Protocol, is_protocol
from threads.host.ui.listener import LiveListener
from threads.host.ui.session import SessionPlan, own_events
from threads.log import Event, EventId, Principal, ThreadId
from threads.result import Ok
from threads.thread.handle import Thread

_CURSOR: Final = re.compile(r"^(\d+):(\d+)$")


def frames(host: Host) -> Handler:
    async def handle(request: Request, principal: Principal) -> Response:
        protocol = request.path_params["protocol"]
        if not is_protocol(protocol):
            return error("not_found", f"no UI protocol {protocol}")
        thread_id = ThreadId(request.path_params["thread_id"])
        run_id = EventId(request.path_params["run_id"])
        store = host.runner.store(principal.tenant)
        sq = await open_store(store)
        found = None
        for row in await sq.tables.branches(thread_id):
            read = await sq.read(row.branch_id, now_ms())
            if isinstance(read, Ok) and run_start(read.value.fold.events, run_id) is not None:
                found = (row.branch_id, read.value.fold.events)
                break
        if found is None:
            return error("not_found", f"no run {run_id} on thread {thread_id}")
        branch, events = found
        raw = request.headers.get("last-event-id") or request.query_params.get("after")
        after = None if raw is None else _cursor(events, protocol, run_id, raw)
        if raw is not None and after is None:
            return error("invalid_cursor", f"{raw} is not a frame of run {run_id}")
        plan = SessionPlan(protocol, run_id, RunIds(thread_id, run_id))
        if after is not None and protocol == "ai-sdk":
            plan = SessionPlan(protocol, run_id, plan.ids, after=after)
        elif after is not None:
            receipts = await sq.tables.ui_messages(thread_id)
            plan = SessionPlan(protocol, run_id, plan.ids, replay=int(after.seq), receipts=receipts)
        listener = LiveListener(None, thread_id)
        return streamed(host, Thread(thread_id, branch, store), plan, listener)

    return authenticated(host, handle)


def _cursor(
    events: Sequence[Event], protocol: Protocol, run_id: EventId, raw: str
) -> Cursor | None:
    """A cursor names a frame of the run: event `seq` of its slice, `k` below its frame count."""
    m = _CURSOR.match(raw)
    if m is None:
        return None
    seq, k = int(m[1]), int(m[2])
    facts = RunFacts()
    for e in own_events(events, run_id):
        count = len(event_frames(protocol, e, facts))
        if e.seq == seq:
            return Cursor(seq, k) if k < count else None
    return None
