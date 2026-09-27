"""A message with a taskId continues that task, and only while it is INPUT_REQUIRED: its text
answers the run's open ask_user, strictly to that question's options. The continuation carries a
receipt under the same key form as a send, so a retried answer returns the task in its current state
rather than an error — a caller in doubt about us is never punished for asking again."""

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import Message
from threads.a2a.protocol import A2aFault, fault, is_settled, is_terminal, text_of
from threads.agents.store import now_ms, open_store
from threads.host.a2a.keys import send_key
from threads.host.a2a.read import Accepted, Located, located, slice_of
from threads.host.a2a.state import open_question, task_of
from threads.host.runs import Runner
from threads.log import Principal
from threads.log.keys import principal_key
from threads.result import Err
from threads.store import receipts
from threads.thread.handle import Thread


async def continue_task(
    runner: Runner, principal: Principal, name: str, message: Message, digest: str
) -> Accepted | A2aFault:
    if message.taskId is MISSING:
        raise AssertionError("a continuation carries a taskId")
    task_id = message.taskId
    store = runner.store(principal.tenant)
    sq = await open_store(store)
    key = receipts.Key(
        principal.tenant,
        receipts.A2A_SEND,
        send_key(principal, name, message.messageId),
        principal_key(principal),
        digest,
    )
    # The receipt first here too: a retried answer is answered from what is stored.
    prior = await sq.tables.receipt(key)
    if prior is not None:
        if prior.body_hash != digest:
            why = f"messageId {message.messageId} was already used with a different message"
            return fault("InvalidParamsError", why)
        at = Located(prior.thread_id, prior.branch_id, prior.run_id)
        return await located(runner, principal, at)
    # Resolved only through the caller's own receipts: another principal's task is not found, and is
    # indistinguishable from one that never existed.
    owned = await sq.tables.a2a_task(principal_key(principal), task_id)
    if owned is None:
        return fault("TaskNotFoundError", f"no task {task_id}")
    at = Located(owned.thread_id, owned.branch_id, owned.run_id)
    answered = await _answer(runner, principal, at, message, task_id)
    if answered is not None:
        return answered
    # The answer is recorded; the receipt follows, so a later retry of this messageId replays it.
    row = receipts.TaskReceipt(key.idempotency_key, at.thread, at.branch, at.run_id, now_ms())
    await sq.tables.record_receipt(key, row)
    return await located(runner, principal, at)


async def _answer(
    runner: Runner, principal: Principal, at: Located, message: Message, task_id: str
) -> A2aFault | None:
    """The message's text as the answer to the task's open question, or why it is not one."""
    view = await slice_of(runner, principal.tenant, at)
    if view is None:
        return fault("InternalError", f"task {task_id} could not be read")
    state = task_of(view).status.state
    if is_terminal(state):
        return fault(
            "UnsupportedOperationError", f"task {task_id} has ended and cannot be continued"
        )
    if not is_settled(state):
        return fault("UnsupportedOperationError", "task is still working; wait or cancel it")
    question = open_question(view)
    if question is None:
        return fault("InternalError", f"task {task_id} has no open question")
    thread = Thread(at.thread, at.branch, runner.store(principal.tenant))
    done = await thread.answer(question.call_id, text_of(message.parts), principal)
    if isinstance(done, Err):
        refused = done.error.code == "invalid_answer"
        return fault(
            "InvalidParamsError" if refused else "UnsupportedOperationError", done.error.message
        )
    await runner.resume(thread.store, at.thread, at.branch, runner.generation)
    return None
