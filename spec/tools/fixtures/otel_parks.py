# pyright: strict
"""OpenTelemetry goldens of parked turns, built from the event order the loop writes: approvals
granted and denied, two approvals, an unknown effect a human resolves, and a cancel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, BRANCH, DAY, T0, sha, text, tokens
from .jcs import JsonValue, Obj, canonical
from .log import Log
from .otel_case import TENANT, Sync, write
from .otel_expect import Ctx, Span, event
from .otel_shapes import chat_of, continuation_of, tool_of, turn_of
from .pieces import EMAIL, EMAIL_IN, READ_FILE, answer, call, effect_call, started, user

if TYPE_CHECKING:
    import pathlib

CTX = Ctx(TENANT, BRANCH)


def build(root: pathlib.Path) -> None:
    _granted(root)
    _denied(root)
    _two_approvals(root)
    _unknown_resolved(root)
    _cancel(root)


def _challenge(call_id: str) -> str:
    return f"0192c000-0000-7000-8000-{int(call_id.removeprefix('call_')):012x}"


def _ask(log: Log, call_id: str) -> None:
    log.add("permission_decision", {"call_id": call_id, "decision": "ask", "source": "policy"})


def _request_and_park(log: Log, call_id: str) -> Obj:
    """approval_requested, then parked on it. Returns the parked."""
    data: Obj = {
        "challenge_id": _challenge(call_id),
        "call_id": call_id,
        "args_hash": sha(canonical(EMAIL_IN)),
        "expires_at": T0 + 3_600_000,
    }
    log.add("approval_requested", data)
    address: Obj = {"kind": "approval", "id": _challenge(call_id)}
    return log.add("parked", {"address": address, "reason": "awaiting_approval"})


def _answer(log: Log, call_id: str, granted: bool) -> Obj:
    """The approver's answer, then resumed on it. Returns the answer."""
    binding: Obj = {
        "challenge_id": _challenge(call_id),
        "call_id": call_id,
        "args_hash": sha(canonical(EMAIL_IN)),
    }
    kind = "approval_granted" if granted else "approval_denied"
    got = log.add(kind, binding, actor="approver", principal=ALICE)
    address: Obj = {"kind": "approval", "id": _challenge(call_id)}
    log.add("resumed", {"address": address, "cause_event_id": got["event_id"]})
    return got


def _send(log: Log, call_id: str, attempt: int = 1) -> None:
    """effect_begin, effect_commit and the result of an email send."""
    log.add("effect_begin", {"call_id": call_id, "attempt": attempt})
    ref = log.art(b"sent", "text/plain")
    log.add("effect_commit", {"call_id": call_id, "result_ref": ref})
    data: Obj = {
        "call_id": call_id,
        "is_error": False,
        "completeness": "complete",
        "preview": "sent",
        "origin": "executed",
    }
    log.add("tool_result", data, actor="tool")


def _parked_turn(log: Log) -> tuple[Span, Span, Span]:
    """The turn, chat and tool spans of seq 2..8: an email call parked for approval."""
    e = log.events
    t = turn_of(CTX, e[1], e[7])
    return t, chat_of(CTX, t, e[2], e[3]), tool_of(CTX, t, log, 5, 8, "unguarded")


def _granted(root: pathlib.Path) -> None:
    log = Log()
    started(log, [EMAIL])
    effect_call(
        log, EMAIL, EMAIL_IN, "Email bob.", permission={"decision": "ask", "source": "policy"}
    )
    _request_and_park(log, "call_1")
    got = _answer(log, "call_1", granted=True)
    _send(log, "call_1")
    answer(log, "Sent.")
    e = log.events
    t1, c1, tool1 = _parked_turn(log)
    run = text(e[1]["event_id"])
    t2 = turn_of(CTX, got, e[-1], (event(got),), trace=t1.trace, links=(t1,), run_id=run)
    spans = [
        t1,
        c1,
        tool1,
        t2,
        continuation_of(CTX, t2, log, 5, 10, 13, tool1, "unguarded"),
        chat_of(CTX, t2, e[13], e[14]),
    ]
    write(
        root,
        "otel-approval-resume-executes",
        "The park closes the turn and the tool span (threads.parked). The approval opens the "
        "resumed turn in the same trace, linked to the parked turn; resumed opens a "
        "continuation of the call that carries effect_begin and effect_commit and links to the "
        "parked tool span.",
        [log],
        [Sync(BRANCH, log.seq, 0, spans)],
    )


def _denied(root: pathlib.Path) -> None:
    log = Log()
    started(log, [EMAIL])
    effect_call(
        log, EMAIL, EMAIL_IN, "Email bob.", permission={"decision": "ask", "source": "policy"}
    )
    _request_and_park(log, "call_1")
    got = _answer(log, "call_1", granted=False)
    data: Obj = {
        "call_id": "call_1",
        "is_error": True,
        "completeness": "complete",
        "preview": "denied",
        "origin": "denied",
    }
    log.add("tool_result", data)
    answer(log, "Not sent.")
    e = log.events
    t1, c1, tool1 = _parked_turn(log)
    run = text(e[1]["event_id"])
    t2 = turn_of(CTX, got, e[-1], (event(got),), trace=t1.trace, links=(t1,), run_id=run)
    write(
        root,
        "otel-approval-denied",
        "A denial is an event of the resumed turn span, which it opens. The denied call never "
        "runs, so no continuation opens, though the loop writes its result after resumed.",
        [log],
        [Sync(BRANCH, log.seq, 0, [t1, c1, tool1, t2, chat_of(CTX, t2, e[-3], e[-2])])],
    )


