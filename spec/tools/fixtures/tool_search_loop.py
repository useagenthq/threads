# pyright: strict
"""tool_search in the loop (recover cases): the runtime's own search, its tools_loaded, the
requests it renders (pinned by hash, so C7 and the history prefix hold on the runtime's bytes),
a call before any load, a crash before the search's result, and recovery of a loaded call."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import BRANCH, NOW, aref, obj, text, tokens
from .log import Log, reduce
from .pieces import (
    FINAL,
    REFUND,
    TAIL,
    call,
    case,
    user,
    write_case,
)
from .search_ref import search
from .tool_search import JIRA, current, pinned, pinned_ref, searched

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

EXACT = "mcp__jira__create_issue, mcp__jira__add_comment"
FILE: Obj = {"project": "WEB", "summary": "Login fails"}
NOT_LOADED = "tool_not_loaded: mcp__jira__create_issue; find it with tool_search first"


def build(root: pathlib.Path) -> None:
    _loads_and_calls(root)
    _before_load(root)
    _rerun(root)
    _loaded_call(root)


def _jira() -> Log:
    log = Log()
    pinned(log, JIRA)
    user(log, "File a bug about the login page.")
    return log


def _use(call_id: str, name: str, inp: Obj) -> Obj:
    return {
        "content": [{"type": "tool_use", "call_id": call_id, "name": name, "input": inp}],
        "stop_reason": "tool_use",
        "usage": tokens(80, 20),
    }


def _request(e: Obj) -> Obj:
    """A model_request the runtime must append, as a matcher on its exact bytes."""
    d = obj(e["data"])
    return {
        "type": "model_request",
        "epoch": 2,
        "data": {
            "attempt": 1,
            "declared_prefix": d["declared_prefix"],
            "request_ref": {"sha256": obj(d["request_ref"])["sha256"]},
        },
    }


def _loads_and_calls(root: pathlib.Path) -> None:
    log = _jira()
    sim = log.copy()
    searched(sim, EXACT, "call_1")
    new = sim.events[len(log.events) :]
    first, lines, tools = (
        _request(new[0]),
        obj(new[4]["data"])["preview"],
        obj(new[5]["data"])["tools"],
    )
    second = _request(sim.model_request())
    bodies = [
        sim.artifacts[text(obj(obj(r["data"])["request_ref"])["sha256"])] for r in (first, second)
    ]
    if not bodies[1].startswith(bodies[0]):
        raise AssertionError("a load must only append to the request")
    out = "created WEB-7"
    write_case(
        root,
        case(
            "tool-search-loads-and-calls",
            "tools_streaming",
            "recover",
            "End to end on the runtime: the model searches two exact names, the tool_search "
            "result lists both and the same batch appends tools_loaded with their pinned "
            "spec_refs; the next request renders their full specs; the model then calls one and "
            "it runs. Both requests are pinned by hash: line 0 (the declared prefix) is the "
            "same, and the second request's bytes extend the first's.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                first,
                {
                    "type": "model_response",
                    "data": {"content": _use("call_1", "tool_search", {"query": EXACT})["content"]},
                },
                {"type": "tool_call", "data": {"call_id": "call_1", "name": "tool_search"}},
                {"type": "permission_decision", "data": {"call_id": "call_1", "decision": "allow"}},
                {
                    "type": "tool_result",
                    "actor_kind": "tool",
                    "data": {
                        "call_id": "call_1",
                        "is_error": False,
                        "origin": "executed",
                        "preview": lines,
                    },
                },
                {
                    "type": "tools_loaded",
                    "actor_kind": "host",
                    "data": {"call_id": "call_1", "tools": tools},
                },
                second,
                {"type": "model_response", "data": {"stop_reason": "tool_use"}},
                {
                    "type": "tool_call",
                    "data": {"call_id": "call_2", "name": "mcp__jira__create_issue"},
                },
                {"type": "permission_decision", "data": {"call_id": "call_2", "decision": "allow"}},
                {"type": "effect_begin", "data": {"call_id": "call_2", "attempt": 1}},
                {"type": "effect_commit", "data": {"call_id": "call_2"}},
                {
                    "type": "tool_result",
                    "data": {
                        "call_id": "call_2",
                        "is_error": False,
                        "origin": "executed",
                        "preview": out,
                    },
                },
                *TAIL,
            ],
            "sandbox": {"dispatches": {"mcp__jira__create_issue": 1}},
        },
        extra={
            "model.json": {
                "responses": [
                    _use("call_1", "tool_search", {"query": EXACT}),
                    _use("call_2", "mcp__jira__create_issue", FILE),
                    FINAL,
                ]
            },
            "sandbox.json": {"tools": {"mcp__jira__create_issue": {"output": out}}},
        },
    )


def _before_load(root: pathlib.Path) -> None:
    log = _jira()
    write_case(
        root,
        case(
            "tool-search-call-before-load",
            "tools_streaming",
            "recover",
            "The model calls a deferred tool without loading it. The call fails before any "
            "effect: tool_result{not_executed} with the one pinned text, no permission decision "
            "and no effect event, and the tool never runs.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {"type": "model_request", "data": {"attempt": 1}},
                {"type": "model_response", "data": {"stop_reason": "tool_use"}},
                {
                    "type": "tool_call",
                    "data": {"call_id": "call_1", "name": "mcp__jira__create_issue"},
                },
                {
                    "type": "tool_result",
                    "data": {
                        "call_id": "call_1",
                        "is_error": True,
                        "origin": "not_executed",
                        "preview": NOT_LOADED,
                    },
                },
                *TAIL,
            ],
            "sandbox": {"dispatches": {"mcp__jira__create_issue": 0}},
        },
        extra={
            "model.json": {"responses": [_use("call_1", "mcp__jira__create_issue", FILE), FINAL]},
            "sandbox.json": {"tools": {"mcp__jira__create_issue": {"output": "created WEB-7"}}},
        },
    )


def _rerun(root: pathlib.Path) -> None:
    log = _jira()
    call(log, "tool_search", {"query": EXACT})
    deferred, others = current(log)
    found = search(EXACT, 5, deferred, others)
    lines = "\n".join(found.lines)
    tools: list[JsonValue] = [{"name": n, "spec_ref": pinned_ref(log, n)} for n in found.loaded]
    write_case(
        root,
        case(
            "tool-search-recover-reruns",
            "cancellation_resume",
            "recover",
            "A crash after a tool_search call was recorded and allowed, before its result. The "
            "search is read_only and depends only on the log and the pinned tables, so recovery "
            "runs it again: the same result text and the same tools_loaded bytes.",
            model_script="model.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "tool_result",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_1",
                        "is_error": False,
                        "origin": "executed",
                        "preview": lines,
                    },
                },
                {"type": "tools_loaded", "epoch": 2, "data": {"call_id": "call_1", "tools": tools}},
                *TAIL,
            ],
        },
        extra={"model.json": {"responses": [FINAL]}},
    )


def _loaded_call(root: pathlib.Path) -> None:
    log = Log()
    pinned(log, [REFUND])
    user(log, "Refund charge ch_1.")
    searched(log, "refund_card", "call_1")
    call(log, "refund_card", {"charge": "ch_1"}, "call_2")
    log.add("effect_begin", {"call_id": "call_2", "attempt": 1})
    key = f"{BRANCH}:call_2"
    out = "refunded ch_1 re_77"
    write_case(
        root,
        case(
            "recover-loaded-call-effect-class",
            "cancellation_resume",
            "recover",
            "Crash after effect_begin of a call to refund_card, a reconcilable tool that was "
            "deferred and loaded by tool_search. Its call-time spec is the pinned stub, which "
            "carries the effect class: recovery reconciles it with a lookup and never sends it "
            "again, exactly as for a tool pinned inline.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "effect_unknown",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {"call_id": "call_2", "reason": "crash_after_begin"},
                },
                {
                    "type": "effect_resolved",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_2",
                        "outcome": "confirmed_success",
                        "by": "reconcile",
                        "result_ref": aref(out.encode(), "text/plain"),
                    },
                },
                {
                    "type": "tool_result",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_2",
                        "is_error": False,
                        "origin": "executed",
                        "preview": out,
                    },
                },
                *TAIL,
            ],
            "sandbox": {
                "dispatches": {"refund_card": 0},
                "new_executions": {"refund_card": 0},
                "lookups": {"refund_card": 1},
            },
        },
        extra={
            "model.json": {"responses": [FINAL]},
            "sandbox.json": {
                "tools": {
                    "refund_card": {
                        "output": "refunded",
                        "lookup": {key: {"result": "found", "final": True, "output": out}},
                    }
                }
            },
        },
    )
