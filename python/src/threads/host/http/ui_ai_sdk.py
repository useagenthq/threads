"""The AI SDK routes (spec/schema/ui/README.md, "Routes"): POST /v1/ui/ai-sdk/{agent}, which
useChat sends a new message or its approvals and answers to, and the reconnect route
useChat({resume: true}) calls."""

from collections.abc import Sequence

from pydantic.experimental.missing_sentinel import MISSING
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from threads._generated.host_api_v1 import AiSdkChatRequest, AiSdkMessage
from threads.host.app import Host
from threads.host.http.common import Handler, authenticated, body, error
from threads.host.http.ui_common import refused, streamed
from threads.host.ui.ai_sdk_decisions import record_parts
from threads.host.ui.closing import RunIds
from threads.host.ui.common import ui_log
from threads.host.ui.connection import INF, Cursor
from threads.host.ui.key import CHAT_KEY, ui_thread_id
from threads.host.ui.listener import LiveListener
from threads.host.ui.session import SessionPlan
from threads.host.ui.start import UserMessage, start_ui_run
from threads.log import Event, EventId, Principal, ThreadId, UserInputEvent
from threads.reduce.run_end import run_end
from threads.result import Err
from threads.thread.handle import Thread

_ID = "a message id is 1 to 128 of A-Z a-z 0-9 _ . -"


def chat(host: Host) -> Handler:
    async def handle(request: Request, principal: Principal) -> Response:
        agent = request.path_params["agent"]
        if host.runner.agent(agent) is None:
            return error("not_found", f"no agent {agent}")
        parsed = await body(request, AiSdkChatRequest)
        if isinstance(parsed, JSONResponse):
            return parsed
        if parsed.trigger == "regenerate-message":
            why = "regenerate isn't supported; fork the thread to try again from an earlier point"
            return error("invalid_request", why)
        thread_id = ui_thread_id(principal, agent, parsed.id)
        last = parsed.messages[-1]
        if last.role == "user":
            return await _submitted(host, principal, agent, thread_id, last)
        if last.role == "assistant":
            return await _decided(host, principal, thread_id, last)
        return error("invalid_request", "the last message must be the user's or the assistant's")

    return authenticated(host, handle)


async def _submitted(
    host: Host, principal: Principal, agent: str, thread_id: ThreadId, message: AiSdkMessage
) -> Response:
    if any(p.type != "text" for p in message.parts):
        return error("invalid_request", "only text parts are supported in a user message")
    if not CHAT_KEY.match(message.id):
        return error("invalid_request", _ID)
    text = "".join("" if p.text is MISSING else p.text for p in message.parts)
    if not text:
        return error("invalid_request", "the message has no text")
    listener = LiveListener(host.runner.hub, thread_id)
    started = await start_ui_run(
        host.runner, agent, principal, thread_id, UserMessage(message.id, text)
    )
    if isinstance(started, Err):
        return refused(started.error, listener)
    run = started.value
    thread = Thread(thread_id, run.branch_id, host.runner.store(principal.tenant))
    plan = SessionPlan("ai-sdk", run.run_id, RunIds(thread_id, run.run_id))
    return streamed(host, thread, plan, listener)


async def _decided(
    host: Host, principal: Principal, thread_id: ThreadId, message: AiSdkMessage
) -> Response:
    """Records the message's decisions and answers, then streams the run after the last."""
    listener = LiveListener(host.runner.hub, thread_id)
    log = await ui_log(host, principal, thread_id)
    if log is None:
        listener.stop()
        return error("not_found", "this chat has no thread yet")
    recorded = await record_parts(host, principal, log, message.parts)
    if isinstance(recorded, Err):
        return refused(recorded.error, listener)
    after = await ui_log(host, principal, thread_id)
    events = () if after is None else after.events
    last = next((e for e in reversed(events) if e.event_id in recorded.value), None)
    run = None if last is None else _run_of(events, last.seq)
    if last is None or run is None:
        listener.stop()
        return Response(status_code=204)
    plan = SessionPlan("ai-sdk", run, RunIds(thread_id, run), after=Cursor(last.seq, INF))
    return streamed(host, log.thread, plan, listener)


def _run_of(events: Sequence[Event], seq: int) -> EventId | None:
    """The run whose slice holds the event at `seq`: the latest user_input at or before it."""
    runs = [e.event_id for e in events if isinstance(e, UserInputEvent) and e.seq <= seq]
    return runs[-1] if runs else None


def reconnect(host: Host) -> Handler:
    """GET {api}/{chat_id}/stream: the key's latest run from its start while it goes on, else
    204."""

    async def handle(request: Request, principal: Principal) -> Response:
        agent = request.path_params["agent"]
        if host.runner.agent(agent) is None:
            return error("not_found", f"no agent {agent}")
        key = request.path_params["chat_id"]
        if not CHAT_KEY.match(key):
            return error("invalid_request", "a chat id is 1 to 128 of A-Z a-z 0-9 _ . -")
        thread_id = ui_thread_id(principal, agent, key)
        listener = LiveListener(host.runner.hub, thread_id)
        log = await ui_log(host, principal, thread_id)
        runs = [] if log is None else [e for e in log.events if isinstance(e, UserInputEvent)]
        if log is None or not runs or run_end(log.events, runs[-1].event_id).status != "running":
            listener.stop()
            return Response(status_code=204)
        run = runs[-1].event_id
        plan = SessionPlan("ai-sdk", run, RunIds(thread_id, run))
        return streamed(host, log.thread, plan, listener)

    return authenticated(host, handle)
