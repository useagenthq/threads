"""Approval challenges on a thread: the open ones, and a
single-use answer.

An answer is checked against the approver policy as it stands now, then against the challenge's
row: it must be this thread's and branch's, open and unexpired. The approval event and the row's
consumption commit together, so a second answer (a redelivered button press, a race) is
approval_duplicate and appends nothing.
"""

import shlex
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from threads._generated.host_api_v1 import PendingApproval
from threads.agents.store import Store, now_ms, open_store
from threads.log import (
    ApprovalRequestedEvent,
    BranchId,
    JsonObject,
    ParkAddress,
    ParseError,
    PermissionRule,
    Principal,
    ThreadId,
    ToolCallEvent,
)
from threads.reduce import Fold
from threads.result import Err, Ok
from threads.store import Draft, approvals
from threads.store.companion import Companion, both
from threads.store.lines import uuid7
from threads.thread.authority import Checked, refused
from threads.thread.control import Controlled, actor, append, principal_key, resumed

if TYPE_CHECKING:
    from pydantic import JsonValue


def pending(fold: Fold) -> tuple[PendingApproval, ...]:
    """Open challenges: requested, unanswered, their call still waiting."""
    calls = {e.data.call_id: e for e in fold.events if isinstance(e, ToolCallEvent)}
    found: list[PendingApproval] = []
    for event in fold.events:
        if not isinstance(event, ApprovalRequestedEvent):
            continue
        data = event.data
        call = calls.get(data.call_id)
        if data.challenge_id in fold.consumed or call is None:
            continue
        if data.call_id not in fold.pending:
            continue
        found.append(
            PendingApproval(
                challenge_id=data.challenge_id,
                call_id=data.call_id,
                tool=call.data.name,
                input=call.data.input,
                args_hash=data.args_hash,
                expires_at=data.expires_at,
                suggested_rules=suggested_rules(call.data.name, call.data.input),
            )
        )
    return tuple(found)


def suggested_rules(tool: str, input: JsonObject) -> tuple[str, ...]:
    """What an approver may keep for the thread: a shell command exactly, or its two-word
    prefix form (`bash(git push:*)`); any other tool as a whole."""
    command = input.get("command")
    if tool != "bash" or not isinstance(command, str) or not command.strip():
        return (tool,)
    try:
        words = shlex.split(command)
    except ValueError:
        return (f"bash({command})",)
    # ponytail: two-word prefix (git push, npm run); a smarter prefix needs the shell grammar.
    prefix = " ".join(words[:2])
    return (f"bash({command})", f"bash({prefix}:*)")


async def decide(  # noqa: PLR0913 - one answer and its bindings
    store: Store,
    thread: tuple[ThreadId, BranchId],
    challenge_id: str,
    principal: Principal,
    decision: Literal["granted", "denied"],
    *,
    authority: Checked | None,
    remember_rule: PermissionRule | None = None,
    reason: str | None = None,
    installation: str | None = None,
    companion: Companion | None = None,
) -> Controlled:
    """approval_granted or approval_denied for an open challenge, consuming its row, then
    resumed when the branch is parked on it. Over a channel, the answer must come from the
    installation the challenge was issued in; `companion` binds more host rows (the inbox item
    that carried the answer) to the same append."""
    denied = await refused(store, thread[0], principal, authority)
    if denied is not None:
        return denied
    now = now_ms()
    row = await _open_row(store, thread, challenge_id, now)
    if isinstance(row, Err):
        return row
    if installation is not None and row.value.installation_id != installation:
        return Err(ParseError("approval_mismatch", "the challenge belongs to another installation"))
    by = actor("approver", principal)
    binding: dict[str, JsonValue] = {
        "challenge_id": challenge_id,
        "call_id": row.value.call_id,
        "args_hash": row.value.args_hash,
    }
    if reason is not None:
        binding["reason"] = reason
    record = Draft(f"approval_{decision}", binding, by, True, uuid7(now))
    rule = _rule(challenge_id, remember_rule, by)

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        if rule is not None:
            offered = next((p for p in pending(fold) if p.challenge_id == challenge_id), None)
            if offered is None or remember_rule not in offered.suggested_rules:
                return Err(ParseError("invalid_request", "not one of the suggested rules"))
        address = ParkAddress(kind="approval", id=challenge_id)
        cause = record.event_id or ""
        return Ok((record, *(() if rule is None else (rule,)), *resumed(fold, address, cause)))

    consume = approvals.consume(challenge_id, decision, principal_key(principal), now)
    return await append(store, thread[1], build, both(consume, companion))


async def _open_row(
    store: Store, thread: tuple[ThreadId, BranchId], challenge_id: str, now: int
) -> Ok[approvals.Challenge] | Err[ParseError]:
    """The challenge's row, if it is this thread's and branch's and still open. An answer at or
    after the expiry expires it: a denial."""
    tables = (await open_store(store)).tables
    row = await tables.challenge(challenge_id)
    if row is None or row.thread_id != thread[0]:
        return Err(ParseError("not_found", f"no challenge {challenge_id} on this thread"))
    if row.branch_id != thread[1]:
        return Err(ParseError("approval_mismatch", "the challenge belongs to another branch"))
    if row.state == "open" and row.expires_at <= now:
        await tables.expire(challenge_id, now)
        return Err(ParseError("approval_expired", f"challenge {challenge_id} expired"))
    if row.state != "open":
        code = "approval_expired" if row.state == "expired" else "approval_duplicate"
        return Err(ParseError(code, f"challenge {challenge_id} is {row.state}"))
    return Ok(row)


def _rule(
    challenge_id: str, rule: PermissionRule | None, by: "dict[str, JsonValue]"
) -> Draft | None:
    if rule is None:
        return None
    data: dict[str, JsonValue] = {"rule": rule, "decision": "allow", "challenge_id": challenge_id}
    return Draft("permission_rule_added", data, by)
