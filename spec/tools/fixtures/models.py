# pyright: strict
"""Model settings epochs, retries and fallback, budgets and structured output (ADRs 0012, 0020)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, NOW, obj, tokens
from .jcs import JsonValue, Obj, canonical
from .log import Log, reduce
from .pieces import (
    FINAL,
    READ_FILE,
    call,
    case,
    negative,
    reduce_case,
    reject,
    render_case,
    result,
    started,
    user,
    write_case,
)
from .policies import FINAL_OUTPUT, OUTPUT, RETRY, SMALL_SETTINGS, policy
from .projections import cost
from .render import render

if TYPE_CHECKING:
    import pathlib

FAM = "cancellation_resume"
BUDGET = 770_000_000
# One scripted-1 attempt reserves 200000 x 3750 + 1024 x 15000 = 765,360,000 nanos.
RESERVE = 200_000 * 3750 + 1024 * 15_000


def build(root: pathlib.Path) -> None:
    _epochs(root)
    _prefix_cases(root)
    _fallback(root)
    _budget(root)
    _output(root)


def _epochs(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy())
    user(log, "hi")
    r = log.model_request()
    block = canonical({"signature": "sig_9", "thinking": "Greet back.", "type": "thinking"})
    thinking: Obj = {
        "type": "reasoning",
        "provider": "scripted",
        "model": "scripted-1",
        "format": "thinking",
        "ref": log.art(block, "application/json"),
    }
    log.model_response(r, [thinking, {"type": "text", "text": "Hello!"}], "end_turn", tokens(40, 9))
    log.add("turn_completed", {"reason": "end_turn"})
    log.add(
        "settings_changed",
        {"reason": "user", "settings": SMALL_SETTINGS},
        actor="user",
        principal=ALICE,
    )
    user(log, "Which model are you?")
    render_case(
        root,
        (
            "settings-change-new-prefix-epoch",
            FAM,
            "A settings_changed starts a new settings epoch: the next request's line 0 names "
            "the new model, and C7 holds within each epoch. reasoning_carryover omit_prior "
            "omits the earlier thinking part as a recorded decision, never silently.",
        ),
        log,
    )
    log = Log()
    started(log, [READ_FILE], policy=policy())
    user(log, "hi")
    log.model_request()
    log.add("settings_changed", {"reason": "fallback", "settings": SMALL_SETTINGS})
    reject(
        root,
        (
            "settings-change-during-attempt-rejected",
            FAM,
            "settings_changed while a model_request awaits its response: invalid_transition.",
        ),
        log,
    )


def _prefix_cases(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy())
    user(log, "hi")
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": "Hello!"}], "end_turn", tokens(40, 3))
    log.add("turn_completed", {"reason": "end_turn"})
    change: Obj = {"reason": "user", "settings": SMALL_SETTINGS}
    log.add("settings_changed", change, actor="user", principal=ALICE)
    user(log, "again")
    body, _ = render(log.events, log.artifacts)
    old = obj(r["data"])["declared_prefix"]
    data: Obj = {
        "attempt": 1,
        "request_ref": log.art(body, "application/x-ndjson"),
        "declared_prefix": old,
    }
    log.add("model_request", data)
    write_case(
        root,
        case(
            "prefix-changed-mid-epoch-rejected",
            FAM,
            "render",
            "After an authorized settings_changed, a request declares the previous epoch's "
            "prefix. Each epoch's prefix must match its own pinned settings; the baseline is "
            "never reset to whatever was sent: prefix_changed.",
        ),
        log,
        {"outcome": "error", "error": {"code": "prefix_changed", "seq": log.seq}},
    )
    log = Log()
    started(log, [READ_FILE], policy=policy())
    log.add("settings_changed", change, actor="model")
    negative(
        root,
        "settings-change-by-model-rejected",
        "A settings_changed whose actor is the model. Only the host, recovery or an operator "
        "principal may change settings: the line fails its schema (invalid_line).",
        log,
        ("invalid_line", log.seq),
    )
    log = Log()
    started(log, [READ_FILE], policy=policy())
    log.add("settings_changed", {**change, "reason": "fallback"}, actor="user", principal=ALICE)
    negative(
        root,
        "settings-change-auto-reason-by-user-rejected",
        "A settings_changed with an automatic reason (fallback) whose actor is a user. "
        "Automatic reasons come only from the host or recovery; a person's change is reason "
        "user: invalid_line.",
        log,
        ("invalid_line", log.seq),
    )


def _abandoned(reason: str, status: int, **more: JsonValue) -> Obj:
    data: Obj = {"provider_outcome": "failed", "reason": reason, "http_status": status, **more}
    return {"type": "model_attempt_abandoned", "data": data}


def _fallback(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy(retry=RETRY, fallback=[SMALL_SETTINGS]))
    user(log, "Say done.")
    r = log.model_request()
    crash: Obj = {
        "request_event_id": r["event_id"],
        "provider_outcome": "unknown",
        "reason": "crash",
    }
    appended: list[JsonValue] = [
        {"type": "model_attempt_abandoned", "actor_kind": "recovery", "epoch": 2, "data": crash},
        {"type": "model_request", "data": {"attempt": 2}},
        _abandoned("rate_limited", 429, retry_after_ms=2000),
        {
            "type": "retry_scheduled",
            "critical": True,
            "data": {"delay_ms": 2000, "basis": "retry_after"},
        },
        {"type": "model_request", "data": {"attempt": 3}},
        _abandoned("overloaded", 529),
        {
            "type": "retry_scheduled",
            "critical": True,
            "data": {"delay_ms": 2000, "basis": "backoff"},
        },
        {"type": "model_request", "data": {"attempt": 4}},
        _abandoned("overloaded", 529),
        {"type": "settings_changed", "data": {"reason": "fallback", "settings": SMALL_SETTINGS}},
        {"type": "model_request", "data": {"attempt": 5}},
        {
            "type": "model_response",
            "data": {"content": FINAL["content"], "completeness": "complete"},
        },
        {"type": "turn_completed", "data": {"reason": "end_turn"}},
    ]
    errors: list[JsonValue] = [
        {"error": {"reason": "rate_limited", "http_status": 429, "retry_after_ms": 2000}},
        {"error": {"reason": "overloaded", "http_status": 529}},
        {"error": {"reason": "overloaded", "http_status": 529}},
    ]
    write_case(
        root,
        case(
            "model-retry-backoff-then-fallback",
            FAM,
            "recover",
            "After a crash re-send, the provider answers 429 with retry-after 2 s (waited as "
            "recorded), then 529 twice (backoff base x 2^(k-1) = 2 s). With fallback_after 2 a "
            "settings_changed{fallback} switches to the configured model, which answers. Every "
            "attempt and wait is an event; the SDK's own retries are off.",
            model_script="model.json",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "appended": appended},
        extra={"model.json": {"responses": [*errors, FINAL]}},
    )


def _budget_log() -> Log:
    log = Log()
    started(log, [READ_FILE], policy=policy())
    log.add(
        "user_input",
        {"source": "api", "text": "What is in README.md?", "budget": {"max_cost_nanos": BUDGET}},
        actor="user",
        principal=ALICE,
    )
    r = log.model_request()
    use: Obj = {
        "type": "tool_use",
        "call_id": "call_1",
        "name": "read_file",
        "input": {"path": "README.md"},
    }
    log.model_response(r, [use], "tool_use", tokens(1000, 200))
    log.tool_call(r, "call_1", "read_file", {"path": "README.md"})
    log.add(
        "permission_decision",
        {
            "call_id": "call_1",
            "decision": "allow",
            "source": "policy",
            "rule_id": "conformance_allow",
        },
    )
    result(log, "call_1", "# demo\n")
    exceeded: Obj = {
        "scope": "run",
        "limit": "max_cost_nanos",
        "limit_value": BUDGET,
        "observed": 1000 * 3000 + 200 * 15000 + RESERVE,
        "observed_is_upper_bound": True,
    }
    log.add("budget_exceeded", exceeded)
    return log


def _budget(root: pathlib.Path) -> None:
    log = _budget_log()
    log.add("turn_completed", {"reason": "budget_exhausted"})
    reduce_case(
        root,
        (
            "budget-exceeded-terminal",
            FAM,
            "A run budget of 770,000,000 nanos rides on the user_input. Each attempt first "
            "reserves its model-declared bound (765,360,000). The first fits; after it settles "
            "at 6,000,000 the second reservation would reach 771,360,000, so budget_exceeded is "
            "recorded before any request exists and the turn ends budget_exhausted. Reservation "
            "before dispatch means no response can overshoot.",
        ),
        log,
        {"cost": cost(log)},
    )
    log = _budget_log()
    last = log.events.pop()
    log.lines.pop()
    log.add("budget_exceeded", {**obj(last["data"]), "scope": "ancestor"})
    negative(
        root,
        "budget-exceeded-ancestor-without-owner-rejected",
        "budget_exceeded with scope ancestor but no owner_thread_id: the ancestor whose "
        "budget refused the reservation must be named: invalid_line.",
        log,
        ("invalid_line", log.seq),
    )
    log = _budget_log()
    log.model_request()
    reject(
        root,
        (
            "model-request-after-budget-exceeded-rejected",
            FAM,
            "A model_request after budget_exceeded in the same run: invalid_transition.",
        ),
        log,
    )


def _output(root: pathlib.Path) -> None:
    fam = "tools_streaming"
    log = Log()
    started(log, [FINAL_OUTPUT], policy=policy(output=OUTPUT))
    user(log, "Is the bug fixed? Answer with final_output.")
    r = log.model_request()
    bad: Obj = {"fixed": "yes"}
    good: Obj = {"fixed": True}
    sha = OUTPUT["schema_sha256"]

    def step(cid: str, inp: Obj) -> Obj:
        use: Obj = {"type": "tool_use", "call_id": cid, "name": "final_output", "input": inp}
        return {"content": [use], "stop_reason": "tool_use", "usage": tokens(60, 8)}

    crash: Obj = {
        "request_event_id": r["event_id"],
        "provider_outcome": "unknown",
        "reason": "crash",
    }
    appended: list[JsonValue] = [
        {"type": "model_attempt_abandoned", "actor_kind": "recovery", "epoch": 2, "data": crash},
        {"type": "model_request", "data": {"attempt": 2}},
        {"type": "model_response", "data": {"content": step("call_1", bad)["content"]}},
        {"type": "tool_call", "data": {"call_id": "call_1", "name": "final_output", "input": bad}},
        {"type": "permission_decision", "data": {"call_id": "call_1", "decision": "allow"}},
        {"type": "output_validated", "data": {"schema_sha256": sha, "outcome": "rejected"}},
        {
            "type": "tool_result",
            "data": {"call_id": "call_1", "is_error": True, "origin": "not_executed"},
        },
        {"type": "model_request", "data": {"attempt": 1}},
        {"type": "model_response", "data": {"content": step("call_2", good)["content"]}},
        {"type": "tool_call", "data": {"call_id": "call_2", "name": "final_output", "input": good}},
        {"type": "permission_decision", "data": {"call_id": "call_2", "decision": "allow"}},
        {
            "type": "output_validated",
            "data": {"schema_sha256": sha, "outcome": "accepted", "value": good},
        },
        {
            "type": "tool_result",
            "data": {"call_id": "call_2", "is_error": False, "origin": "executed"},
        },
        {"type": "turn_completed", "data": {"reason": "end_turn"}},
    ]
    write_case(
        root,
        case(
            "structured-output-retry-then-accept",
            fam,
            "recover",
            "Structured output in tool mode. The first final_output candidate fails the pinned "
            "schema: output_validated{rejected} (the raw candidate is the tool_call) and an "
            "error result, so the model retries. The second is accepted with its typed value, "
            "and final_output ends the turn with no further model call.",
            model_script="model.json",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "appended": appended},
        extra={"model.json": {"responses": [step("call_1", bad), step("call_2", good)]}},
    )
    log = Log()
    started(log, [FINAL_OUTPUT], policy=policy(output=OUTPUT))
    user(log, "Is the bug fixed?")
    c = call(log, "final_output", bad)
    log.add(
        "output_validated",
        {
            "source_event_id": c["event_id"],
            "schema_sha256": sha,
            "outcome": "accepted",
            "value": bad,
        },
    )
    reject(
        root,
        (
            "output-validated-schema-mismatch-rejected",
            fam,
            "output_validated{accepted} whose value fails the pinned output schema: "
            "invalid_transition.",
        ),
        log,
    )
