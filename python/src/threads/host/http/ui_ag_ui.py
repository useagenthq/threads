"""POST /v1/ui/ag-ui/{agent} (spec/schema/ui/README.md, "Bodies"): an AG-UI 1.0 run input. Its
last user message starts a run, once; a retry (a new runId, the same message id) replays that
run with a snapshot. Its resume entries answer the interrupts of a parked run, which then replays
the same way."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from threads._generated.host_api_v1 import (
    AgUiMessage,
    AgUiResumeEntry,
    AgUiRunInput,
    RunAccepted,
)
from threads.agents.store import now_ms, open_store
from threads.host.app import Host
from threads.host.http.common import Handler, authenticated, body, error
from threads.host.http.ui_common import refused, streamed
from threads.host.ui.ag_ui_resume import apply_resume
from threads.host.ui.closing import RunIds
from threads.host.ui.common import ui_log
from threads.host.ui.frame import Chunk
from threads.host.ui.key import CHAT_KEY, ui_thread_id
from threads.host.ui.listener import LiveListener
from threads.host.ui.session import SessionPlan
from threads.host.ui.start import UserMessage, find_ui_run, start_ui_run
from threads.log import ParseError, Principal, ThreadId, UserInputEvent
from threads.result import Err, Ok
from threads.thread.handle import Thread


@dataclass(frozen=True, slots=True)
class _Stream:
    run: RunAccepted
    replay: bool
    extra: Sequence[Chunk]


type _Answer = _Stream | ParseError


def run(host: Host) -> Handler:
    async def handle(request: Request, principal: Principal) -> Response:
        agent = request.path_params["agent"]
        if host.runner.agent(agent) is None:
            return error("not_found", f"no agent {agent}")
        parsed = await body(request, AgUiRunInput)
        if isinstance(parsed, JSONResponse):
            return parsed
        if parsed.tools is not MISSING and parsed.tools:
            why = "frontend tools aren't supported; define tools on the agent"
            return error("invalid_request", why)
        last = next((m for m in reversed(parsed.messages) if m.role == "user"), None)
        message = None if last is None else _user_message(last)
        if isinstance(message, str):
            return error("invalid_request", message)
        thread_id = ui_thread_id(principal, agent, parsed.threadId)
        listener = LiveListener(host.runner.hub, thread_id)
        resume = () if parsed.resume is MISSING else parsed.resume
        answered = await _respond(host, principal, agent, thread_id, message, resume=resume)
        if isinstance(answered, ParseError):
            return refused(answered, listener)
        store = host.runner.store(principal.tenant)
        receipts = await (await open_store(store)).tables.ui_messages(thread_id)
        plan = SessionPlan(
            "ag-ui",
            answered.run.run_id,
            RunIds(parsed.threadId, parsed.runId),
            replay="head" if answered.replay else None,
            extra=answered.extra,
            receipts=receipts,
        )
        thread = Thread(thread_id, answered.run.branch_id, store)
        return streamed(host, thread, plan, listener)

    return authenticated(host, handle)


async def _respond(  # noqa: PLR0913 - the request and its route
    host: Host,
    principal: Principal,
    agent: str,
    thread_id: ThreadId,
    message: UserMessage | None,
    *,
    resume: Sequence[AgUiResumeEntry],
) -> _Answer:
    found = (
        None
        if message is None
        else await find_ui_run(host.runner, agent, principal, thread_id, message)
    )
    if isinstance(found, Err):
        return found.error
    extra: Sequence[Chunk] = ()
    if resume:
        applied = await _resumed(
            host, principal, thread_id, resume, message is not None and found is None
        )
        if isinstance(applied, ParseError):
            return applied
        extra = applied
        if message is None or found is not None:
            return await _replay(host, principal, thread_id, found, extra)
    if found is not None:
        return _Stream(found.value, True, extra)
    if message is None:
        return ParseError("invalid_request", "the input has no user message")
    started = await start_ui_run(host.runner, agent, principal, thread_id, message)
    return started.error if isinstance(started, Err) else _Stream(started.value, False, extra)


async def _resumed(
    host: Host,
    principal: Principal,
    thread_id: ThreadId,
    resume: Sequence[AgUiResumeEntry],
    new_message: bool,
) -> Sequence[Chunk] | ParseError:
    log = await ui_log(host, principal, thread_id)
    if log is None:
        return ParseError("not_found", "this chat has no thread yet")
    applied = await apply_resume(
        host, principal, log, resume, now=now_ms(), new_message=new_message
    )
    return applied.error if isinstance(applied, Err) else applied.value


async def _replay(
    host: Host,
    principal: Principal,
    thread_id: ThreadId,
    named: Ok[RunAccepted] | None,
    extra: Sequence[Chunk],
) -> _Answer:
    """After a resume: the run its message names, else the thread's latest, as a replay."""
    if named is not None:
        return _Stream(named.value, True, extra)
    log = await ui_log(host, principal, thread_id)
    runs = [] if log is None else [e for e in log.events if isinstance(e, UserInputEvent)]
    if log is None or not runs:
        return ParseError("not_found", "this chat has no run to resume")
    run = RunAccepted(thread_id=thread_id, branch_id=log.thread.branch, run_id=runs[-1].event_id)
    return _Stream(run, True, extra)


def _user_message(m: AgUiMessage) -> UserMessage | str:
    """The message's id and text, or why it can't start a run."""
    if not CHAT_KEY.match(m.id):
        return "a message id is 1 to 128 of A-Z a-z 0-9 _ . -"
    content = "" if m.content is MISSING else m.content
    if isinstance(content, str):
        return UserMessage(m.id, content) if content else "the message has no text"
    if any(p.type != "text" for p in content):
        return "only text parts are supported in a user message"
    text = "".join("" if p.text is MISSING else p.text for p in content)
    return UserMessage(m.id, text) if text else "the message has no text"
