"""One operation, once the request is parsed and the caller is known. The request message is parsed
here from the generated schema rather than passed around loosely typed: inside these functions the
fields are the operation's own, not JsonValue."""

from collections.abc import Callable
from typing import assert_never

from pydantic import JsonValue
from starlette.responses import Response

from threadsai._generated.a2a_v1 import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    SendMessageRequest,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
)
from threadsai.a2a.protocol import A2aFault, Method, fault, is_terminal
from threadsai.host.a2a.config import ExposedAgent
from threadsai.host.a2a.keys import raw_message
from threadsai.host.a2a.parse import Inbound
from threadsai.host.a2a.read import slice_of
from threadsai.host.a2a.send import send_message
from threadsai.host.a2a.state import task_of
from threadsai.host.a2a.stream import follow, one, parse_cursor
from threadsai.host.a2a.tasks import cancel_task, get_task, list_tasks, locate
from threadsai.host.a2a.wire import Envelope, answer, item_of, refuse, streamed
from threadsai.host.runs import Runner
from threadsai.log import Principal
from threadsai.reduce.handlers import to_json


async def run_operation(  # noqa: PLR0913, PLR0917 - one operation and all it was asked with
    runner: Runner,
    principal: Principal,
    inbound: Inbound,
    envelope: Envelope,
    agent: ExposedAgent,
    method: Method,
    params: JsonValue,
) -> Response:
    match method:
        case "SendMessage" | "SendStreamingMessage":
            return await _send(runner, principal, inbound, envelope, agent, method, params)
        case "GetTask":
            got = await get_task(runner, principal, GetTaskRequest.model_validate(params).id)
            return _answered(envelope, got)
        case "ListTasks":
            listed = await list_tasks(
                runner, principal, inbound.name, ListTasksRequest.model_validate(params)
            )
            if isinstance(listed, A2aFault):
                return refuse(envelope, listed)
            return answer(envelope, to_json(listed))
        case "CancelTask":
            done = await cancel_task(runner, principal, CancelTaskRequest.model_validate(params).id)
            return _answered(envelope, done)
        case "SubscribeToTask":
            asked = SubscribeToTaskRequest.model_validate(params)
            return await _subscribe(runner, principal, inbound, envelope, asked.id)
        case _:
            assert_never(method)


def _answered(envelope: Envelope, result: Task | A2aFault) -> Response:
    if isinstance(result, A2aFault):
        return refuse(envelope, result)
    return answer(envelope, to_json(result))


async def _send(  # noqa: PLR0913, PLR0917 - one operation and all it was asked with
    runner: Runner,
    principal: Principal,
    inbound: Inbound,
    envelope: Envelope,
    agent: ExposedAgent,
    method: Method,
    params: JsonValue,
) -> Response:
    sent = await send_message(
        runner,
        principal,
        inbound.name,
        agent,
        SendMessageRequest.model_validate(params),
        raw_message(params),
    )
    if isinstance(sent, A2aFault):
        return refuse(envelope, sent)
    if method == "SendMessage":
        return answer(envelope, {"task": to_json(sent.task)})
    item = _item(envelope)
    # A task refused before it existed has no log to follow: its one frame is the whole stream.
    if sent.at is None:
        return streamed(one(item(StreamResponse(task=sent.task))))
    return streamed(follow(runner, principal, sent.at, None, item))


async def _subscribe(
    runner: Runner, principal: Principal, inbound: Inbound, envelope: Envelope, task_id: str
) -> Response:
    """A subscribe on a terminal task is UnsupportedOperationError, as the spec requires: a client
    that finds its task already finished reads the result with GetTask. A resume starts after the
    frame Last-Event-ID names, so it sees exactly the frames it has not seen."""
    at = await locate(runner, principal, task_id)
    if isinstance(at, A2aFault):
        return refuse(envelope, at)
    view = await slice_of(runner, principal.tenant, at)
    if view is None:
        return refuse(envelope, fault("TaskNotFoundError", f"no task {task_id}"))
    if is_terminal(task_of(view).status.state):
        why = f"task {task_id} has ended; read it with GetTask"
        return refuse(envelope, fault("UnsupportedOperationError", why))
    raw = inbound.request.headers.get("last-event-id") or inbound.request.query_params.get(
        "lastEventId"
    )
    cursor = parse_cursor(raw)
    if isinstance(cursor, A2aFault):
        return refuse(envelope, cursor)
    return streamed(follow(runner, principal, at, cursor, _item(envelope)))


def _item(envelope: Envelope) -> Callable[[StreamResponse], str]:
    """One stream item as its `data:` line, in the caller's own binding."""

    def written(item: StreamResponse) -> str:
        return item_of(envelope, to_json(item))

    return written
