# pyright: strict
"""Rule 17: a tools_changed never makes dispatch less safe (spec/schema/README.md)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .changes import MCP_SEARCH
from .common import BRANCH, DAY, NOW, aref, num, sha, tool
from .jcs import canonical
from .log import Log, reduce
from .pieces import (
    CHARGE,
    EMAIL,
    FINAL,
    READ_FILE,
    REFUND,
    TAIL,
    call,
    case,
    effect_call,
    reject,
    started,
    user,
    write_case,
)

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

FAM = "tools_streaming"
DEPLOY = tool("mcp__ops__deploy", "Deploy the app.", {"env": {"type": "string"}}, "unguarded")
WIDE: Obj = {"type": "object"}


def changed(log: Log, tools: list[JsonValue], cause: Obj | None = None) -> None:
    data: Obj = {"tools": tools, "tools_hash": sha(canonical(tools))}
    log.add("tools_changed", data if cause is None else {**data, "cause": cause})


def _one(pinned: list[JsonValue], tools: list[JsonValue], cause: Obj | None = None) -> Log:
    log = Log()
    started(log, pinned)
    changed(log, tools, cause)
    return log


def _pinned() -> list[tuple[str, str, Log]]:
    flip = Log()
    started(flip, [REFUND])
    user(flip, "Refund charge ch_1.")
    call(flip, "refund_card", {"charge": "ch_1"})
    changed(flip, [{**REFUND, "effect_class": "read_only"}])
    readded = _one([REFUND, READ_FILE], [READ_FILE])
    changed(readded, [READ_FILE, {**REFUND, "effect_class": "read_only"}])
    return [
        (
            "tools-changed-effect-class-flip-rejected",
            "A pending refund_card call (reconcilable, no effect_begin), then a tools_changed "
            "that reclasses refund_card as read_only. Recovery would re-send it with no durable "
            "effect record, so a pinned tool's effect class never changes.",
            flip,
        ),
        (
            "tools-changed-dedup-window-rejected",
            "A tools_changed doubles a pinned idempotent tool's dedup_window_ms.",
            _one([CHARGE], [{**CHARGE, "dedup_window_ms": 2 * DAY}]),
        ),
        (
            "tools-changed-ends-turn-rejected",
            "A tools_changed gives a pinned tool ends_turn.",
            _one([EMAIL], [{**EMAIL, "ends_turn": True}]),
        ),
        (
            "tools-changed-input-schema-widened-rejected",
            "A tools_changed widens a pinned tool's input_schema to any object.",
            _one([EMAIL], [{**EMAIL, "input_schema": WIDE}]),
        ),
        (
            "tools-changed-readded-reclassed-rejected",
            "refund_card is removed, then re-added as read_only: a re-add keeps the pinned spec.",
            readded,
        ),
    ]


def _deferred() -> list[tuple[str, str, Log]]:
    deferred: Obj = {**DEPLOY, "defer_loading": True}
    return [
        (
            "tools-changed-loaded-without-search-rejected",
            "A deferred tool is loaded (defer_loading dropped) by a tools_changed with no cause: "
            "only a tool_search loads a deferred tool.",
            _one([READ_FILE, deferred], [READ_FILE, DEPLOY]),
        ),
        (
            "tools-changed-search-unknown-call-rejected",
            "A tools_changed{cause: tool_search} names call_9, which no tool_call recorded.",
            _one(
                [READ_FILE, deferred],
                [READ_FILE, DEPLOY],
                {"kind": "tool_search", "call_id": "call_9"},
            ),
        ),
        (
            "tools-changed-redeferred-rejected",
            "A tools_changed defers a loaded tool: defer_loading only goes from true to absent.",
            _one([READ_FILE], [{**READ_FILE, "defer_loading": True}]),
        ),
    ]


def _added() -> list[tuple[str, str, Log]]:
    later = _one([READ_FILE], [READ_FILE, MCP_SEARCH])
    changed(later, [READ_FILE, {**MCP_SEARCH, "input_schema": WIDE}])
    return [
        (
            "tools-changed-added-not-unguarded-rejected",
            "A tools_changed adds a tool thread_started did not pin as read_only: an added tool "
            "is unguarded, so uncertainty about it always parks.",
            _one([READ_FILE], [READ_FILE, {**MCP_SEARCH, "effect_class": "read_only"}]),
        ),
        (
            "tools-changed-added-ends-turn-rejected",
            "A tools_changed adds an unguarded tool with ends_turn.",
            _one([READ_FILE], [READ_FILE, {**MCP_SEARCH, "ends_turn": True}]),
        ),
        (
            "tools-changed-added-tool-changed-rejected",
            "An added tool keeps the spec it was first added with: a later set widens its "
            "input_schema.",
            later,
        ),
    ]


def _causes() -> list[tuple[str, str, Log]]:
    tools: list[JsonValue] = [READ_FILE, MCP_SEARCH]
    return [
        (
            "tools-changed-cause-mcp-list-changed-rejected",
            "cause mcp_list_changed has no writer yet, so an imported one is refused.",
            _one([READ_FILE], tools, {"kind": "mcp_list_changed", "server": "docs"}),
        ),
        (
            "tools-changed-cause-host-rejected",
            "cause host has no writer yet (an absent cause means the host), so it is refused.",
            _one([READ_FILE], tools, {"kind": "host"}),
        ),
    ]


def _readded(root: pathlib.Path) -> None:
    log = _one([READ_FILE, EMAIL], [READ_FILE])
    changed(log, [READ_FILE, EMAIL])
    write_case(
        root,
        case(
            "tools-changed-removed-and-readded",
            FAM,
            "reduce",
            "The host removes send_email, then re-adds it with its pinned spec: removal is "
            "always allowed, and a re-add that keeps the pinned spec is too.",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW)},
    )


def _after_call(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE, CHARGE])
    inp: Obj = {"amount_cents": 2000, "customer": "c_42"}
    effect_call(log, CHARGE, inp, "Charge customer c_42 $20.")
    b = log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    changed(log, [READ_FILE])
    now = num(b["time"]) + 60_000
    out = b"charged ch_001 amount_cents=2000"
    write_case(
        root,
        case(
            "tools-changed-after-call-keeps-call-spec",
            "cancellation_resume",
            "recover",
            "Crash after effect_begin of an idempotent tool, then the host removed the tool from "
            "the set (a valid tools_changed). Recovery classes the pending call by the spec it "
            "was made under, not the latest set: it settles safe_to_retry inside the dedup "
            "window and re-dispatches with the same effect key, as if the set had not changed.",
            now,
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, now),
            "appended": [
                {
                    "type": "effect_unknown",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {"call_id": "call_1", "reason": "crash_after_begin"},
                },
                {
                    "type": "effect_resolved",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_1",
                        "outcome": "safe_to_retry",
                        "by": "provider_dedup",
                    },
                },
                {"type": "effect_begin", "epoch": 2, "data": {"call_id": "call_1", "attempt": 2}},
                {
                    "type": "effect_commit",
                    "data": {"call_id": "call_1", "result_ref": aref(out, "text/plain")},
                },
                {
                    "type": "tool_result",
                    "actor_kind": "tool",
                    "data": {
                        "call_id": "call_1",
                        "is_error": False,
                        "origin": "executed",
                        "preview": out.decode(),
                    },
                },
                *TAIL,
            ],
            "sandbox": {
                "dispatches": {"charge_card": 1},
                "new_executions": {"charge_card": 0},
            },
        },
        extra={
            "model.json": {"responses": [FINAL]},
            "sandbox.json": {
                "tools": {
                    "charge_card": {
                        "output": "charged ch_999 amount_cents=2000",
                        "executed_keys": {f"{BRANCH}:call_1": out.decode()},
                    }
                }
            },
        },
    )


def build(root: pathlib.Path) -> None:
    for name, desc, log in [*_pinned(), *_deferred(), *_added(), *_causes()]:
        reject(root, (name, "log", f"Rule 17: {desc}"), log)
    _readded(root)
    _after_call(root)
