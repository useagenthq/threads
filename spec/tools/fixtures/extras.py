# pyright: strict
"""Thread permission rules, reminder placement and the full hook set (ADRs 0014, 0022, 0023)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, MAX_SAFE, T0, obj, sha, tokens, tool
from .jcs import JsonValue, Obj, canonical
from .log import Log
from .pieces import answer, call, reduce_case, render_case, result, started, user
from .policies import CONTEXT, MODELS, SMALL_SETTINGS, permissions, policy
from .projections import cache_breaks, cost

if TYPE_CHECKING:
    import pathlib

SHELL = tool("bash", "Run a shell command.", {"command": {"type": "string"}}, "read_only")
TODO = tool("todo_write", "Replace the todo list.", {"todos": {"type": "array"}}, "read_only")
RULE = "bash(git push:*)"


def build(root: pathlib.Path) -> None:
    _thread_rule(root)
    _reminder(root)
    _hooks(root)
    _unpinned_projections(root)
    _unpriced_epoch(root)
    _recovered_cache_break(root)
    _usage_overflow(root)
    _cost_overflow(root)


def _ask_call(log: Log, cid: str, cmd: str) -> Obj:
    r = log.model_request()
    inp: Obj = {"command": cmd}
    use: Obj = {"type": "tool_use", "call_id": cid, "name": "bash", "input": inp}
    log.model_response(r, [use], "tool_use", tokens(60, 10))
    return log.tool_call(r, cid, "bash", inp)


def _thread_rule(root: pathlib.Path) -> None:
    log = Log()
    started(log, [SHELL], policy=policy(permissions=permissions("default")))
    user(log, "Push the branch.")
    _ask_call(log, "call_1", "git push origin feat")
    log.add(
        "permission_decision",
        {"call_id": "call_1", "decision": "ask", "source": "mode", "mode": "default"},
    )
    ch = "0192c000-0000-7000-8000-000000000003"
    args_hash = sha(canonical({"command": "git push origin feat"}))
    log.add(
        "approval_requested",
        {
            "challenge_id": ch,
            "call_id": "call_1",
            "args_hash": args_hash,
            "expires_at": T0 + 3_600_000,
        },
    )
    binding: Obj = {"challenge_id": ch, "call_id": "call_1", "args_hash": args_hash}
    log.add("approval_granted", binding, actor="approver", principal=ALICE)
    log.add(
        "permission_rule_added",
        {"rule": RULE, "decision": "allow", "challenge_id": ch},
        actor="approver",
        principal=ALICE,
    )
    result(log, "call_1", "pushed")
    _ask_call(log, "call_2", "git push origin feat --tags")
    log.add(
        "permission_decision",
        {
            "call_id": "call_2",
            "decision": "allow",
            "source": "thread_rule",
            "rule_id": RULE,
            "mode": "default",
        },
    )
    result(log, "call_2", "pushed tags")
    answer(log, "Pushed.")
    reduce_case(
        root,
        (
            "permission-thread-rule-added",
            "permissions_approvals",
            "The approver answers with 'allow for this thread' and picks the prefix rule. "
            "permission_rule_added records it with the approver as actor; the next matching "
            "call is allowed without a prompt, source thread_rule. Thread rules sit after deny "
            "rules and protected paths, so they never open those.",
        ),
        log,
        {},
    )


def _reminder(root: pathlib.Path) -> None:
    log = Log()
    started(log, [TODO])
    user(log, "Plan the migration.")
    items: list[JsonValue] = [{"id": "1", "content": "Add the column", "status": "in_progress"}]
    call(log, "todo_write", {"todos": items})
    log.add("todos_updated", {"call_id": "call_1", "todos": items})
    result(log, "call_1", "ok")
    answer(log, "Planned.")
    for i in range(10):
        user(log, f"Status {i + 1}?")
        answer(log, "Working on it.")
    user(log, "Anything left?")
    note: Obj = {
        "source": "todo",
        "trust": "untrusted_reference",
        "origin": {"id": "todos"},
        "text": "- [in_progress] Add the column",
    }
    log.add("injected", note)
    render_case(
        root,
        (
            "todos-reminder-throttled",
            "tasks",
            "Ten turns pass with open items and no todo_write, so the reminder producer appends "
            "one injected{source: todo} right after the turn's user_input, before the first "
            "request. It renders at that position as untrusted reference and fires at most once "
            "per 10 turns.",
        ),
        log,
    )


def _hook(log: Log, hook: str, decision: str, **more: JsonValue) -> None:
    log.add("hook_decision", {"extension": "ops", "hook": hook, "decision": decision, **more})


def _hooks(root: pathlib.Path) -> None:
    log = Log()
    started(log, [SHELL], policy=policy())
    _hook(log, "session_start", "proceed")
    ctx: Obj = {
        "source": "hook",
        "trust": "trusted_instruction",
        "origin": {"id": "ops"},
        "text": "On-call: alice.",
    }
    log.add("injected", ctx)
    u = user(log, "Check disk usage.")
    _hook(log, "before_input", "allow", input_event_id=u["event_id"])
    _ask_call(log, "call_1", "df -h")
    _hook(log, "before_tool", "allow", call_id="call_1")
    log.add("permission_decision", {"call_id": "call_1", "decision": "allow", "source": "hook"})
    result(log, "call_1", "/ 40% used")
    _hook(log, "before_tool_result", "proceed", call_id="call_1")
    _hook(log, "after_tool_batch", "proceed")
    _hook(log, "notification", "failed", reason="webhook timed out")
    _hook(log, "before_model_switch", "allow")
    log.add(
        "settings_changed",
        {"reason": "user", "settings": SMALL_SETTINGS},
        actor="user",
        principal=ALICE,
    )
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": "Disk is at 40%."}], "end_turn", tokens(90, 8))
    _hook(log, "on_stop", "stop")
    log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "hooks-full-set-recorded",
            "hooks",
            "Hooks across one turn: session_start (context, with its injection), before_input, "
            "before_tool (folded as permission source hook), before_tool_result, "
            "after_tool_batch, before_model_switch before settings_changed, and on_stop. A "
            "notification observer failing is recorded and changes nothing; the turn "
            "completes.",
        ),
        log,
        {},
    )


def _unpinned_projections(root: pathlib.Path) -> None:
    log = Log()
    started(log, [], policy={"models": MODELS})
    for i, cache_read in enumerate((8000, 1000)):
        user(log, f"Question {i + 1}.")
        r = log.model_request()
        usage: Obj = {**tokens(100, 10), "cache_read_tokens": cache_read, "cache_write_tokens": 0}
        log.model_response(r, [{"type": "text", "text": f"Answer {i + 1}."}], "end_turn", usage)
        log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "projections-absent-without-policy",
            "context_compaction",
            "The policy pins priced models but no currency and no context. cost is null: "
            "prices mean nothing without a currency, and a zero would read as free. "
            "cache_breaks is null though reads fell from 8000 to 1000: the projection has no "
            "pinned ttl to judge against.",
        ),
        log,
        {"cost": cost(log), "cache_breaks": cache_breaks(log)},
    )


def _unpriced_epoch(root: pathlib.Path) -> None:
    unpriced = {k: v for k, v in obj(MODELS[0]).items() if k != "price"}
    log = Log()
    started(log, [], policy={"models": [unpriced], "currency": "USD"})
    user(log, "Question.")
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": "Answer."}], "end_turn", tokens(100, 10))
    log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "cost-unpriced-model-incomplete",
            "cancellation_resume",
            "The pinned model declares no price. Its response is known usage but has no known "
            "cost: cost adds nothing for it and is neither complete nor bounded, never an exact "
            "zero.",
        ),
        log,
        {"cost": cost(log)},
    )


def _recovered_cache_break(root: pathlib.Path) -> None:
    log = Log()
    started(log, [], policy=policy(context=CONTEXT))
    for i, cache_read in enumerate((8000, 1000)):
        user(log, f"Question {i + 1}.")
        r = log.model_request()
        usage: Obj = {**tokens(100, 10), "cache_read_tokens": cache_read, "cache_write_tokens": 0}
        reply: list[JsonValue] = [{"type": "text", "text": f"Answer {i + 1}."}]
        if i == 0:
            log.model_response(r, reply, "end_turn", usage)
        else:
            recovered: Obj = {
                "request_event_id": r["event_id"],
                "provider_request_id": "msg_02",
                "content": reply,
                "stop_reason": "end_turn",
                "usage": usage,
                "completeness": "complete",
            }
            log.add("model_response_recovered", recovered, actor="recovery")
        log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "cache-break-recovered-response",
            "context_compaction",
            "The second turn's response was recovered by lookup after a crash. A recovered "
            "response is a turn response like any other, so its cache reads falling from 8000 to "
            "1000 is a break (unknown cause), and its usage counts.",
        ),
        log,
        {"cache_breaks": cache_breaks(log)},
    )


def _usage_overflow(root: pathlib.Path) -> None:
    log = Log()
    started(log, [])
    half = MAX_SAFE // 2 + 1
    for i in range(2):
        user(log, f"Question {i + 1}.")
        r = log.model_request()
        reply: list[JsonValue] = [{"type": "text", "text": f"Answer {i + 1}."}]
        log.model_response(r, reply, "end_turn", tokens(half, 10))
        log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "usage-overflow-counts-unknown",
            "cancellation_resume",
            "Each response reports more than half of 2^53-1 input tokens. The second would take "
            "the input total past the wire's integers, so it adds neither count and is counted "
            "in unknown_responses: a total is never out of range, rounded or saturated.",
        ),
        log,
        {},
    )


def _cost_overflow(root: pathlib.Path) -> None:
    log = Log()
    started(log, [], policy=policy())
    user(log, "Question.")
    r = log.model_request()
    # 4e12 input tokens at 3000 nanos each: 1.2e16 nanos, past 2^53-1.
    reply: list[JsonValue] = [{"type": "text", "text": "Answer."}]
    log.model_response(r, reply, "end_turn", tokens(4_000_000_000_000, 10))
    log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "cost-overflow-projection",
            "cancellation_resume",
            "The known cost passes 2^53-1 nanos. The cost projection is the typed overflow "
            "outcome {error: cost_overflow}, never an out-of-range, rounded or saturated amount.",
        ),
        log,
        {"cost": cost(log)},
    )
