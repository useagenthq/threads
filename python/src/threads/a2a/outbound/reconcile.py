"""Reconciliation: the only thing that can turn a lost answer into a confirmed success.

It asks the partner for the tasks of this call's context and looks for our own messageId in one's
history. The lookup's finality is NONFINAL, and that is the whole point: a ListTasks that finds
nothing may be looking while the request is still in flight, or at a history the peer has truncated,
so it never proves absence and never settles the park by itself (30-uncertainty.md Part 1)."""

from pydantic import ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import ListTasksResponse, Task
from threads.a2a.outbound.card import Unresolved, resolve_card
from threads.a2a.outbound.exchange import preview, status_of, task_text
from threads.a2a.outbound.log import called_in
from threads.a2a.outbound.request import list_params
from threads.a2a.outbound.send import headers
from threads.a2a.protocol import Answer, Answered, Faulted, Sending, call
from threads.a2a.remote import Remote
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFound
from threads.loop.tools import Invocation

MAX_PAGE_SIZE = 100
"""The ceiling the proto's own comment on `page_size` sets."""


async def reconcile_send(
    remote: Remote, invocation: Invocation, sending: Sending
) -> LookupResult[str]:
    called = called_in(invocation.events(), invocation.call_id)
    if called is None:
        return LookupUnknown("the call has no remote_call to look up")
    card = await resolve_card(remote, invocation, sending)
    if isinstance(card, Unresolved):
        return LookupUnknown(card.why)
    answer = await call(
        card.card.wire,
        "ListTasks",
        list_params(called.context_id, MAX_PAGE_SIZE),
        headers(card.card, sending),
    )
    page = _page(answer)
    if isinstance(page, LookupUnknown):
        return page
    mine = next((t for t in page.tasks if _carries(t, called.message_id)), None)
    # Reported as what we saw; whether "nothing here" settles anything is the tool's declared
    # finality to decide, never this answer's, and for A2A it is nonfinal.
    if mine is None:
        return NotFound()
    return Found(preview(status_of(mine.status.state), mine.id, task_text(mine)))


def _page(answer: Answer) -> ListTasksResponse | LookupUnknown:
    """The peer's task list, or why this lookup answered nothing. A read that did not answer is
    never absence: it is one more reason the park stands."""
    if isinstance(answer, Faulted):
        return LookupUnknown(f"{answer.fault.name}: {answer.fault.message}")
    if not isinstance(answer, Answered):
        return LookupUnknown("the lookup did not answer")
    try:
        return ListTasksResponse.model_validate(answer.value)
    except ValidationError:
        return LookupUnknown("the peer's task list could not be read")


def _carries(task: Task, message_id: str) -> bool:
    """Our own receipt for the send: a task whose history holds the messageId this call derived."""
    if task.history is MISSING:
        return False
    return any(m.messageId == message_id for m in task.history)
