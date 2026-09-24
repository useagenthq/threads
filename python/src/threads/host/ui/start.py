"""A UI route's run start (spec/schema/ui/README.md, "Bodies"): the last user message starts a
run on the key's thread, once. Its id keys the run: a retry finds it by the `ui` receipt, or as
the event id of one of the thread's user_inputs, and replays it; a different text under the same
id is idempotency_key_reused."""

from dataclasses import dataclass

from threads._generated.host_api_v1 import RunAccepted
from threads.agents.store import now_ms, open_store
from threads.host.runs import Runner
from threads.host.start import Launched, Started, recorded_run
from threads.log import BranchId, ParseError, Principal, ThreadId, UserInputEvent
from threads.log.keys import principal_key
from threads.result import Err, Ok
from threads.store import receipts
from threads.store.lines import uuid7
from threads.thread.handle import Thread


@dataclass(frozen=True, slots=True)
class UserMessage:
    id: str
    text: str


def _reused(message: UserMessage) -> Err[ParseError]:
    why = f"message {message.id} was sent with another text"
    return Err(ParseError("idempotency_key_reused", why))


async def find_ui_run(
    runner: Runner, agent: str, principal: Principal, thread_id: ThreadId, message: UserMessage
) -> Started | None:
    """The run a message already started, by its `ui` receipt, else as the event id of one of
    the thread's user_inputs; None for a new message. Another text is idempotency_key_reused."""
    sq = await open_store(runner.store(principal.tenant))
    key = receipts.Key(
        principal.tenant, receipts.UI, receipts.ui_key(thread_id, message.id), "", ""
    )
    found = await sq.tables.receipt(key)
    if found is not None:
        wanted = runner.bound_to(agent).definition.name
        if found.body_hash != receipts.ui_body_hash(wanted, message.text):
            return _reused(message)
        return Ok(RunAccepted(thread_id=thread_id, branch_id=found.branch_id, run_id=found.run_id))
    root = await sq.root(thread_id)
    read = None if not isinstance(root, Ok) else await sq.read(root.value, now_ms())
    if read is None or not isinstance(read, Ok):
        return None
    run = next(
        (
            e
            for e in read.value.fold.events
            if isinstance(e, UserInputEvent) and e.event_id == message.id
        ),
        None,
    )
    if run is None:
        return None
    if receipts.input_text(run) != message.text:
        return _reused(message)
    return Ok(RunAccepted(thread_id=thread_id, branch_id=run.branch_id, run_id=run.event_id))


async def start_ui_run(
    runner: Runner, agent: str, principal: Principal, thread_id: ThreadId, message: UserMessage
) -> Started:
    """Replays the message's run, or starts it on the key's thread (created with its derived id
    when missing, atomically)."""
    since = runner.generation
    found = await find_ui_run(runner, agent, principal, thread_id, message)
    if found is not None:
        return found
    wanted = runner.bound_to(agent)
    store = runner.store(principal.tenant)
    now = now_ms()
    branch = await (await open_store(store)).root_or_create(thread_id, BranchId(uuid7(now)), now)
    bound = await runner.bound(store, thread_id)
    if bound is not None and bound.definition is not wanted.definition:
        why = f"this chat's thread does not run agent {agent}"
        return Err(ParseError("invalid_request", why))
    body = receipts.ui_body_hash(wanted.definition.name, message.text)
    key = receipts.Key(
        principal.tenant,
        receipts.UI,
        receipts.ui_key(thread_id, message.id),
        principal_key(principal),
        body,
    )
    thread = Thread(thread_id, branch, store)
    run = Launched(wanted, message.text, thread, principal, client_message_id=message.id)
    return await recorded_run(runner, key, run, since)
