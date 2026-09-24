"""What the UI routes share on the wire: a UI stream as an SSE response, and a failure answered
with its route's code set (spec/schema/ui/README.md, "Routes")."""

from starlette.responses import JSONResponse, StreamingResponse

from threads.host.app import Host
from threads.host.http.common import error
from threads.host.ui.common import UI_CODES
from threads.host.ui.listener import LiveListener
from threads.host.ui.session import SessionPlan
from threads.host.ui.sse import HEADERS, sse
from threads.host.ui.stream import ui_frames
from threads.log import ParseError
from threads.thread.handle import Thread


def streamed(
    host: Host, thread: Thread, plan: SessionPlan, listener: LiveListener
) -> StreamingResponse:
    frames = ui_frames(host.runner, thread, plan, listener)
    return StreamingResponse(
        sse(plan.protocol, frames),
        media_type="text/event-stream",
        headers=dict(HEADERS[plan.protocol]),
    )


def refused(failure: ParseError, listener: LiveListener | None = None) -> JSONResponse:
    """A failure before any stream starts. A code outside the UI routes' set is a bug in the
    host, never a new wire code."""
    if listener is not None:
        listener.stop()
    if failure.code not in UI_CODES:
        raise AssertionError(f"{failure.code} is not a failure of the UI routes")
    return error(failure.code, failure.message)