def _two_calls(log: Log) -> None:
    user(log, "Email bob and carol.")
    r = log.model_request()
    uses: list[JsonValue] = [
        {"type": "tool_use", "call_id": c, "name": "send_email", "input": EMAIL_IN}
        for c in ("call_1", "call_2")
    ]
    log.model_response(r, uses, "tool_use", tokens(80, 40))
    for c in ("call_1", "call_2"):
        log.tool_call(r, c, "send_email", EMAIL_IN)
        _ask(log, c)


def _two_approvals(root: pathlib.Path) -> None:
    log = Log()
    started(log, [EMAIL])
    _two_calls(log)
    _request_and_park(log, "call_1")
    got1 = _answer(log, "call_1", granted=True)
    _send(log, "call_1")
    _request_and_park(log, "call_2")
    e = log.events
    t1 = turn_of(CTX, e[1], e[9])
    a, b = tool_of(CTX, t1, log, 5, 10, "unguarded"), tool_of(CTX, t1, log, 7, 10, "unguarded")
    run = text(e[1]["event_id"])
    turn2 = (e[-1], (event(got1), event(e[15])))
    t2 = turn_of(CTX, got1, turn2[0], turn2[1], trace=t1.trace, links=(t1,), run_id=run)
    first = [t1, chat_of(CTX, t1, e[2], e[3]), a, b, t2]
    first.append(continuation_of(CTX, t2, log, 5, 12, 15, a, "unguarded"))
    write(
        root,
        "otel-two-approvals-one-resume",
        "Two calls wait for approval and only the first is granted. resumed opens a "
        "continuation for it alone: the second is still waiting on its own approval, so its "
        "approval_requested lands on the resumed turn, which the second park closes.",
        [log],
        [Sync(BRANCH, log.seq, 0, first)],
    )
    head = log.seq
    got2 = _answer(log, "call_2", granted=True)
    _send(log, "call_2")
    answer(log, "Both sent.")
    e = log.events
    t3 = turn_of(CTX, got2, e[-1], (event(got2),), trace=t1.trace, links=(t2,), run_id=run)
    rest = [t3, continuation_of(CTX, t3, log, 7, 19, 22, b, "unguarded")]
    rest.append(chat_of(CTX, t3, e[22], e[23]))
    write(
        root,
        "otel-resumed-root",
        "After two parks the second resumed turn is still in the first user_input's trace, "
        "linked to the turn it resumes; the second call's continuation links to its parked "
        "tool span.",
        [log],
        [Sync(BRANCH, head, 0, first), Sync(BRANCH, log.seq, head, rest)],
    )


def _unknown_resolved(root: pathlib.Path) -> None:
    log = Log()
    started(log, [EMAIL])
    effect_call(log, EMAIL, EMAIL_IN, "Email bob.")
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    log.add("effect_unknown", {"call_id": "call_1", "reason": "crash_after_begin"})
    address: Obj = {"kind": "effect", "id": f"{BRANCH}:call_1"}
    park: Obj = {"address": address, "reason": "effect_unknown", "expires_at": T0 + DAY}
    log.add("parked", park)
    decided: Obj = {"call_id": "call_1", "outcome": "assume_not_done", "by": "human"}
    got = log.add("effect_resolved", decided, actor="approver", principal=ALICE)
    log.add("resumed", {"address": address, "cause_event_id": got["event_id"]})
    _send(log, "call_1", attempt=2)
    answer(log, "Sent.")
    e = log.events
    t1 = turn_of(CTX, e[1], e[8])
    tool1 = tool_of(CTX, t1, log, 5, 9, "unguarded")
    run = text(e[1]["event_id"])
    t2 = turn_of(CTX, got, e[-1], (event(got),), trace=t1.trace, links=(t1,), run_id=run)
    spans = [
        t1,
        chat_of(CTX, t1, e[2], e[3]),
        tool1,
        t2,
        continuation_of(CTX, t2, log, 5, 11, 14, tool1, "unguarded"),
        chat_of(CTX, t2, e[14], e[15]),
    ]
    write(
        root,
        "otel-effect-unknown-resolved",
        "An unknown effect parks. The human's effect_resolved is the first event after the "
        "park, so it opens the resumed turn and is its first span event; the retried send is "
        "a continuation linked to the parked tool span.",
        [log],
        [Sync(BRANCH, log.seq, 0, spans)],
    )


def _cancel(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE])
    user(log, "What is in README.md?")
    call(log, "read_file", {"path": "README.md"})
    cancel = log.add("cancel_requested", {"scope": "turn"}, actor="user", principal=ALICE)
    stopped: Obj = {
        "call_id": "call_1",
        "is_error": True,
        "completeness": "complete",
        "preview": "not executed: cancelled",
        "origin": "not_executed",
    }
    log.add("tool_result", stopped)
    log.add("cancelled", {"request_event_id": cancel["event_id"]})
    log.add("turn_completed", {"reason": "cancelled"})
    e = log.events
    t = turn_of(CTX, e[1], e[-1])
    spans = [t, chat_of(CTX, t, e[2], e[3]), tool_of(CTX, t, log, 5, 8, "read_only")]
    write(
        root,
        "otel-cancel-during-tool",
        "main's cancel order: cancel_requested while the tool runs, a not_executed result for "
        "the pending call, cancelled, then turn_completed{cancelled}. The tool span closes at "
        "its own result with status ERROR and no threads.cut; the turn closes as cancelled.",
        [log],
        [Sync(BRANCH, log.seq, 0, spans)],
    )
