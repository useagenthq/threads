"""Team tools: the lead's log holds the shared task list and mailbox, and a
member changes it only through the lead's writer, so 's single writer makes each claim
atomic. A redelivered call never claims twice or sends twice: a claim the member already holds
and a message id already sent are answered without a second event."""

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import (
    SendMessageInput,
    TeamTaskClaimInput,
    TeamTaskCreateInput,
    TeamTaskUpdateInput,
)
from threads.log import InjectedEvent, TeamMessageEvent, ToolCallData
from threads.loop.drafts import draft
from threads.loop.history import CallState
from threads.loop.results import As, result_draft
from threads.loop.runtime import Halt, Runtime, lost
from threads.result import Err
from threads.store import Draft
from threads.tools import TEAM


async def team_tool(lead: Runtime, member: str, rt: Runtime, state: CallState) -> Halt | None:
    """`rt` is the calling thread; `lead` is the team's lead (`rt` itself for the lead)."""
    call = state.call.data
    event, text, failed = _decide(lead, member, call)
    if event is not None and lead is not rt:
        # The lead's writer records the team state; this member's writer records the result.
        done = await lead.append(event)
        if isinstance(done, Err):
            return lost(done.error)
        event = None
    result = await result_draft(rt, call.call_id, text, As("executed", failed))
    done = await rt.append(*((event, result) if event is not None else (result,)))
    return lost(done.error) if isinstance(done, Err) else None


type Answer = tuple[Draft | None, str, bool]
"""The team event to append (if any), the result text, and whether it is an error."""


def _decide(lead: Runtime, member: str, call: ToolCallData) -> Answer:
    """Ids are keyed to the call (`<member>/<call_id>`), so a call re-run after a crash finds
    what it already recorded and appends nothing (invariant 3)."""
    input = dict(call.input)
    match call.name:
        case "team_task_create":
            args = TeamTaskCreateInput.model_validate(input)
            return _create(lead, f"{member}/{call.call_id}", args)
        case "team_task_claim":
            return _claim(lead, member, TeamTaskClaimInput.model_validate(input).task_id)
        case "team_task_update":
            return _update(lead, member, TeamTaskUpdateInput.model_validate(input))
        case _:
            args_m = SendMessageInput.model_validate(input)
            return _send(lead, member, f"{member}/{call.call_id}", args_m)


def _create(lead: Runtime, task_id: str, args: TeamTaskCreateInput) -> Answer:
    if task_id in lead.fold.tasks:
        return None, task_id, False
    blockers = [] if args.blocked_by is MISSING else [str(b) for b in args.blocked_by]
    unknown = [b for b in blockers if b not in lead.fold.tasks]
    if unknown:
        return None, f"no task {unknown[0]}", True
    data: dict[str, JsonValue] = {"task_id": task_id, "subject": args.subject}
    data["blocked_by"] = list[JsonValue](blockers)
    if args.description is not MISSING:
        data["description"] = args.description
    return draft("team_task_created", data), task_id, False


def _claim(lead: Runtime, member: str, task_id: str) -> Answer:
    tasks = lead.fold.tasks
    task = tasks.get(task_id)
    if task is not None and task.status == "claimed" and task.owner == member:
        return None, f"claimed {task_id}", False
    if task is None or task.status != "open":
        return None, f"can't claim: task {task_id} is not open", True
    blocked = [b for b in task.blocked_by if b not in tasks or tasks[b].status != "completed"]
    if blocked:
        return None, f"can't claim: task {task_id} has a blocker that is not completed", True
    claim = {"task_id": task_id, "member": member}
    return draft("team_task_claimed", claim), f"claimed {task_id}", False


def _update(lead: Runtime, member: str, args: TeamTaskUpdateInput) -> Answer:
    task = lead.fold.tasks.get(args.task_id)
    done = f"{args.task_id} {args.status}"
    if task is not None and task.status == args.status:
        return None, done, False
    if task is None or task.status != "claimed" or task.owner != member:
        return None, f"{args.task_id} is not claimed by {member}", True
    data: dict[str, JsonValue] = {"task_id": args.task_id, "status": args.status}
    return draft("team_task_updated", data), done, False


def _send(lead: Runtime, member: str, message_id: str, args: SendMessageInput) -> Answer:
    sent = any(
        isinstance(e, TeamMessageEvent) and e.data.message_id == message_id for e in lead.events
    )
    if sent:
        return None, "sent", False
    data: dict[str, JsonValue] = {
        "message_id": message_id,
        "from": member,
        "to": args.to,
        "text": args.text,
    }
    return draft("team_message", data), "sent", False


async def deliver(lead: Runtime, member: str, rt: Runtime) -> Halt | bool:
    """Messages to this member (or to everyone) it hasn't seen yet, as untrusted reference;
    delivery is deduplicated by message id, so a restart never injects one twice."""
    seen = {
        e.data.origin.id
        for e in rt.events
        if isinstance(e, InjectedEvent) and e.data.source == "agent"
    }
    drafts: list[Draft] = []
    for event in lead.events:
        if not isinstance(event, TeamMessageEvent):
            continue
        data = event.data
        addressed = data.to in (member, "*") and data.from_ != member
        if addressed and data.message_id not in seen and isinstance(data.text, str):
            note: dict[str, JsonValue] = {
                "source": "agent",
                "trust": "untrusted_reference",
                "origin": {"id": data.message_id},
                "text": f"{data.from_}: {data.text}",
            }
            drafts.append(draft("injected", note))
    if not drafts:
        return False
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else True


class Lead:
    """A thread that is its own team lead and starts no subagents: only the team tools, acting
    on its own log (the conformance runner's framework, spec/conformance/README.md)."""

    def __init__(self, member: str) -> None:
        self._member = member

    @property
    def names(self) -> frozenset[str]:
        return TEAM

    async def run(self, rt: Runtime, state: CallState) -> Halt | None:
        return await team_tool(rt, self._member, rt, state)

    async def flush(self, rt: Runtime) -> Halt | None:
        return None

    async def deliver(self, rt: Runtime) -> Halt | bool:
        return await deliver(rt, self._member, rt)
