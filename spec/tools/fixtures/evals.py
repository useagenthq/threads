# pyright: strict
"""Eval conformance cases (lane 22): the free checks over saved cases (replay, rerun, the
not-runnable reasons) and drift against dry pins. Hooks and older case formats are in
evals_hooks.py and evals_formats.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ADAPTER, MODEL, PARAMS, sha, tool
from .evals_turn import Turn
from .evals_write import RERUN_OK, passed, result, saved_case, skipped, write_eval
from .jcs import JsonValue, Obj, canonical
from .render import render

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from .log import Log

LOOKUP = tool("lookup_order", "Look up an order by id.", {"id": {"type": "string"}}, "read_only")
REFUND = tool("issue_refund", "Refund an order.", {"id": {"type": "string"}}, "unguarded")
MUST_LOOKUP: list[JsonValue] = [{"type": "tool_call", "data": {"name": "lookup_order"}}]
INSTRUCTIONS = "You handle refunds."


def pinned(tools: list[JsonValue], **more: JsonValue) -> Obj:
    """thread_started data as agent() pins it (config_hash over the rest, as the corpus does)."""
    cfg: Obj = {
        "agent_name": "support",
        "instructions": INSTRUCTIONS,
        "model": MODEL,
        "model_params": PARAMS,
        "adapter": ADAPTER,
        "tools": tools,
        **more,
    }
    return {**cfg, "config_hash": sha(canonical(cfg))}


def start(log: Log, tools: list[JsonValue] | None = None, **more: JsonValue) -> Obj:
    return log.add("thread_started", pinned([LOOKUP, REFUND] if tools is None else tools, **more))


def refund_turn(t: Turn, text: str = "Please refund order 42.") -> None:
    """Look the order up, refund it, answer: a read-only call and a stubbed effect."""
    t.user(text)
    r1 = t.request()
    t.uses(r1, [("c1", "lookup_order", {"id": "42"})])
    t.read("c1", "lookup_order", {"id": "42"}, "order 42: shipped 12 days ago")
    r2 = t.request()
    t.uses(r2, [("c2", "issue_refund", {"id": "42"})])
    t.effect("c2", "issue_refund", {"id": "42"}, "refunded order 42")
    t.say("Refunded order 42; it is inside the 30-day window.")
    t.done()


def first_turn(log: Log, provider: str = "fake") -> Turn:
    start(log, sandbox_provider=provider)
    t = Turn(log)
    refund_turn(t)
    return t


def build(root: pathlib.Path) -> None:
    write_eval(root, "eval-pass", lambda d: _one(d, "refund", first_turn))
    write_eval(
        root,
        "eval-real-sandbox-portable",
        lambda d: _one(d, "refund-e2b", lambda log: first_turn(log, "e2b")),
    )
    write_eval(root, "eval-must-unmatched", _must_unmatched)
    write_eval(root, "eval-replay-mismatch", _replay_mismatch)
    write_eval(root, "eval-stub-unmatched", _stub_unmatched)
    write_eval(root, "eval-script-left", _script_left)
    write_eval(root, "eval-not-runnable", _not_runnable)


def _one(d: pathlib.Path, name: str, scenario: Callable[[Log], Turn]) -> list[Obj]:
    saved_case(d, name, scenario, must=MUST_LOOKUP)
    return [passed(name)]


def _must_unmatched(d: pathlib.Path) -> list[Obj]:
    must: Obj = {"type": "tool_call", "data": {"name": "cancel_order"}}
    saved_case(d, "refund", first_turn, must=[must])
    rerun: Obj = {**RERUN_OK, "ok": False, "unmatched": [must]}
    reason = 'rerun: unmatched tool_call{"name":"cancel_order"}'
    return [result("refund", "failed", reason, {"replay": {"ok": True}, "rerun": rerun})]


def _corrupt_request(log: Log) -> Obj:
    """A request whose request_ref names its artifact with a wrong byte count."""
    body, line0 = render(log.events, log.artifacts, None, None)
    ref = {**log.art(body, "application/x-ndjson"), "bytes": len(body) + 1}
    data: Obj = {
        "attempt": 1,
        "request_ref": ref,
        "declared_prefix": {"bytes": len(line0), "sha256": sha(line0)},
    }
    return log.add("model_request", data)


def _second_turn(log: Log) -> Turn:
    start(log)
    first = Turn(log)
    first.user("Hi.")
    req = _corrupt_request(log)
    first.respond(req, [{"type": "text", "text": "Hello."}], "end_turn")
    first.done()
    t = Turn(log)
    refund_turn(t)
    return t


def _replay_mismatch(d: pathlib.Path) -> list[Obj]:
    saved_case(d, "second-turn", _second_turn, must=MUST_LOOKUP)
    replay: Obj = {"ok": False, "code": "artifact_corrupt", "seq": 3}
    return [result("second-turn", "failed", "replay: artifact_corrupt", {"replay": replay})]


def _stub_unmatched(d: pathlib.Path) -> list[Obj]:
    saved_case(d, "refund", first_turn, must=MUST_LOOKUP, files={"stubs.json": {"stubs": []}})
    rerun: Obj = {
        **RERUN_OK,
        "ok": False,
        "script_left": 1,
        "stubs_unmatched": 1,
        "mismatch": {"index": 11, "want": "effect_commit", "got": "effect_resolved"},
    }
    checks: Obj = {"replay": {"ok": True}, "rerun": rerun}
    return [result("refund", "failed", "rerun: unmatched_external_op", checks)]


def _script_left(d: pathlib.Path) -> list[Obj]:
    extra: Obj = {
        "content": [{"type": "text", "text": "One more thing."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }

    def scenario(log: Log) -> Turn:
        t = first_turn(log)
        t.responses.append(extra)
        return t

    saved_case(d, "refund", scenario, must=MUST_LOOKUP)
    rerun: Obj = {**RERUN_OK, "ok": False, "script_left": 1}
    checks: Obj = {"replay": {"ok": True}, "rerun": rerun}
    return [result("refund", "failed", "rerun: 1 recorded model replies left", checks)]


def _not_runnable(d: pathlib.Path) -> list[Obj]:
    offline: Obj = {"runnable": False, "reason": "unsettled_effect"}
    saved_case(d, "refund", first_turn, must=MUST_LOOKUP, offline=offline)
    return [skipped("refund", "offline_not_runnable:unsettled_effect")]
