"""SendMessage and SendStreamingMessage. The receipt lookup is the first thing that happens,
before any contextId is minted, so a retry that carries no contextId still answers the original
task and the original context. The receipt row and the run's user_input commit in one transaction,
which is what makes a duplicate delivery — even to two hosts on one store — one run."""

from collections.abc import Mapping

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import (
    Message,
    SendMessageRequest,
    Task,
    TaskStatus,
    TextPart,
)
from threads.a2a.protocol import A2aFault, fault, is_file_part, text_of
from threads.agents.store import now_ms, open_store
from threads.host.a2a.config import ExposedAgent
from threads.host.a2a.continuation import continue_task
from threads.host.a2a.keys import (
    MAX_HOPS,
    a2a_thread_id,
    body_hash,
    claimed_hops,
    claims,
    message_id_too_long,
    rejected_task_id,
    send_key,
)
from threads.host.a2a.read import Accepted, Located, located, open_run
from threads.host.a2a.target import context_branch
from threads.host.runs import Runner
from threads.host.start import Launched, recorded_run
from threads.log import Principal
from threads.log.keys import principal_key
from threads.result import Err
from threads.store import receipts
from threads.store.lines import uuid7


async def send_message(  # noqa: PLR0913, PLR0917 - one message and everything it arrived with
    runner: Runner,
    principal: Principal,
    name: str,
    agent: ExposedAgent,
    request: SendMessageRequest,
    raw: JsonValue,
) -> Accepted | A2aFault:
    """`raw` is the caller's own `message` JSON, which is what the body hash is taken over."""
    message = request.message
    if request.configuration is not MISSING and (
        request.configuration.taskPushNotificationConfig is not MISSING
    ):
        return fault(
            "PushNotificationNotSupportedError",
            "this agent does not support push notifications; "
            "follow the task with SubscribeToTask or GetTask",
        )
    bad = _unusable(message)
    if bad is not None:
        return bad
    digest = body_hash(name, raw)
    if digest is None:
        return fault("InvalidParamsError", "the message is not representable JSON")
    if message.taskId is not MISSING:
        return await continue_task(runner, principal, name, message, digest)
    return await _start_task(runner, principal, name, agent, message, digest)


def _unusable(message: Message) -> A2aFault | None:
    """What we refuse before looking at anything stored."""
    if any(is_file_part(part) for part in message.parts):
        return fault(
            "ContentTypeNotSupportedError",
            "this agent takes text and JSON parts; a file part is not supported",
        )
    if message_id_too_long(message.messageId):
        return fault("InvalidParamsError", "messageId is at most 255 bytes")
    return None


async def _start_task(  # noqa: PLR0913, PLR0917 - one message and everything it arrived with
    runner: Runner,
    principal: Principal,
    name: str,
    agent: ExposedAgent,
    message: Message,
    digest: str,
) -> Accepted | A2aFault:
    since = runner.generation
    key = receipts.Key(
        principal.tenant,
        receipts.A2A_SEND,
        send_key(principal, name, message.messageId),
        principal_key(principal),
        digest,
    )
    replay = await _replayed(runner, principal, key, message, digest)
    if replay is not None:
        return replay
    claim = claims(None if message.metadata is MISSING else message.metadata)
    hops = claimed_hops(claim)
    if hops is not None and hops >= MAX_HOPS:
        return Accepted(_too_deep(principal, name, message), None)
    context_id = message.contextId if message.contextId is not MISSING else uuid7(now_ms())
    thread_id = a2a_thread_id(principal, name, context_id)
    if await open_run(runner, principal, thread_id):
        return fault("UnsupportedOperationError", "a task is already working in this context")
    target = await context_branch(runner, name, principal.tenant, thread_id)
    if isinstance(target, Err):
        return fault("InvalidParamsError", target.error.message)
    thread, bound = target.value
    run = Launched(
        bound,
        text_of(message.parts),
        thread,
        principal,
        budget=agent.budget,
        a2a=_recorded(message, context_id, claim),
    )
    started = await recorded_run(runner, key, run, since)
    if isinstance(started, Err):
        return _refused(message, started.error.code, started.error.message)
    accepted = started.value
    at = Located(accepted.thread_id, accepted.branch_id, accepted.run_id)
    return await located(runner, principal, at)


async def _replayed(
    runner: Runner, principal: Principal, key: receipts.Key, message: Message, digest: str
) -> Accepted | A2aFault | None:
    """The task this messageId already made, looked up **before any contextId is minted**, so a
    retry that carries no contextId still answers the original task and the original context. None
    when this messageId is new."""
    stored = await (await open_store(runner.store(principal.tenant))).tables.receipt(key)
    if stored is None:
        return None
    if stored.body_hash != digest:
        return _reused(message)
    at = Located(stored.thread_id, stored.branch_id, stored.run_id)
    return await located(runner, principal, at)


def _reused(message: Message) -> A2aFault:
    why = f"messageId {message.messageId} was already used with a different message"
    return fault("InvalidParamsError", why)


def _refused(message: Message, code: str, why: str) -> A2aFault:
    """A run that recorded no input. A key taken between our read and our commit is the two-host
    race: the winner's receipt is what a retry replays, so the caller is told to ask again with the
    same message rather than being given a second run."""
    if code == "branch_busy":
        return fault("UnsupportedOperationError", why)
    if code == "idempotency_key_reused":
        return _reused(message)
    return fault("InvalidParamsError", why)


def _too_deep(principal: Principal, name: str, message: Message) -> Task:
    """A claimed hop count too deep. Nothing is stored and no run starts, so the refusal costs the
    caller everything and us nothing; the id is derived from the request, so a retry is answered
    identically. A claim is never authority, so this is the only thing a claim can decide."""
    task_id = rejected_task_id(principal, name, message.messageId)
    context = message.contextId if message.contextId is not MISSING else task_id
    return Task(
        id=task_id,
        contextId=context,
        status=TaskStatus(
            state="TASK_STATE_REJECTED",
            message=Message(
                messageId=task_id,
                role="ROLE_AGENT",
                parts=[TextPart(text="call chain too deep")],
            ),
        ),
    )


def _recorded(
    message: Message, context_id: str, claim: Mapping[str, JsonValue] | None
) -> Mapping[str, JsonValue]:
    """The run's `user_input.a2a`: the message it arrived as, and the caller's claim as a claim."""
    recorded: dict[str, JsonValue] = {
        "message_id": message.messageId,
        "context_id": context_id,
    }
    if message.taskId is not MISSING:
        recorded["task_id"] = message.taskId
    if claim is not None:
        recorded["claims"] = dict(claim)
    return recorded
